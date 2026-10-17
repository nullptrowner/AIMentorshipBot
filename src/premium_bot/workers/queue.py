import asyncio
import contextlib
import uuid
from datetime import timedelta

import structlog
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import Update
from redis.exceptions import LockError
from sqlalchemy import delete, or_, select, text, update

from premium_bot.application.common import enqueue
from premium_bot.bot.dispatcher import dispatcher
from premium_bot.db import Delivery, Invoice, Job, Media, Task
from premium_bot.domain import TERMINAL, now
from premium_bot.observability import TASKS
from premium_bot.runtime import Runtime
from premium_bot.workers import billing, generation, notifications

logger = structlog.get_logger()


async def claim(rt: Runtime) -> Task | None:
    async with rt.sessions.begin() as s:
        item = await s.scalar(
            select(Task)
            .where(
                Task.done.is_(False),
                Task.due_at <= now(),
                or_(Task.lease_until.is_(None), Task.lease_until < now()),
            )
            .order_by(Task.due_at, Task.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if item:
            item.lease_until = now() + timedelta(seconds=90)
            item.lease_owner = uuid.uuid4()
            item.attempts += 1
        return item


async def heartbeat(rt: Runtime, task: Task) -> None:
    while True:
        await asyncio.sleep(20)
        try:
            async with rt.sessions.begin() as s:
                await s.execute(
                    update(Task)
                    .where(Task.id == task.id, Task.lease_owner == task.lease_owner)
                    .values(lease_until=now() + timedelta(seconds=90))
                )
        except Exception as exc:
            logger.warning(
                "lease_renewal_failed", task_id=str(task.id), error_type=type(exc).__name__
            )


async def complete(
    rt: Runtime, task: Task, error: str | None = None, delay: int | None = None
) -> None:
    async with rt.sessions.begin() as s:
        stored = await s.get(Task, task.id, with_for_update=True)
        if stored is None or stored.lease_owner != task.lease_owner:
            return
        stored.lease_until, stored.lease_owner = None, None
        stored.last_error = error
        if delay is None:
            stored.done = True
            if task.kind == "telegram" and error is None:
                stored.payload = {}
        else:
            stored.due_at = now() + timedelta(seconds=delay)


async def sweep(rt: Runtime) -> None:
    async with rt.sessions.begin() as s:
        acquired = await s.scalar(text("SELECT pg_try_advisory_xact_lock(741913)"))
        if not acquired:
            return
        # One recovery task per time bucket. DB remains the source of truth.
        bucket = int(now().timestamp()) // 60
        jobs = list(await s.scalars(select(Job).where(Job.status.in_(["queued", "processing"]))))
        protected = {mid for job in jobs for mid in job.input_ids}
        for job in jobs:
            kind = "sync" if job.submit_started else "submit"
            pending = await s.scalar(
                select(Task.id)
                .where(
                    Task.done.is_(False),
                    Task.kind.in_(["submit", "sync"]),
                    Task.payload.contains({"job_id": str(job.id)}),
                )
                .limit(1)
            )
            if pending is None:
                await enqueue(s, f"recover:{job.id}:{bucket}", kind, {"job_id": str(job.id)})
        await s.execute(
            update(Job)
            .where(Job.status.in_(["created", "awaiting_payment"]), Job.expires_at < now())
            .values(status="expired")
        )
        media = list(
            await s.scalars(
                select(Media).where(Media.expires_at < now(), Media.deleted.is_(False)).limit(100)
            )
        )
        for item in media:
            if str(item.id) in protected:
                continue
            # Row lock coordinates expiration with generation confirmations.
            locked = await s.get(Media, item.id, with_for_update=True, populate_existing=True)
            if locked is not None and locked.expires_at < now():
                await asyncio.to_thread(rt.media.path(locked.path).unlink, missing_ok=True)
                locked.deleted = True
        await s.execute(
            update(Invoice)
            .where(
                Invoice.status.in_(["creating", "active", "checkout"]), Invoice.expires_at < now()
            )
            .values(status="expired")
        )
        invoices = list(
            await s.scalars(
                select(Invoice).where(
                    Invoice.provider == "crypto", Invoice.status.in_(["active", "creating_unknown"])
                )
            )
        )
        for invoice in invoices:
            if invoice.provider_id:
                await enqueue(
                    s,
                    f"recover-invoice:{invoice.id}:{bucket}",
                    "crypto_sync",
                    {"invoice_id": str(invoice.id)},
                )
        successful = list(
            await s.scalars(
                select(Job.id).where(
                    Job.status.in_(TERMINAL), Job.finished_at > now() - timedelta(days=1)
                )
            )
        )
        for job_id in successful:
            if await s.get(Delivery, f"generation:{job_id}") is None:
                await enqueue(
                    s, f"recover-notify:{job_id}:{bucket}", "notify", {"job_id": str(job_id)}
                )
        await s.execute(
            delete(Task).where(Task.done.is_(True), Task.created_at < now() - timedelta(days=7))
        )
    # Files left by a crash before DB commit are reclaimed only after an hour.
    async with rt.sessions() as s:
        paths = set(await s.scalars(select(Media.path).where(Media.deleted.is_(False))))
    import time

    for path in await asyncio.to_thread(lambda: list(rt.cfg.media_dir.iterdir())):
        if path.is_file() and path.name not in paths and time.time() - path.stat().st_mtime > 3600:
            await asyncio.to_thread(path.unlink, missing_ok=True)


async def worker(rt: Runtime, stop: asyncio.Event) -> None:
    dp = dispatcher(rt)

    async def dispatch(task: Task) -> None:
        payload = task.payload
        if task.kind == "telegram":
            await dp.feed_update(rt.bot, Update.model_validate(payload), rt=rt)
        elif task.kind == "submit":
            await generation.submit(rt, uuid.UUID(payload["job_id"]))
        elif task.kind == "sync":
            await generation.synchronize(rt, uuid.UUID(payload["job_id"]))
        elif task.kind == "notify":
            await notifications.notify_generation(rt, uuid.UUID(payload["job_id"]))
        elif task.kind == "invoice":
            await billing.issue(rt, uuid.UUID(payload["invoice_id"]))
        elif task.kind == "crypto_sync":
            await billing.sync_crypto(rt, uuid.UUID(payload["invoice_id"]))
        elif task.kind == "message":
            await rt.bot.send_message(int(payload["user_id"]), payload["text"])
        elif task.kind == "broadcast":
            await notifications.broadcast(rt, uuid.UUID(payload["broadcast_id"]))
        elif task.kind == "stars_reconcile":
            await billing.reconcile_stars(rt)
        else:
            raise ValueError("Unknown task kind")

    async def consumer() -> None:
        while not stop.is_set():
            try:
                task = await claim(rt)
            except Exception as exc:
                logger.warning("queue_unavailable", error_type=type(exc).__name__)
                await asyncio.sleep(2)
                continue
            if task is None:
                try:
                    await asyncio.wait_for(stop.wait(), 0.5)
                except TimeoutError:
                    continue
                return
            beat = asyncio.create_task(heartbeat(rt, task))
            try:
                async with asyncio.timeout(720):
                    await dispatch(task)
                await complete(rt, task)
                TASKS.labels(task.kind, "succeeded").inc()
            except generation.RetryLater as exc:
                await complete(rt, task, delay=exc.seconds)
            except TelegramRetryAfter as exc:
                await complete(rt, task, "TelegramRetryAfter", delay=exc.retry_after + 1)
            except LockError:
                await complete(rt, task, delay=5)
            except asyncio.CancelledError:
                # Lease expires; the next worker reconciles persisted intent instead of resubmitting.
                raise
            except Exception as exc:
                error = type(exc).__name__
                TASKS.labels(task.kind, "failed").inc()
                logger.error(
                    "task_failed",
                    task_id=str(task.id),
                    kind=task.kind,
                    error_type=error,
                    attempt=task.attempts,
                )
                if task.attempts < 20:
                    await complete(rt, task, error, delay=min(300, 2 ** min(task.attempts, 8)))
                else:
                    await complete(rt, task, error)
                    await generation.alert(
                        rt,
                        "task:" + str(task.id),
                        f"Задача {task.id} ({task.kind}) требует проверки после повторных ошибок: {error}.",
                    )
            finally:
                beat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await beat

    async def periodic() -> None:
        last = 0
        while not stop.is_set():
            try:
                await rt.redis.set("worker:heartbeat", str(now().timestamp()), ex=90)
                await sweep(rt)
                minute = int(now().timestamp()) // 300
                if minute != last:
                    async with rt.sessions.begin() as s:
                        await enqueue(s, f"stars-reconcile:{minute}", "stars_reconcile", {})
                    last = minute
            except Exception as exc:
                logger.error("sweep_failed", error_type=type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), 30)
            except TimeoutError:
                continue

    tasks = [asyncio.create_task(consumer()) for _ in range(6)] + [asyncio.create_task(periodic())]
    await stop.wait()
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), 45)
    except TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    await dp.storage.close()

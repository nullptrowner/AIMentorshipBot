import uuid
from datetime import timedelta

import httpx
from sqlalchemy import func, select, text

from premium_bot.application.common import enqueue
from premium_bot.application.jobs import finish
from premium_bot.db import Job, Media
from premium_bot.domain import TERMINAL, BusinessError, Json, now
from premium_bot.infrastructure.replicate import REGISTRY, validate_input
from premium_bot.infrastructure.security import prediction_binding
from premium_bot.observability import GENERATION_SECONDS, GENERATIONS
from premium_bot.runtime import Runtime


class RetryLater(Exception):
    def __init__(self, seconds: int = 10) -> None:
        self.seconds = seconds


async def alert(rt: Runtime, key: str, message: str) -> None:
    async with rt.sessions.begin() as s:
        for uid in rt.cfg.admin_ids:
            await enqueue(s, f"alert:{key}:{uid}", "message", {"user_id": uid, "text": message})


async def schedule_sync(rt: Runtime, job_id: uuid.UUID, delay: int = 0) -> None:
    async with rt.sessions.begin() as s:
        await enqueue(
            s,
            f"poll:{job_id}:{int((now() + timedelta(seconds=delay)).timestamp()) // rt.cfg.poll_seconds}",
            "sync",
            {"job_id": str(job_id)},
            delay=delay,
        )


async def submit(rt: Runtime, job_id: uuid.UUID) -> None:
    async with rt.redis.lock(f"generation:{job_id}", timeout=180, blocking_timeout=1):
        async with rt.sessions.begin() as s:
            job = await s.get(Job, job_id, with_for_update=True)
            if job is None or job.status in TERMINAL:
                return
            if job.status not in {"queued", "processing"}:
                return
            if not job.submit_started and now() - job.created_at > timedelta(
                seconds=rt.cfg.job_timeout_seconds
            ):
                await finish(
                    s, job, "expired", "Истекло время ожидания в очереди. Резерв возвращён."
                )
                return
            if job.prediction_id or job.submit_started:
                await enqueue(
                    s, f"resume:{job.id}:{uuid.uuid4().hex}", "sync", {"job_id": str(job.id)}
                )
                return
            await s.execute(text("SELECT pg_advisory_xact_lock(741912)"))
            count = await s.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.submit_started.is_(True), Job.status.not_in(TERMINAL))
            )
            if (count or 0) >= rt.cfg.concurrent_generations:
                raise RetryLater(10)
            items = [await s.get(Media, uuid.UUID(mid)) for mid in job.input_ids]
            if any(i is None or i.deleted for i in items):
                await finish(s, job, "failed", "Исходные файлы недоступны.")
                return
            images = [rt.media.url(i) for i in items if i is not None and i.kind == "photo"]
            audio = next(
                (rt.media.url(i) for i in items if i is not None and i.kind == "audio"), None
            )
            data = REGISTRY[job.model_key].build(
                job.snapshot["final_prompt"], job.params, images, audio, job.seconds
            )
            validate_input(job.snapshot["schema"], data)
            job.submit_started, job.status, job.started_at = True, "processing", now()
            job.snapshot = {**job.snapshot, "submitted_input": data}
            version = job.snapshot["version"]
        try:
            result = await rt.replicate.create(
                version,
                data,
                f"{rt.cfg.public_url}/webhooks/replicate/{job_id}?binding={prediction_binding(str(job_id), rt.cfg.media_secret.get_secret_value())}",
                rt.cfg.job_timeout_seconds,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                async with rt.sessions.begin() as s:
                    job = await s.get(Job, job_id, with_for_update=True)
                    if job and not job.prediction_id and job.status not in TERMINAL:
                        job.submit_started, job.status = False, "queued"
                raise RetryLater(30) from exc
            if 400 <= exc.response.status_code < 500:
                async with rt.sessions.begin() as s:
                    job = await s.get(Job, job_id, with_for_update=True)
                    if job:
                        await finish(
                            s, job, "failed", "Replicate отклонил запрос. Резерв возвращён."
                        )
                return
            await uncertain(rt, job_id)
            return
        except httpx.RequestError:
            await uncertain(rt, job_id)
            return
        async with rt.sessions.begin() as s:
            job = await s.get(Job, job_id, with_for_update=True)
            if job and job.status not in TERMINAL:
                job.prediction_id = str(result["id"])
                job.error = None
        await schedule_sync(rt, job_id)


async def uncertain(rt: Runtime, job_id: uuid.UUID) -> None:
    async with rt.sessions.begin() as s:
        job = await s.get(Job, job_id, with_for_update=True)
        if job and not job.prediction_id:
            job.error = "Ожидается подтверждение запуска от Replicate. Требуется сверка при отсутствии webhook."
    await alert(
        rt,
        str(job_id),
        f"Задание {job_id}: результат запроса запуска неизвестен. Повторный запуск заблокирован; проверьте Replicate и привяжите prediction ID в админке.",
    )
    await schedule_sync(rt, job_id, 30)


async def synchronize(rt: Runtime, job_id: uuid.UUID) -> None:
    async with rt.redis.lock(f"generation:{job_id}", timeout=900, blocking_timeout=1):
        async with rt.sessions() as s:
            job = await s.get(Job, job_id)
            if job is None or job.status in TERMINAL:
                return
            prediction_id = job.prediction_id
        if not prediction_id:
            if job.started_at and now() - job.started_at > timedelta(
                seconds=rt.cfg.job_timeout_seconds
            ):
                await alert(
                    rt,
                    f"unresolved:{job_id}",
                    f"Задание {job_id}: нет prediction ID. Нужна ручная сверка. Средства остаются зарезервированы.",
                )
            await schedule_sync(rt, job_id, 60)
            return
        if (
            job.cancel_requested
            or job.started_at
            and now() - job.started_at > timedelta(seconds=rt.cfg.job_timeout_seconds)
        ):
            try:
                await rt.replicate.cancel(prediction_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {409, 422}:
                    raise
        result = await rt.replicate.get(prediction_id)
        try:
            await apply_result(rt, job_id, result)
        except BusinessError as exc:
            async with rt.sessions.begin() as s:
                current = await s.get(Job, job_id, with_for_update=True)
                if current:
                    await finish(s, current, "failed", str(exc)[:400] + " Резерв возвращён.")


async def apply_result(rt: Runtime, job_id: uuid.UUID, result: Json) -> None:
    state = result.get("status")
    if state in {"starting", "processing"}:
        await schedule_sync(rt, job_id, rt.cfg.poll_seconds)
        return
    if state not in {"succeeded", "failed", "canceled"}:
        raise RuntimeError("Unknown provider status requires reconciliation")
    if state == "succeeded":
        async with rt.sessions() as s:
            job = await s.get(Job, job_id)
            if job is None or job.status in TERMINAL:
                return
            existing = list(
                await s.scalars(select(Media).where(Media.job_id == job_id, Media.output.is_(True)))
            )
        output = result.get("output")
        urls = output if isinstance(output, list) else [output]
        if not urls or len(urls) > 4 or any(not isinstance(url, str) for url in urls):
            raise BusinessError("Replicate вернул неожиданный формат результата.")
        if not existing:
            downloaded: list[Media] = []
            saved = False
            try:
                async with rt.redis.lock("media-processing", timeout=900, blocking_timeout=1):
                    for url in urls:
                        downloaded.append(
                            await rt.media.download(
                                str(url), job.user_id, job_id, job.snapshot["kind"]
                            )
                        )
                async with rt.sessions.begin() as s:
                    job = await s.get(Job, job_id, with_for_update=True)
                    if job is None or job.status in TERMINAL:
                        for media in downloaded:
                            rt.media.path(media.path).unlink(missing_ok=True)
                        return
                    s.add_all(downloaded)
                    await finish(s, job, "succeeded")
                    GENERATIONS.labels(job.model_key, "succeeded").inc()
                    if job.started_at:
                        GENERATION_SECONDS.labels(job.model_key).observe(
                            (now() - job.started_at).total_seconds()
                        )
                saved = True
            except BaseException:
                for media in downloaded:
                    # Remove only uncommitted files; committed outputs are managed by retention.
                    if not saved:
                        rt.media.path(media.path).unlink(missing_ok=True)
                raise
        else:
            async with rt.sessions.begin() as s:
                job = await s.get(Job, job_id, with_for_update=True)
                if job:
                    await finish(s, job, "succeeded")
    else:
        async with rt.sessions.begin() as s:
            job = await s.get(Job, job_id, with_for_update=True)
            if job:
                await finish(
                    s,
                    job,
                    str(state),
                    "Генерация отменена."
                    if state == "canceled"
                    else "Генерация завершилась ошибкой. Резерв возвращён.",
                )
                GENERATIONS.labels(job.model_key, state).inc()

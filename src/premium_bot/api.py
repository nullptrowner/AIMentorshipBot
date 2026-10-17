import hmac
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import extract, func, select, text
from sqlalchemy.dialects.postgresql import insert

from premium_bot.application.common import enqueue
from premium_bot.application.payments import credit_crypto
from premium_bot.db import Invoice, Job, JobEvent, Media, Task
from premium_bot.domain import TERMINAL, BusinessError, Json, now
from premium_bot.infrastructure.security import (
    prediction_binding,
    verify_crypto,
    verify_media,
    verify_replicate,
)
from premium_bot.runtime import Runtime


async def body_limited(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1_000_000:
            raise HTTPException(413)
    return bytes(body)


def create_app(rt: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await rt.close()

    app = FastAPI(
        title="Team AI Studio", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )

    @app.get("/healthz")
    async def health() -> Json:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready() -> Json:
        try:
            async with rt.sessions() as s:
                await s.execute(text("SELECT 1"))
                await s.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
            await rt.redis.ping()
            heartbeat = await rt.redis.get("worker:heartbeat")
            if not heartbeat or now().timestamp() - float(heartbeat) > 90:
                raise ValueError("Worker heartbeat expired")
        except Exception as exc:
            raise HTTPException(503, "Dependencies unavailable") from exc
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics() -> Response:
        from premium_bot.observability import JOB_COUNTS, JOB_LATENCY, QUEUE_COUNTS

        async with rt.sessions() as s:
            rows = (
                await s.execute(
                    select(Job.model_key, Job.status, func.count()).group_by(
                        Job.model_key, Job.status
                    )
                )
            ).all()
            JOB_COUNTS.clear()
            for model, status, count in rows:
                JOB_COUNTS.labels(model, status).set(count)
            pending = await s.scalar(
                select(func.count()).select_from(Task).where(Task.done.is_(False))
            )
            errors = await s.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.done.is_(True), Task.last_error.is_not(None))
            )
            QUEUE_COUNTS.labels("pending").set(pending or 0)
            QUEUE_COUNTS.labels("failed").set(errors or 0)
            durations = (
                await s.execute(
                    select(
                        Job.model_key, func.avg(extract("epoch", Job.finished_at - Job.started_at))
                    )
                    .where(Job.finished_at.is_not(None), Job.started_at.is_not(None))
                    .group_by(Job.model_key)
                )
            ).all()
            for model_key, mean_value in durations:
                JOB_LATENCY.labels(str(model_key)).set(float(str(mean_value or 0)))
        return Response(generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

    @app.post("/webhooks/telegram")
    async def telegram(request: Request) -> Json:
        expected = rt.cfg.telegram_secret.get_secret_value()
        if not expected or not hmac.compare_digest(
            request.headers.get("x-telegram-bot-api-secret-token", ""), expected
        ):
            raise HTTPException(403)
        raw = await body_limited(request)
        try:
            payload = json.loads(raw)
            update_id = int(payload["update_id"])
            # Pre-checkout queries require a response within 10 seconds, so bypass the work queue.
            if payload.get("pre_checkout_query"):
                from aiogram.types import PreCheckoutQuery

                from premium_bot.application.payments import verify_stars_checkout

                item = PreCheckoutQuery.model_validate(payload["pre_checkout_query"])
                try:
                    async with rt.sessions.begin() as s:
                        await verify_stars_checkout(
                            s,
                            item.from_user.id,
                            item.invoice_payload,
                            item.currency,
                            item.total_amount,
                            item.id,
                        )
                    await rt.bot.answer_pre_checkout_query(item.id, ok=True)
                except (BusinessError, ValueError) as exc:
                    await rt.bot.answer_pre_checkout_query(
                        item.id, ok=False, error_message=str(exc)[:200]
                    )
                return {"ok": True}
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(400) from exc
        async with rt.sessions.begin() as s:
            await enqueue(s, f"telegram:{update_id}", "telegram", payload)
        return {"ok": True}

    @app.post("/webhooks/replicate/{job_id}")
    async def replicate(job_id: uuid.UUID, request: Request) -> Json:
        expected_binding = prediction_binding(str(job_id), rt.cfg.media_secret.get_secret_value())
        if not rt.cfg.media_secret.get_secret_value() or not hmac.compare_digest(
            request.query_params.get("binding", ""), expected_binding
        ):
            raise HTTPException(403)
        raw = await body_limited(request)
        if not verify_replicate(
            raw, dict(request.headers), rt.cfg.replicate_webhook_secret.get_secret_value()
        ):
            raise HTTPException(403)
        try:
            payload = json.loads(raw)
            prediction_id = str(payload["id"])
            event_id = request.headers["webhook-id"]
        except (ValueError, KeyError) as exc:
            raise HTTPException(400) from exc
        async with rt.sessions.begin() as s:
            job = await s.get(Job, job_id, with_for_update=True)
            if job is None or not job.submit_started:
                raise HTTPException(404)
            if job.prediction_id and job.prediction_id != prediction_id:
                raise HTTPException(409)
            if payload.get("version") != job.snapshot["version"]:
                raise HTTPException(409)
            if payload.get("input", {}).get("prompt") != job.snapshot["final_prompt"]:
                raise HTTPException(409)
            if job.prediction_id is None:
                job.prediction_id = prediction_id
            await s.execute(
                insert(JobEvent)
                .values(
                    job_id=job_id,
                    key="webhook:" + event_id,
                    kind="webhook",
                    payload={"status": payload.get("status"), "prediction_id": prediction_id},
                )
                .on_conflict_do_nothing(index_elements=[JobEvent.key])
            )
            # Webhook is a signal. Always reconcile authoritative provider state via authenticated GET.
            if job.status not in TERMINAL:
                await enqueue(s, "webhook-sync:" + event_id, "sync", {"job_id": str(job_id)})
        return {"ok": True}

    @app.post("/webhooks/crypto")
    async def crypto(request: Request) -> Json:
        raw = await body_limited(request)
        if not verify_crypto(
            raw,
            request.headers.get("crypto-pay-api-signature", ""),
            rt.cfg.crypto_token.get_secret_value(),
        ):
            raise HTTPException(403)
        try:
            event = json.loads(raw)
            if event.get("update_type") != "invoice_paid":
                return {"ok": True}
            remote = await rt.crypto.get(str(event["payload"]["invoice_id"]))
            if remote is None:
                raise HTTPException(409)
            async with rt.sessions.begin() as s:
                invoice_id = uuid.UUID(remote["payload"])
                item = await s.get(Invoice, invoice_id, with_for_update=True)
                if (
                    item
                    and item.provider == "crypto"
                    and item.provider_id is None
                    and item.status == "creating_unknown"
                ):
                    item.provider_id = str(remote["invoice_id"])
                await credit_crypto(s, remote)
        except (ValueError, KeyError, BusinessError) as exc:
            raise HTTPException(400, "Invalid payment") from exc
        return {"ok": True}

    @app.get("/media/{file_id}")
    async def media(file_id: uuid.UUID, expires: int, signature: str) -> FileResponse:
        if not verify_media(
            str(file_id), expires, signature, rt.cfg.media_secret.get_secret_value()
        ):
            raise HTTPException(403)
        async with rt.sessions() as s:
            item = await s.get(Media, file_id)
            if item is None or item.deleted or item.expires_at < now():
                raise HTTPException(404)
            active_job = await s.scalar(
                select(Job.id)
                .where(
                    Job.status.in_(["queued", "processing"]), Job.input_ids.contains([str(file_id)])
                )
                .limit(1)
            )
            if active_job is None:
                raise HTTPException(404)
        return FileResponse(
            rt.media.path(item.path),
            media_type=item.mime,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    return app

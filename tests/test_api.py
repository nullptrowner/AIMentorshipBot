import base64
import hmac
import json
import time

import httpx
import pytest
from sqlalchemy import func, select

from premium_bot.api import create_app
from premium_bot.application import jobs
from premium_bot.db import Job, JobEvent, Task

pytestmark = pytest.mark.integration


async def test_telegram_secret_and_duplicate_updates(rt):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(rt)), base_url="https://test"
    ) as client:
        assert (await client.post("/webhooks/telegram", json={"update_id": 123})).status_code == 403
        for _ in range(3):
            response = await client.post(
                "/webhooks/telegram",
                json={"update_id": 123},
                headers={"x-telegram-bot-api-secret-token": "t" * 32},
            )
            assert response.status_code == 200
        assert (
            await client.post(
                "/webhooks/telegram",
                content=b"x" * 1_000_001,
                headers={"x-telegram-bot-api-secret-token": "t" * 32},
            )
        ).status_code == 413
    async with rt.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Task)) == 1


async def test_signed_replicate_webhook_can_recover_lost_create_response(rt):
    from premium_bot.infrastructure.security import prediction_binding

    async with rt.sessions.begin() as s:
        job = await jobs.quote(s, rt.cfg, 2, "z_image", "portrait", {}, [])
        await jobs.confirm(s, rt.cfg, 2, job.id, "wallet")
        job.submit_started = True
    body = json.dumps(
        {
            "id": "prediction",
            "version": "test-version",
            "input": {"prompt": "portrait"},
            "status": "succeeded",
        }
    ).encode()
    stamp = str(int(time.time()))
    signature = base64.b64encode(
        hmac.digest(b"test-secret", b"event." + stamp.encode() + b"." + body, "sha256")
    ).decode()
    headers = {
        "webhook-id": "event",
        "webhook-timestamp": stamp,
        "webhook-signature": "v1," + signature,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(rt)), base_url="https://test"
    ) as client:
        for _ in range(3):
            response = await client.post(
                f"/webhooks/replicate/{job.id}?binding={prediction_binding(str(job.id), rt.cfg.media_secret.get_secret_value())}",
                content=body,
                headers=headers,
            )
            assert response.status_code == 200
        assert (
            await client.post(
                f"/webhooks/replicate/{job.id}?binding={prediction_binding(str(job.id), rt.cfg.media_secret.get_secret_value())}",
                content=body + b" ",
                headers=headers,
            )
        ).status_code == 403
    async with rt.sessions() as s:
        stored = await s.get(Job, job.id)
        assert stored.prediction_id == "prediction"
        assert stored.status == "queued"  # webhook is a signal, not a trusted finalization shortcut
        assert (
            await s.scalar(
                select(func.count()).select_from(JobEvent).where(JobEvent.kind == "webhook")
            )
            == 1
        )


async def test_health_readiness_and_unsigned_private_media(rt):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(rt)), base_url="https://test"
    ) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 503
        await rt.redis.set("worker:heartbeat", str(time.time()), ex=90)
        assert (await client.get("/readyz")).status_code == 200
        assert (
            await client.get(
                "/media/00000000-0000-0000-0000-000000000000?expires=1&signature=invalid"
            )
        ).status_code == 403

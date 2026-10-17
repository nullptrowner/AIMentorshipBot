import io
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from premium_bot.application import jobs
from premium_bot.db import Job, Media, Wallet
from premium_bot.workers.generation import submit, synchronize

pytestmark = pytest.mark.integration


async def test_full_generation_submit_poll_save_and_capture(rt, monkeypatch):
    import premium_bot.infrastructure.media as module

    monkeypatch.setattr(module, "resolve_public", lambda host, port: None)
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buffer, format="PNG")
    calls = []

    async def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.host == "replicate.delivery":
            return httpx.Response(200, content=buffer.getvalue())
        if request.method == "POST":
            return httpx.Response(201, json={"id": "prediction-1", "status": "starting"})
        return httpx.Response(
            200,
            json={
                "id": "prediction-1",
                "status": "succeeded",
                "output": "https://replicate.delivery/result.png",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rt.replicate.token = "test"
        rt.replicate.client = client
        rt.media.client = client
        async with rt.sessions.begin() as s:
            job = await jobs.quote(s, rt.cfg, 2, "z_image", "portrait", {}, [])
            await jobs.confirm(s, rt.cfg, 2, job.id, "wallet")
        await submit(rt, job.id)
        await submit(rt, job.id)
        await synchronize(rt, job.id)
        await synchronize(rt, job.id)
    assert sum(method == "POST" for method, path in calls) == 1
    async with rt.sessions() as s:
        stored = await s.get(Job, job.id)
        balance = await s.get(Wallet, 2)
        outputs = list(await s.scalars(select(Media).where(Media.job_id == job.id)))
        assert stored.status == "succeeded"
        assert balance.balance == 9 and balance.reserved == 0
        assert len(outputs) == 1 and rt.media.path(outputs[0].path).exists()


async def test_ambiguous_create_is_not_retried_or_refunded(rt):
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("test timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rt.replicate.token = "test"
        rt.replicate.client = client
        async with rt.sessions.begin() as s:
            job = await jobs.quote(s, rt.cfg, 2, "z_image", "portrait", {}, [])
            await jobs.confirm(s, rt.cfg, 2, job.id, "wallet")
        await submit(rt, job.id)
        await submit(rt, job.id)
    assert calls == 1
    async with rt.sessions() as s:
        stored = await s.get(Job, job.id)
        balance = await s.get(Wallet, 2)
        assert stored.submit_started
        assert stored.prediction_id is None
        assert balance.reserved == 1 and balance.balance == 10


async def test_provider_failure_releases_and_cancel_waits_for_confirmation(rt):
    rt.replicate.get = AsyncMock(return_value={"id": "p", "status": "canceled"})
    rt.replicate.cancel = AsyncMock(return_value={"id": "p", "status": "processing"})
    async with rt.sessions.begin() as s:
        job = await jobs.quote(s, rt.cfg, 2, "z_image", "portrait", {}, [])
        await jobs.confirm(s, rt.cfg, 2, job.id, "wallet")
        job.prediction_id, job.submit_started, job.status = "p", True, "processing"
    async with rt.sessions.begin() as s:
        await jobs.cancel(s, 2, job.id)
    async with rt.sessions() as s:
        assert (await s.get(Wallet, 2)).reserved == 1
    await synchronize(rt, job.id)
    async with rt.sessions() as s:
        assert (await s.get(Wallet, 2)).reserved == 0
        assert (await s.get(Job, job.id)).status == "canceled"

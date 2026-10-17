from decimal import Decimal

import pytest
from sqlalchemy import func, select

from premium_bot.application import admin, jobs
from premium_bot.application.admin_commands import apply_operation
from premium_bot.application.pricing import resolve_pricing
from premium_bot.db import AIModel, ModelRevision, Wallet
from premium_bot.domain import BusinessError
from premium_bot.workers.queue import sweep

pytestmark = pytest.mark.integration


async def test_admin_prompt_versions_and_restore(rt):
    async with rt.sessions.begin() as s:
        await admin.update_model(s, rt.cfg, 1, "z_image", "prompt", "system: {user_prompt}")
    async with rt.sessions.begin() as s:
        await admin.restore_revision(s, rt.cfg, 1, "z_image", 1)
    async with rt.sessions() as s:
        model = await s.get(AIModel, "z_image")
        assert model.prompt == "{user_prompt}"
        assert model.prompt_revision == 3
        assert await s.scalar(select(func.count()).select_from(ModelRevision)) == 2


async def test_admin_balance_adjustment_is_idempotent_and_requires_reason(rt):
    operation = {
        "action": "adjust",
        "target": "2",
        "value": {"amount": "3", "reason": "test grant"},
    }
    await apply_operation(rt, 1, operation, "operation-id")
    await apply_operation(rt, 1, operation, "operation-id")
    async with rt.sessions() as s:
        assert (await s.get(Wallet, 2)).balance == 13
    with pytest.raises(BusinessError):
        await apply_operation(rt, 2, operation, "unauthorized")


async def test_user_function_restrictions_and_concurrency_cap(rt):
    async with rt.sessions.begin() as s:
        await admin.record_user_change(s, rt.cfg, 1, 2, "restrictions", {"photo": False})
    with pytest.raises(BusinessError):
        async with rt.sessions.begin() as s:
            await jobs.quote(s, rt.cfg, 2, "z_image", "test", {}, [])
    async with rt.sessions.begin() as s:
        await admin.record_user_change(s, rt.cfg, 1, 2, "restrictions", {"max_active_jobs": 1})
        one = await jobs.quote(s, rt.cfg, 2, "z_image", "test", {}, [])
        two = await jobs.quote(s, rt.cfg, 2, "z_image", "test", {}, [])
        await jobs.confirm(s, rt.cfg, 2, one.id, "wallet")
    with pytest.raises(BusinessError):
        async with rt.sessions.begin() as s:
            await jobs.confirm(s, rt.cfg, 2, two.id, "wallet")


async def test_recovery_does_not_multiply_pending_poll_loops(rt):
    from premium_bot.db import Task

    async with rt.sessions.begin() as s:
        item = await jobs.quote(s, rt.cfg, 2, "z_image", "test", {}, [])
        await jobs.confirm(s, rt.cfg, 2, item.id, "wallet")
    await sweep(rt)
    await sweep(rt)
    async with rt.sessions() as s:
        count = await s.scalar(
            select(func.count()).select_from(Task).where(Task.kind.in_(["submit", "sync"]))
        )
        assert count == 1


async def test_resolution_pricing_variants(rt):
    raw = {
        "per_second": "0.1",
        "markup_percent": "20",
        "source": "test",
        "variants": {"resolution": {"720p": {"per_second": "0.2"}}},
    }
    formula = resolve_pricing(raw, {"resolution": "720p"}, None)
    assert formula.quote(Decimal(5), Decimal(0), Decimal(0)) == (
        Decimal("1.000000"),
        Decimal("1.200000"),
    )

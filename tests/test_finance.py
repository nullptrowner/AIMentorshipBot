import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from premium_bot.application import jobs, payments, subscriptions, wallet
from premium_bot.application.common import require_admin
from premium_bot.db import (
    AIModel,
    Invoice,
    Job,
    Ledger,
    Plan,
    Setting,
    Subscription,
    Task,
    Usage,
    Wallet,
)
from premium_bot.domain import BusinessError, now
from premium_bot.workers.queue import claim

pytestmark = pytest.mark.integration


async def make_job(rt, uid=2):
    async with rt.sessions.begin() as s:
        return await jobs.quote(s, rt.cfg, uid, "z_image", "portrait", {}, [])


async def test_registration_is_idempotent(rt):
    from premium_bot.application.common import register

    async with rt.sessions.begin() as s:
        await register(s, 2, "updated")
    async with rt.sessions() as s:
        item = await s.get(Wallet, 2)
        assert item.balance == Decimal(10)


async def test_awaiting_payment_is_not_submitted_before_explicit_confirmation(rt):
    from premium_bot.workers.queue import sweep

    item = await make_job(rt, uid=3)
    assert item.status == "awaiting_payment"
    await sweep(rt)
    async with rt.sessions() as s:
        assert (
            await s.scalar(select(func.count()).select_from(Task).where(Task.kind == "submit")) == 0
        )
    async with rt.sessions.begin() as s:
        await wallet.adjust(s, 3, Decimal(2), "fund-later", "test only")
        confirmed = await jobs.confirm(s, rt.cfg, 3, item.id, "wallet")
        assert confirmed.status == "queued"


async def test_parallel_spending_cannot_overdraw_wallet(rt):
    items = [await make_job(rt) for _ in range(20)]

    async def confirm(item):
        try:
            async with rt.sessions.begin() as s:
                await jobs.confirm(s, rt.cfg, 2, item.id, "wallet")
            return True
        except BusinessError:
            return False

    results = await asyncio.gather(*(confirm(item) for item in items))
    assert sum(results) == 10
    async with rt.sessions() as s:
        balance = await s.get(Wallet, 2)
        assert balance.balance == balance.reserved == Decimal(10)
        assert (
            await s.scalar(select(func.count()).select_from(Ledger).where(Ledger.kind == "reserve"))
            == 10
        )


async def test_repeated_confirmation_and_terminal_events_charge_once(rt):
    item = await make_job(rt)
    for _ in range(3):
        async with rt.sessions.begin() as s:
            await jobs.confirm(s, rt.cfg, 2, item.id, "wallet")
    for _ in range(3):
        async with rt.sessions.begin() as s:
            stored = await s.get(Job, item.id, with_for_update=True)
            await jobs.finish(s, stored, "succeeded")
    async with rt.sessions() as s:
        balance = await s.get(Wallet, 2)
        assert balance.balance == 9
        assert balance.reserved == 0
        assert (
            await s.scalar(
                select(func.count()).select_from(Ledger).where(Ledger.kind == "captured")
            )
            == 1
        )


@pytest.mark.parametrize("status", ["failed", "canceled", "expired"])
async def test_failed_jobs_release_reserves(rt, status):
    item = await make_job(rt)
    async with rt.sessions.begin() as s:
        await jobs.confirm(s, rt.cfg, 2, item.id, "wallet")
    async with rt.sessions.begin() as s:
        stored = await s.get(Job, item.id, with_for_update=True)
        await jobs.finish(s, stored, status)
    async with rt.sessions() as s:
        balance = await s.get(Wallet, 2)
        assert balance.balance == 10
        assert balance.reserved == 0


async def test_cancel_queued_job_never_reaches_provider(rt):
    item = await make_job(rt)
    async with rt.sessions.begin() as s:
        await jobs.confirm(s, rt.cfg, 2, item.id, "wallet")
    async with rt.sessions.begin() as s:
        result = await jobs.cancel(s, 2, item.id)
        assert result.status == "canceled"


async def test_payment_duplicate_and_parallel_webhooks_credit_once(rt):
    async with rt.sessions.begin() as s:
        item = Invoice(
            user_id=2,
            provider="stars",
            amount=Decimal(5),
            stars=100,
            status="active",
            expires_at=now() + timedelta(hours=1),
        )
        s.add(item)
        await s.flush()

    async def credit():
        async with rt.sessions.begin() as s:
            return await payments.credit(s, item.id, "stars", "stars:charge", Decimal(5), 2)

    assert sum(await asyncio.gather(*(credit() for _ in range(12)))) == 1
    async with rt.sessions() as s:
        balance = await s.get(Wallet, 2)
        assert balance.balance == 15


async def test_invoice_amount_and_payer_must_match(rt):
    async with rt.sessions.begin() as s:
        item = Invoice(
            user_id=2,
            provider="stars",
            amount=Decimal(5),
            stars=100,
            status="active",
            expires_at=now() + timedelta(hours=1),
        )
        s.add(item)
        await s.flush()
    for amount, payer in [(Decimal(6), 2), (Decimal(5), 3)]:
        with pytest.raises(BusinessError):
            async with rt.sessions.begin() as s:
                await payments.credit(s, item.id, "stars", "stars:bad", amount, payer)


async def test_provider_toggle_preserves_existing_invoice_and_checkout_is_single_use(rt):
    async with rt.sessions.begin() as s:
        s.add(Setting(key="payments", value={"stars_enabled": False}))
        item = Invoice(
            user_id=2,
            provider="stars",
            amount=Decimal(5),
            stars=100,
            status="active",
            expires_at=now() + timedelta(hours=1),
        )
        s.add(item)
        await s.flush()
    for _ in range(2):
        async with rt.sessions.begin() as s:
            await payments.verify_stars_checkout(s, 2, str(item.id), "XTR", 100, "same-query")
    with pytest.raises(BusinessError):
        async with rt.sessions.begin() as s:
            await payments.verify_stars_checkout(s, 2, str(item.id), "XTR", 100, "other-query")
    with pytest.raises(BusinessError):
        async with rt.sessions.begin() as s:
            await payments.create_invoice(s, rt.cfg, 2, "stars", Decimal(5))


async def test_subscription_quota_reserves_atomically_and_returns_on_failure(rt):
    async with rt.sessions.begin() as s:
        plan = Plan(
            name="Team",
            months=1,
            price=Decimal(3),
            limits={"z_image": [{"daily_count": 1, "max_seconds": 0}]},
        )
        s.add(plan)
        await s.flush()
        await subscriptions.buy(s, 2, plan.id, plan.price, "purchase")
    one, two = await make_job(rt), await make_job(rt)
    async with rt.sessions.begin() as s:
        await jobs.confirm(s, rt.cfg, 2, one.id, "subscription")
    with pytest.raises(BusinessError):
        async with rt.sessions.begin() as s:
            await jobs.confirm(s, rt.cfg, 2, two.id, "subscription")
    async with rt.sessions.begin() as s:
        stored = await s.get(Job, one.id, with_for_update=True)
        await jobs.finish(s, stored, "failed")
    async with rt.sessions.begin() as s:
        await jobs.confirm(s, rt.cfg, 2, two.id, "subscription")
    async with rt.sessions() as s:
        balance = await s.get(Wallet, 2)
        assert balance.balance == 7
        usage = await s.scalar(select(Usage))
        assert usage.held == 1 and usage.used == 0


async def test_expired_subscription_does_not_supply_quota(rt):
    async with rt.sessions.begin() as s:
        plan = Plan(
            name="Old",
            months=1,
            price=Decimal(3),
            limits={"z_image": [{"daily_count": 1, "max_seconds": 0}]},
        )
        s.add(plan)
        await s.flush()
        s.add(
            Subscription(
                user_id=2,
                plan_id=plan.id,
                limits=plan.limits,
                starts_at=now() - timedelta(days=40),
                expires_at=now() - timedelta(days=1),
            )
        )
    item = await make_job(rt)
    with pytest.raises(BusinessError):
        async with rt.sessions.begin() as s:
            await jobs.confirm(s, rt.cfg, 2, item.id, "subscription")


async def test_quote_snapshots_prompt_and_price(rt):
    item = await make_job(rt)
    async with rt.sessions.begin() as s:
        model = await s.get(AIModel, "z_image")
        model.prompt = "new instruction: {user_prompt}"
        model.pricing = {"fixed": "9", "markup_percent": "100", "source": "test"}
    async with rt.sessions.begin() as s:
        confirmed = await jobs.confirm(s, rt.cfg, 2, item.id, "wallet")
        assert confirmed.price == 1
        assert confirmed.snapshot["final_prompt"] == "portrait"


async def test_idor_and_admin_authorization(rt):
    item = await make_job(rt)
    with pytest.raises(BusinessError):
        async with rt.sessions.begin() as s:
            await jobs.confirm(s, rt.cfg, 3, item.id, "wallet")
    async with rt.sessions() as s:
        with pytest.raises(BusinessError):
            await require_admin(s, 2, rt.cfg)
        await require_admin(s, 1, rt.cfg)


async def test_maintenance_prevents_new_jobs_but_allows_completion(rt):
    item = await make_job(rt)
    async with rt.sessions.begin() as s:
        await jobs.confirm(s, rt.cfg, 2, item.id, "wallet")
        s.add(
            Setting(
                key="maintenance",
                value={"enabled": True, "block_generations": True, "message": "Работы"},
            )
        )
    with pytest.raises(BusinessError):
        await make_job(rt)
    async with rt.sessions.begin() as s:
        stored = await s.get(Job, item.id, with_for_update=True)
        assert await jobs.finish(s, stored, "succeeded")


async def test_queue_leases_recover_after_worker_crash(rt):
    async with rt.sessions.begin() as s:
        task = Task(key="recover", kind="message", payload={"user_id": 2, "text": "test"})
        s.add(task)
    first = await claim(rt)
    assert first is not None
    assert await claim(rt) is None
    async with rt.sessions.begin() as s:
        stored = await s.get(Task, first.id)
        stored.lease_until = now() - timedelta(seconds=1)
    second = await claim(rt)
    assert second.id == first.id
    assert second.lease_owner != first.lease_owner


async def test_ledger_is_immutable_in_postgresql(rt):
    with pytest.raises(DBAPIError):
        async with rt.sessions.begin() as s:
            await s.execute(text("UPDATE wallet_transactions SET amount = 999"))

import uuid
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from premium_bot.application.common import locked_wallet
from premium_bot.application.wallet import adjust
from premium_bot.db import Job, Plan, Subscription, Usage
from premium_bot.domain import BusinessError, PlanInput, money, now


async def active(s: AsyncSession, uid: int) -> Subscription | None:
    return await s.scalar(
        select(Subscription)
        .where(
            Subscription.user_id == uid,
            Subscription.canceled.is_(False),
            Subscription.starts_at <= now(),
            Subscription.expires_at > now(),
        )
        .order_by(Subscription.expires_at.desc())
        .limit(1)
    )


async def buy(
    s: AsyncSession, uid: int, plan_id: int, expected_price: Decimal, key: str, admin: bool = False
) -> Subscription:
    plan = await s.get(Plan, plan_id, with_for_update=True)
    await locked_wallet(s, uid)
    if plan is None or not plan.enabled or plan.price != expected_price:
        raise BusinessError("Условия подписки изменились. Откройте тариф заново.")
    if await active(s, uid):
        raise BusinessError("У вас уже есть подписка. Продлить её может администратор.")
    if not admin:
        changed = await adjust(
            s,
            uid,
            -plan.price,
            f"subscription:{key}",
            f"Подписка: {plan.name}",
            kind="subscription",
        )
        if not changed:
            raise BusinessError("Эта покупка уже обработана.")
    item = Subscription(
        user_id=uid,
        plan_id=plan.id,
        limits=plan.limits,
        starts_at=now(),
        expires_at=now() + relativedelta(months=plan.months),
    )
    s.add(item)
    await s.flush()
    return item


async def reserve_quota(s: AsyncSession, job: Job) -> bool:
    subscription = await active(s, job.user_id)
    if subscription is None:
        return False
    rules = sorted(subscription.limits.get(job.model_key, []), key=lambda r: r["max_seconds"])
    applicable = [r for r in rules if Decimal(r["max_seconds"]) >= job.seconds]
    if not applicable:
        return False
    rule = applicable[0]
    bucket = int(rule["max_seconds"])
    day = now().astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()
    usage = await s.get(Usage, (subscription.id, job.model_key, day, bucket), with_for_update=True)
    if usage is None:
        usage = Usage(
            subscription_id=subscription.id,
            model_key=job.model_key,
            day=day,
            bucket=bucket,
            held=0,
            used=0,
        )
        s.add(usage)
    if usage.held + usage.used >= int(rule["daily_count"]):
        return False
    usage.held += 1
    job.subscription_id = subscription.id
    job.quota_day = day
    job.quota_bucket = bucket
    return True


async def settle_quota(s: AsyncSession, job: Job, success: bool) -> None:
    usage = await s.get(
        Usage,
        (job.subscription_id, job.model_key, job.quota_day, job.quota_bucket),
        with_for_update=True,
    )
    if usage is None or usage.held <= 0:
        raise BusinessError("Несогласованность резерва подписки.")
    usage.held -= 1
    if success:
        usage.used += 1


async def save_plan(s: AsyncSession, data: PlanInput, plan_id: int | None = None) -> Plan:
    if plan_id is None:
        plan = Plan()
        s.add(plan)
    else:
        found = await s.get(Plan, plan_id, with_for_update=True)
        if found is None:
            raise BusinessError("Тариф не найден.")
        plan = found
    plan.name, plan.months, plan.price = data.name, data.months, money(data.price)
    plan.limits = {k: [r.model_dump() for r in rules] for k, rules in data.limits.items()}
    return plan


async def change_subscription(
    s: AsyncSession, subscription_id: uuid.UUID, days: int | None
) -> None:
    item = await s.get(Subscription, subscription_id, with_for_update=True)
    if item is None:
        raise BusinessError("Подписка не найдена.")
    if days is None:
        item.canceled = True
    elif not 1 <= days <= 366:
        raise BusinessError("Продление: от 1 до 366 дней.")
    else:
        item.expires_at = max(now(), item.expires_at) + timedelta(days=days)

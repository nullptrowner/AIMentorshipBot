import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from premium_bot.application.common import locked_wallet
from premium_bot.db import Ledger, Reservation
from premium_bot.domain import BusinessError, money


async def adjust(
    s: AsyncSession,
    uid: int,
    amount: Decimal,
    key: str,
    reason: str,
    actor: int | None = None,
    kind: str = "adjustment",
) -> bool:
    amount = money(amount)
    if not amount or not reason.strip():
        raise BusinessError("Укажите ненулевую сумму и причину.")
    wallet = await locked_wallet(s, uid)
    existing = await s.scalar(select(Ledger).where(Ledger.key == key))
    if existing:
        if existing.user_id != uid or existing.amount != amount:
            raise BusinessError("Конфликт идентификатора операции.")
        return False
    if wallet.balance + amount < wallet.reserved:
        raise BusinessError("Недостаточно доступных средств.")
    wallet.balance += amount
    s.add(
        Ledger(
            user_id=uid,
            amount=amount,
            reserve_delta=0,
            key=key,
            kind=kind,
            actor_id=actor,
            reason=reason,
        )
    )
    return True


async def reserve(s: AsyncSession, uid: int, job_id: uuid.UUID, amount: Decimal) -> None:
    wallet = await locked_wallet(s, uid)
    if wallet.balance - wallet.reserved < amount:
        raise BusinessError("Недостаточно средств. Пополните баланс.")
    wallet.reserved += amount
    s.add(Reservation(job_id=job_id, user_id=uid, amount=amount, status="held"))
    s.add(
        Ledger(
            user_id=uid,
            amount=0,
            reserve_delta=amount,
            key=f"reserve:{job_id}",
            kind="reserve",
            reason="Резервирование генерации",
        )
    )


async def settle(s: AsyncSession, job_id: uuid.UUID, success: bool) -> None:
    held = await s.get(Reservation, job_id, with_for_update=True)
    if held is None or held.status != "held":
        return
    wallet = await locked_wallet(s, held.user_id)
    wallet.reserved -= held.amount
    if success:
        wallet.balance -= held.amount
    held.status = "captured" if success else "released"
    s.add(
        Ledger(
            user_id=held.user_id,
            amount=-held.amount if success else 0,
            reserve_delta=-held.amount,
            key=f"settle:{job_id}",
            kind=held.status,
            reason="Завершение генерации",
        )
    )

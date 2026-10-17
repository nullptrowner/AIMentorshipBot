import uuid
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from premium_bot.application.common import allowed_user, enqueue, get_setting, maintenance, positive
from premium_bot.application.wallet import adjust
from premium_bot.config import Settings
from premium_bot.db import Invoice, PaymentEvent
from premium_bot.domain import BusinessError, Json, money, now


async def create_invoice(
    s: AsyncSession, cfg: Settings, uid: int, provider: str, amount: Decimal
) -> Invoice:
    await allowed_user(s, uid, cfg)
    await maintenance(s, uid, cfg, "payments")
    amount = money(amount)
    positive(amount)
    config = await get_setting(s, "payments")
    if provider not in {"stars", "crypto"} or not config.get(provider + "_enabled"):
        raise BusinessError("Этот способ оплаты отключён.")
    if provider == "crypto" and not cfg.crypto_sales_enabled:
        raise BusinessError("Crypto Pay недоступен для этого сценария продажи.")
    lower, upper = config.get("min_topup"), config.get("max_topup")
    if lower is None or upper is None:
        raise BusinessError("Администратор ещё не настроил пополнения.")
    if not Decimal(str(lower)) <= amount <= Decimal(str(upper)):
        raise BusinessError(f"Сумма пополнения: от ${lower} до ${upper}.")
    stars = None
    if provider == "stars":
        rate = Decimal(str(config.get("usd_per_star") or 0))
        if rate <= 0:
            raise BusinessError("Курс Stars ещё не настроен.")
        stars = int((amount / rate).to_integral_value(rounding=ROUND_CEILING))
        if stars > 10000:
            raise BusinessError("Слишком большая сумма для одного счёта Stars.")
    item = Invoice(
        user_id=uid,
        provider=provider,
        amount=amount,
        stars=stars,
        expires_at=now() + timedelta(hours=1),
    )
    s.add(item)
    await s.flush()
    return item


async def credit(
    s: AsyncSession,
    invoice_id: uuid.UUID,
    provider: str,
    event_key: str,
    amount: Decimal,
    payer_id: int | None = None,
) -> bool:
    invoice = await s.get(Invoice, invoice_id, with_for_update=True)
    if invoice is None or invoice.provider != provider or invoice.amount != money(amount):
        raise BusinessError("Платёж не соответствует счёту.")
    if payer_id is not None and invoice.user_id != payer_id:
        raise BusinessError("Счёт принадлежит другому пользователю.")
    if invoice.status in {"paid", "refunded"}:
        return False
    existing = await s.get(PaymentEvent, event_key)
    if existing:
        raise BusinessError("Платёжное событие уже относится к другому счёту.")
    await adjust(
        s,
        invoice.user_id,
        invoice.amount,
        f"invoice:{invoice.id}",
        "Пополнение баланса",
        kind="topup",
    )
    invoice.status, invoice.paid_at = "paid", now()
    s.add(PaymentEvent(key=event_key, invoice_id=invoice.id, amount=invoice.amount))
    await enqueue(
        s,
        f"paid:{invoice.id}",
        "message",
        {"user_id": invoice.user_id, "text": f"Баланс пополнен на ${invoice.amount:.6f}."},
    )
    return True


async def credit_crypto(s: AsyncSession, data: Json) -> bool:
    if (
        data.get("status") != "paid"
        or data.get("currency_type") != "fiat"
        or data.get("fiat") != "USD"
    ):
        raise BusinessError("Счёт Crypto Pay не подтверждён.")
    invoice_id = uuid.UUID(str(data["payload"]))
    invoice = await s.get(Invoice, invoice_id)
    if invoice is None or invoice.provider_id != str(data["invoice_id"]):
        raise BusinessError("Неизвестный счёт Crypto Pay.")
    return await credit(
        s, invoice_id, "crypto", f"crypto:{data['invoice_id']}", Decimal(str(data["amount"]))
    )


async def verify_stars_checkout(
    s: AsyncSession,
    uid: int,
    payload: str,
    currency: str,
    total: int,
    checkout_id: str | None = None,
) -> Invoice:
    invoice = await s.get(Invoice, uuid.UUID(payload), with_for_update=True)
    if (
        invoice is None
        or invoice.provider != "stars"
        or invoice.user_id != uid
        or invoice.stars != total
        or currency != "XTR"
        or (
            invoice.status != "active"
            and not (
                invoice.status == "checkout" and checkout_id and invoice.checkout_id == checkout_id
            )
        )
        or invoice.expires_at <= now()
    ):
        raise BusinessError("Счёт недействителен или уже оплачен.")
    # Existing invoices are honored when Stars is disabled for new sales.
    invoice.status = "checkout"
    invoice.checkout_id = checkout_id
    return invoice


async def invoice_history(s: AsyncSession, uid: int, page: int = 0) -> list[Invoice]:
    return list(
        await s.scalars(
            select(Invoice)
            .where(Invoice.user_id == uid)
            .order_by(Invoice.created_at.desc())
            .offset(page * 8)
            .limit(8)
        )
    )

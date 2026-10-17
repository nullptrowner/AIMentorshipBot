import uuid

import httpx
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    TransactionPartnerUser,
)

from premium_bot.application import payments
from premium_bot.application.common import enqueue
from premium_bot.db import Invoice
from premium_bot.domain import BusinessError, now
from premium_bot.runtime import Runtime
from premium_bot.workers.generation import alert


async def issue(rt: Runtime, invoice_id: uuid.UUID) -> None:
    async with rt.redis.lock(f"invoice:{invoice_id}", timeout=90, blocking_timeout=1):
        async with rt.sessions() as s:
            item = await s.get(Invoice, invoice_id)
            if item is None or item.status in {"paid", "expired", "checkout", "refunded"}:
                return
        if item.url:
            await send_link(rt, item)
            return
        if item.provider == "stars":
            url = await rt.bot.create_invoice_link(
                title="Баланс AI Студии",
                description=f"Пополнение внутреннего баланса на ${item.amount:.6f}",
                payload=str(item.id),
                currency="XTR",
                prices=[LabeledPrice(label="Пополнение", amount=item.stars or 0)],
            )
            async with rt.sessions.begin() as s:
                stored = await s.get(Invoice, item.id, with_for_update=True)
                if stored and stored.status == "creating":
                    stored.url, stored.status = url, "active"
                    item = stored
            await send_link(rt, item)
        else:
            if item.status == "creating_unknown":
                await alert(
                    rt,
                    "invoice-unknown:" + str(item.id),
                    f"Счёт {item.id}: неизвестен результат создания в Crypto Pay. Проверьте внешний сервис, повторный запрос не отправляется.",
                )
                return
            # Persist intent BEFORE the external call. A crash must never blindly create another invoice.
            async with rt.sessions.begin() as s:
                stored = await s.get(Invoice, item.id, with_for_update=True)
                if stored:
                    stored.status = "creating_unknown"
            try:
                remote = await rt.crypto.create(str(item.amount), str(item.id))
            except (httpx.RequestError, httpx.HTTPStatusError, BusinessError):
                await alert(
                    rt,
                    "invoice-unknown:" + str(item.id),
                    f"Счёт {item.id}: требуется сверка Crypto Pay после ошибки создания.",
                )
                return
            async with rt.sessions.begin() as s:
                stored = await s.get(Invoice, item.id, with_for_update=True)
                if stored:
                    stored.provider_id = str(remote["invoice_id"])
                    stored.url = str(remote["bot_invoice_url"])
                    if stored.status != "paid":
                        stored.status = "active"
                    item = stored
                    await enqueue(
                        s,
                        "invoice-poll:" + str(item.id),
                        "crypto_sync",
                        {"invoice_id": str(item.id)},
                        delay=30,
                    )
            await send_link(rt, item)


async def send_link(rt: Runtime, item: Invoice) -> None:
    if item.url:
        await rt.bot.send_message(
            item.user_id,
            f"<b>Пополнение баланса</b>\n\nБудет зачислено: ${item.amount:.6f}"
            + (
                f"\nК оплате: {item.stars} Stars"
                if item.provider == "stars"
                else "\nВалюта оплаты выбирается в Crypto Pay."
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Открыть счёт", url=item.url, style="primary")]
                ]
            ),
        )


async def sync_crypto(rt: Runtime, invoice_id: uuid.UUID) -> None:
    async with rt.sessions() as s:
        item = await s.get(Invoice, invoice_id)
        if item is None or item.status in {"paid", "refunded"} or not item.provider_id:
            return
    remote = await rt.crypto.get(item.provider_id)
    if remote and remote.get("status") == "paid":
        async with rt.sessions.begin() as s:
            await payments.credit_crypto(s, remote)
    elif remote and remote.get("status") == "expired" or item.expires_at < now():
        async with rt.sessions.begin() as s:
            stored = await s.get(Invoice, item.id, with_for_update=True)
            if stored and stored.status != "paid":
                stored.status = "expired"
    else:
        async with rt.sessions.begin() as s:
            await enqueue(
                s,
                "invoice-poll:" + uuid.uuid4().hex,
                "crypto_sync",
                {"invoice_id": str(item.id)},
                delay=60,
            )


async def reconcile_stars(rt: Runtime) -> int:
    from sqlalchemy import select

    async with rt.sessions() as s:
        if await s.scalar(select(Invoice.id).where(Invoice.provider == "stars").limit(1)) is None:
            return 0
    count = 0
    for offset in range(0, 10000, 100):
        batch = await rt.bot.get_star_transactions(offset=offset, limit=100)
        for transaction in batch.transactions:
            source = transaction.source
            if (
                not isinstance(source, TransactionPartnerUser)
                or not source.invoice_payload
                or source.transaction_type != "invoice_payment"
            ):
                continue
            try:
                invoice_id = uuid.UUID(source.invoice_payload)
            except ValueError:
                continue
            async with rt.sessions.begin() as s:
                item = await s.get(Invoice, invoice_id)
                if (
                    item
                    and item.provider == "stars"
                    and item.stars == transaction.amount
                    and item.user_id == source.user.id
                ):
                    changed = await payments.credit(
                        s, item.id, "stars", "stars:" + transaction.id, item.amount, source.user.id
                    )
                    count += int(changed)
        if len(batch.transactions) < 100:
            break
    return count

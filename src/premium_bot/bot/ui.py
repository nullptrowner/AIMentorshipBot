from datetime import datetime
from html import escape
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


def keyboard(*rows: list[tuple[str, str]], back: str | None = "home") -> InlineKeyboardMarkup:
    result = [
        [
            InlineKeyboardButton(
                text=title,
                callback_data=data,
                style="success"
                if data.startswith(("confirm:", "apply:", "buy:"))
                else "danger"
                if data.startswith(("cancel:", "delete:", "a:block"))
                else "primary"
                if data.startswith(("models:", "model:")) or data == "quote"
                else None,
            )
            for title, data in row
        ]
        for row in rows
    ]
    if back is not None:
        result.append(
            [
                InlineKeyboardButton(
                    text="← Назад" if back != "home" else "Главное меню", callback_data=back
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=result)


async def show(
    event: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    if len(text) > 4000:
        parser = _PlainText()
        parser.feed(text)
        text = escape("".join(parser.parts)[:3800] + "…")
    if isinstance(event, CallbackQuery):
        if isinstance(event.message, Message):
            try:
                await event.message.edit_text(text, reply_markup=markup)
            except TelegramBadRequest as exc:
                if "message is not modified" not in exc.message:
                    await event.message.answer(text, reply_markup=markup)
        await answer_callback(event)
    else:
        await event.answer(text, reply_markup=markup)


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


async def answer_callback(event: CallbackQuery, text: str = "", show_alert: bool = False) -> None:
    try:
        await event.answer(text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        if not any(
            marker in exc.message.lower()
            for marker in ("query is too old", "query id is invalid", "query_id_invalid")
        ):
            raise
        # Expiration of Telegram's visual ACK must not replay an already committed operation.
        if text and isinstance(event.message, Message):
            await event.message.answer(escape(text))


def local_time(value: datetime) -> datetime:
    return value.astimezone(ZoneInfo("Europe/Moscow"))


def safe(value: object) -> str:
    return escape(str(value))


def status_label(value: str) -> str:
    return {
        "created": "Ожидает подтверждения",
        "awaiting_payment": "Ожидает оплаты",
        "queued": "В очереди",
        "processing": "В работе",
        "succeeded": "Готово",
        "failed": "Ошибка",
        "canceled": "Отменено",
        "expired": "Истёк срок",
        "creating": "Создаётся",
        "active": "Ожидает оплаты",
        "checkout": "Оплата",
        "paid": "Оплачен",
        "refunded": "Возвращён",
        "creating_unknown": "На сверке",
    }.get(value, "На проверке")


def operation_label(value: str) -> str:
    return {
        "topup": "Пополнение",
        "adjustment": "Корректировка",
        "reserve": "Резерв",
        "captured": "Оплата генерации",
        "released": "Возврат резерва",
        "subscription": "Покупка подписки",
    }.get(value, "Операция")


def page_rows(prefix: str, page: int, full: bool) -> list[tuple[str, str]]:
    rows = []
    if page:
        rows.append(("←", f"{prefix}:{page - 1}"))
    if full:
        rows.append(("→", f"{prefix}:{page + 1}"))
    return rows

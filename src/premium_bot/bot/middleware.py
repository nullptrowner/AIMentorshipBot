import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, TelegramObject
from redis.exceptions import RedisError

from premium_bot.application.common import get_setting, is_admin, register
from premium_bot.bot.ui import answer_callback
from premium_bot.domain import BusinessError
from premium_bot.runtime import Runtime

logger = structlog.get_logger()


async def check_membership(rt: Runtime, uid: int) -> None:
    async with rt.sessions() as s:
        if await is_admin(s, uid, rt.cfg):
            return
        config = await get_setting(s, "access")
    ids = config.get("chat_ids", [])
    if not ids:
        raise BusinessError("Использование бота ограничено.")
    digest = hashlib.sha256(str(config).encode()).hexdigest()[:16]
    key = f"access:{uid}:{digest}"
    cached = await rt.redis.get(key)
    if cached == b"yes":
        return
    memberships = []
    for cid in ids:
        try:
            member = await rt.bot.get_chat_member(int(cid), uid)
            memberships.append(
                member.status in {"creator", "administrator", "member"}
                or member.status == "restricted"
                and bool(getattr(member, "is_member", False))
            )
        except Exception as exc:
            logger.warning("membership_check_failed", error_type=type(exc).__name__, user_id=uid)
            raise BusinessError("Сейчас не удалось проверить доступ. Попробуйте позже.") from exc
    if not (all(memberships) if config.get("mode") == "all" else any(memberships)):
        raise BusinessError("Использование бота ограничено.")
    await rt.redis.set(key, "yes", ex=15)


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        rt: Runtime = data["rt"]
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        uid = user.id
        # Financial receipts must be handled even after the user loses membership/access.
        receipt = isinstance(event, Message) and event.successful_payment is not None
        checkout = isinstance(event, PreCheckoutQuery)
        try:
            async with rt.sessions.begin() as s:
                await register(s, uid, user.username)
                admin = await is_admin(s, uid, rt.cfg)
                from premium_bot.db import User

                record = await s.get(User, uid)
                if record and record.blocked and not admin and not receipt:
                    raise BusinessError("Использование бота ограничено.")
                if (
                    isinstance(event, CallbackQuery)
                    and (event.data or "").startswith("a:")
                    and not admin
                ):
                    raise BusinessError("Недостаточно прав.")
            if not receipt and not checkout:
                count = await rt.redis.incr(f"rate:{uid}")
                if count == 1:
                    await rt.redis.expire(f"rate:{uid}", 10)
                if count > 20:
                    raise BusinessError("Слишком много действий. Подождите несколько секунд.")
                await check_membership(rt, uid)
            data["uid"], data["admin"] = uid, admin
            return await handler(event, data)
        except (BusinessError, ValueError, KeyError) as exc:
            text = (
                str(exc)
                if isinstance(exc, BusinessError)
                else "Проверьте формат введённых данных и попробуйте снова."
            )
            if isinstance(event, CallbackQuery):
                await answer_callback(event, text[:200], show_alert=True)
            elif isinstance(event, PreCheckoutQuery):
                await event.answer(ok=False, error_message=text[:200])
            elif isinstance(event, Message):
                await event.answer(text)
            return None
        except RedisError:
            raise

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from premium_bot.config import Settings
from premium_bot.db import Admin, Audit, Setting, Task, User, Wallet
from premium_bot.domain import BusinessError, Json, now

DEFAULTS: dict[str, Json] = {
    "access": {"chat_ids": [], "mode": "any"},
    "maintenance": {
        "enabled": False,
        "message": "Технические работы. Попробуйте позже.",
        "block_generations": True,
        "block_payments": True,
        "until": "",
    },
    "payments": {
        "stars_enabled": False,
        "crypto_enabled": False,
        "usd_per_star": None,
        "min_topup": None,
        "max_topup": None,
    },
    "support": {"text": "Обратитесь к администратору команды."},
    "branding": {"name": "AI Студия"},
    "pricing": {"markup_percent": None},
}


async def get_setting(s: AsyncSession, key: str) -> Json:
    item = await s.get(Setting, key)
    return dict(item.value) if item else dict(DEFAULTS.get(key, {}))


async def is_admin(s: AsyncSession, uid: int, cfg: Settings) -> bool:
    return uid in cfg.admin_ids or await s.get(Admin, uid) is not None


async def require_admin(s: AsyncSession, uid: int, cfg: Settings) -> None:
    if not await is_admin(s, uid, cfg):
        raise BusinessError("Недостаточно прав.")


async def register(s: AsyncSession, uid: int, username: str | None) -> None:
    await s.execute(
        insert(User)
        .values(id=uid, username=username, last_seen=now())
        .on_conflict_do_update(
            index_elements=[User.id], set_={"username": username, "last_seen": now()}
        )
    )
    await s.execute(
        insert(Wallet).values(user_id=uid, balance=0, reserved=0).on_conflict_do_nothing()
    )


async def audit(
    s: AsyncSession, actor: int, action: str, target: str, details: Json | None = None
) -> None:
    s.add(Audit(actor_id=actor, action=action, target=target, details=details or {}))


async def enqueue(s: AsyncSession, key: str, kind: str, payload: Json, delay: int = 0) -> None:
    await s.execute(
        insert(Task)
        .values(
            key=key,
            kind=kind,
            payload=payload,
            due_at=now() + timedelta(seconds=delay),
            attempts=0,
            done=False,
        )
        .on_conflict_do_nothing(index_elements=[Task.key])
    )


async def allowed_user(s: AsyncSession, uid: int, cfg: Settings) -> None:
    user = await s.get(User, uid)
    if user is None:
        raise BusinessError("Начните с /start.")
    if user.blocked and not await is_admin(s, uid, cfg):
        raise BusinessError("Использование бота ограничено.")


async def maintenance(s: AsyncSession, uid: int, cfg: Settings, operation: str) -> None:
    value = await get_setting(s, "maintenance")
    if (
        value.get("enabled")
        and value.get(f"block_{operation}", True)
        and not await is_admin(s, uid, cfg)
    ):
        raise BusinessError(str(value.get("message", "Технические работы.")))


async def locked_wallet(s: AsyncSession, uid: int) -> Wallet:
    wallet = await s.scalar(select(Wallet).where(Wallet.user_id == uid).with_for_update())
    if wallet is None:
        raise BusinessError("Кошелёк не найден.")
    return wallet


def positive(value: Decimal) -> None:
    if not value.is_finite() or value <= 0:
        raise BusinessError("Сумма должна быть положительной.")

from datetime import timedelta
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from premium_bot.application.common import audit, get_setting, require_admin
from premium_bot.application.pricing import validate_profile
from premium_bot.config import Settings
from premium_bot.db import (
    Admin,
    AIModel,
    Audit,
    Broadcast,
    Invoice,
    Job,
    Ledger,
    ModelRevision,
    Plan,
    Setting,
    Subscription,
    User,
    Wallet,
)
from premium_bot.domain import BusinessError, Json, now
from premium_bot.infrastructure.replicate import REGISTRY, Replicate, validate_input


class AccessConfig(BaseModel):
    model_config = {"extra": "forbid"}
    chat_ids: list[int] = Field(default_factory=list, max_length=30)
    mode: str = "any"

    @model_validator(mode="after")
    def mode_valid(self) -> "AccessConfig":
        if self.mode not in {"all", "any"}:
            raise ValueError("mode: all или any")
        return self


class PaymentConfig(BaseModel):
    model_config = {"extra": "forbid"}
    stars_enabled: bool = False
    crypto_enabled: bool = False
    usd_per_star: Decimal | None = Field(default=None, gt=0)
    min_topup: Decimal | None = Field(default=None, gt=0)
    max_topup: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def limits_valid(self) -> "PaymentConfig":
        if (
            self.min_topup is not None
            and self.max_topup is not None
            and self.min_topup > self.max_topup
        ):
            raise ValueError("Минимум превышает максимум")
        if self.stars_enabled and self.usd_per_star is None:
            raise ValueError("Сначала задайте usd_per_star")
        return self


class MaintenanceConfig(BaseModel):
    model_config = {"extra": "forbid"}
    enabled: bool = False
    message: str = Field(default="Технические работы.", max_length=1000)
    block_generations: bool = True
    block_payments: bool = True
    until: str = Field(default="", max_length=100)


async def save_setting(s: AsyncSession, cfg: Settings, actor: int, key: str, value: Json) -> None:
    await require_admin(s, actor, cfg)
    if key == "access":
        value = AccessConfig.model_validate(value).model_dump(mode="json")
    elif key == "payments":
        value = PaymentConfig.model_validate(value).model_dump(mode="json")
    elif key == "maintenance":
        value = MaintenanceConfig.model_validate(value).model_dump(mode="json")
    elif key in {"support", "branding"}:
        field = "text" if key == "support" else "name"
        if (
            set(value) != {field}
            or not isinstance(value[field], str)
            or not 1 <= len(value[field]) <= 1500
        ):
            raise BusinessError(f"Ожидается поле {field} с текстом.")
    elif key == "pricing":
        rate = Decimal(str(value.get("markup_percent")))
        if not rate.is_finite() or not 0 <= rate <= 10000:
            raise BusinessError("Наценка: от 0 до 10000 процентов.")
        value = {"markup_percent": str(rate)}
    else:
        raise BusinessError("Неизвестный раздел настроек.")
    old = await get_setting(s, key)
    await s.execute(
        insert(Setting)
        .values(key=key, value=value)
        .on_conflict_do_update(index_elements=[Setting.key], set_={"value": value})
    )
    await audit(s, actor, "setting", key, {"before": old, "after": value})


def check_model_ready(model: AIModel, global_markup: object = None) -> None:
    if not model.version or not model.api_schema:
        raise BusinessError("Сначала проверьте API модели.")
    pricing = dict(model.pricing)
    if pricing.get("markup_percent") is None:
        pricing["markup_percent"] = global_markup
    try:
        validate_profile(pricing, global_markup)
    except ValueError as exc:
        raise BusinessError("Сначала задайте себестоимость и наценку.") from exc
    adapter = REGISTRY[model.key]
    example = adapter.build(
        "test",
        model.defaults,
        ["https://example.com/input.png"] if adapter.info.max_images else [],
        "https://example.com/audio.wav" if adapter.info.audio else None,
        Decimal(5),
    )
    validate_input(model.api_schema, example)


async def update_model(
    s: AsyncSession, cfg: Settings, actor: int, key: str, field: str, value: object
) -> None:
    await require_admin(s, actor, cfg)
    model = await s.get(AIModel, key, with_for_update=True)
    if model is None:
        raise BusinessError("Модель не найдена.")
    before = {
        "prompt": model.prompt,
        "name": model.name,
        "defaults": model.defaults,
        "pricing": model.pricing,
        "enabled": model.enabled,
    }
    if field == "prompt":
        if not isinstance(value, str) or value.count("{user_prompt}") != 1 or len(value) > 20000:
            raise BusinessError(
                "Шаблон должен содержать ровно один {user_prompt}; максимум 20000 символов."
            )
        model.prompt = value
    elif field == "name" and isinstance(value, str) and 1 <= len(value) <= 100:
        model.name = value
    elif field == "defaults" and isinstance(value, dict):
        forbidden = {
            "prompt",
            "image",
            "input_images",
            "image_input",
            "audio",
            "reference_images",
            "reference_audios",
            "reference_videos",
            "last_frame_image",
        }
        if forbidden.intersection(value):
            raise BusinessError("Исходники и промпт нельзя задавать через параметры модели.")
        if any(
            k
            not in model.api_schema.get("components", {})
            .get("schemas", {})
            .get("Input", {})
            .get("properties", {})
            for k in value
        ):
            raise BusinessError("Параметр отсутствует в подтверждённой API-схеме.")
        model.defaults = value
    elif field == "pricing" and isinstance(value, dict):
        check = dict(value)
        if check.get("markup_percent") is None:
            check["markup_percent"] = (await get_setting(s, "pricing")).get("markup_percent")
        validate_profile(check, (await get_setting(s, "pricing")).get("markup_percent"))
        model.pricing = value
    elif field == "enabled" and isinstance(value, bool):
        model.enabled = value
    else:
        raise BusinessError("Некорректное поле модели.")
    if model.enabled:
        check_model_ready(model, (await get_setting(s, "pricing")).get("markup_percent"))
    s.add(
        ModelRevision(
            model_key=key, revision=model.prompt_revision, snapshot=before, actor_id=actor
        )
    )
    model.prompt_revision += 1
    await audit(s, actor, "model." + field, key, {"revision": model.prompt_revision})


async def verify_model(
    s: AsyncSession, cfg: Settings, actor: int, key: str, provider: Replicate
) -> None:
    await require_admin(s, actor, cfg)
    model = await s.get(AIModel, key, with_for_update=True)
    if model is None:
        raise BusinessError("Модель не найдена.")
    version, schema = await provider.schema(model.slug)
    model.version, model.api_schema, model.verified_at = version, schema, now()
    model.enabled = False
    await audit(s, actor, "model.verify", key, {"version": version})


async def restore_revision(
    s: AsyncSession, cfg: Settings, actor: int, key: str, revision: int
) -> None:
    await require_admin(s, actor, cfg)
    old = await s.scalar(
        select(ModelRevision).where(
            ModelRevision.model_key == key, ModelRevision.revision == revision
        )
    )
    if old is None:
        raise BusinessError("Версия не найдена.")
    await update_model(s, cfg, actor, key, "prompt", old.snapshot["prompt"])


async def dashboard(s: AsyncSession) -> str:
    total = await s.scalar(select(func.count()).select_from(User))
    jobs = (await s.execute(select(Job.status, func.count()).group_by(Job.status))).all()
    payments = await s.scalar(
        select(func.coalesce(func.sum(Invoice.amount), 0)).where(Invoice.status == "paid")
    )
    revenue = await s.scalar(
        select(func.coalesce(-func.sum(Ledger.amount), 0)).where(
            Ledger.kind.in_(["captured", "subscription"])
        )
    )
    costs = await s.scalar(select(func.coalesce(func.sum(Job.actual_cost), 0)))
    estimates = await s.scalar(
        select(func.coalesce(func.sum(Job.estimated_cost), 0)).where(
            Job.submit_started.is_(True), Job.actual_cost.is_(None)
        )
    )
    liabilities = await s.scalar(select(func.coalesce(func.sum(Wallet.balance), 0)))
    active = await s.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.expires_at > now(), Subscription.canceled.is_(False))
    )
    payer_count = await s.scalar(
        select(func.count(func.distinct(Invoice.user_id))).where(Invoice.status == "paid")
    )
    revenue, costs, estimates = revenue or Decimal(0), costs or Decimal(0), estimates or Decimal(0)
    text = f"<b>Управление студией</b>\n\nПользователей: {total}\nАктивных подписок: {active}\nПлативших: {payer_count}\n\nПоступления: ${payments}\nВыручка по операциям: ${revenue}\nПодтверждённые расходы: ${costs}\nОценочные расходы без подтверждения: ${estimates}\nРасчётный результат: ${revenue - costs - estimates}\nОстатки пользователей: ${liabilities}\n\n"
    new_counts = []
    for days in (1, 7, 30):
        new_counts.append(
            await s.scalar(
                select(func.count())
                .select_from(User)
                .where(User.created_at >= now() - timedelta(days=days))
            )
        )
    active_users = await s.scalar(
        select(func.count()).select_from(User).where(User.last_seen >= now() - timedelta(days=1))
    )
    conversion = Decimal(payer_count or 0) / Decimal(total or 1) * 100
    model_counts = (
        await s.execute(select(Job.model_key, func.count()).group_by(Job.model_key))
    ).all()
    return (
        text
        + "\n".join(f"{status}: {count}" for status, count in jobs)
        + f"\n\nНовые за сутки / 7 / 30 дней: {' / '.join(str(n) for n in new_counts)}\nАктивны за сутки: {active_users}\nКонверсия в оплату: {conversion:.1f}%\n\nПо моделям:\n"
        + "\n".join(f"{key}: {count}" for key, count in model_counts)
    )


async def user_lookup(s: AsyncSession, query: str) -> User | None:
    if query.lstrip("-").isdigit():
        return await s.get(User, int(query))
    return await s.scalar(
        select(User).where(func.lower(User.username) == query.lstrip("@").lower())
    )


async def seed(s: AsyncSession, cfg: Settings) -> None:
    for item in REGISTRY.values():
        info = item.info
        await s.execute(
            insert(AIModel)
            .values(
                key=info.key,
                slug=info.slug,
                name=info.name,
                kind=info.kind,
                tier=info.tier,
                enabled=False,
            )
            .on_conflict_do_nothing()
        )
    for uid in cfg.admin_ids:
        await s.execute(insert(Admin).values(user_id=uid).on_conflict_do_nothing())


EXPORTS = {
    "users": User,
    "payments": Invoice,
    "ledger": Ledger,
    "jobs": Job,
    "audit": Audit,
    "subscriptions": Subscription,
    "plans": Plan,
    "broadcasts": Broadcast,
}


async def record_user_change(
    s: AsyncSession, cfg: Settings, actor: int, uid: int, field: str, value: object
) -> None:
    await require_admin(s, actor, cfg)
    user = await s.get(User, uid, with_for_update=True)
    if user is None:
        raise BusinessError("Пользователь не найден.")
    if field == "blocked" and isinstance(value, bool):
        if uid in cfg.admin_ids or (await s.get(Admin, uid)) is not None:
            raise BusinessError("Администратора нельзя блокировать.")
        user.blocked = value
    elif field == "note" and isinstance(value, str) and len(value) <= 2000:
        user.note = value
    elif field == "restrictions" and isinstance(value, dict):
        if set(value) - {"photo", "video", "max_active_jobs"}:
            raise BusinessError("Разрешены photo, video, max_active_jobs.")
        if any(key in value and not isinstance(value[key], bool) for key in ("photo", "video")):
            raise BusinessError("photo и video должны быть true / false.")
        if "max_active_jobs" in value and (
            type(value["max_active_jobs"]) is not int or not 1 <= value["max_active_jobs"] <= 100
        ):
            raise BusinessError("max_active_jobs: целое число от 1 до 100.")
        user.restrictions = value
    else:
        raise BusinessError("Некорректное изменение пользователя.")
    await audit(s, actor, "user." + field, str(uid), {"value": value})

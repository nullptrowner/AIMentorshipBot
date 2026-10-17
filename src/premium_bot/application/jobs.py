import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from premium_bot.application import subscriptions, wallet
from premium_bot.application.common import (
    allowed_user,
    enqueue,
    get_setting,
    locked_wallet,
    maintenance,
)
from premium_bot.application.pricing import resolve_pricing
from premium_bot.config import Settings
from premium_bot.db import AIModel, Job, JobEvent, Media, User, Wallet
from premium_bot.domain import TERMINAL, BusinessError, Json, now
from premium_bot.infrastructure.replicate import REGISTRY, validate_input


def output_megapixels(params: Json, schema: Json) -> Decimal:
    props = schema["components"]["schemas"]["Input"].get("properties", {})
    values = {k: v.get("default") for k, v in props.items()}
    values.update(params)
    if (
        values.get("width")
        and values.get("height")
        and ("aspect_ratio" not in props or values.get("aspect_ratio") == "custom")
    ):
        return Decimal(str(values["width"])) * Decimal(str(values["height"])) / 1_000_000
    resolution = str(values.get("resolution", "1 MP"))
    if resolution.endswith(" MP"):
        return Decimal(resolution.removesuffix(" MP"))
    return {
        "512px": Decimal("0.262144"),
        "1K": Decimal("1.048576"),
        "2K": Decimal("4.194304"),
        "4K": Decimal("16.777216"),
    }.get(resolution, Decimal(0))


async def quote(
    s: AsyncSession,
    cfg: Settings,
    uid: int,
    model_key: str,
    prompt: str,
    params: Json,
    media_ids: list[str],
) -> Job:
    await allowed_user(s, uid, cfg)
    await maintenance(s, uid, cfg, "generations")
    model = await s.get(AIModel, model_key)
    if model is None or not model.enabled or not model.version or not model.api_schema:
        raise BusinessError("Модель пока недоступна.")
    user = await s.get(User, uid)
    if user is not None and user.restrictions.get(model.kind) is False:
        raise BusinessError("Эта функция ограничена администратором.")
    if not prompt.strip() or len(prompt) > 10000:
        raise BusinessError("Промпт должен содержать от 1 до 10000 символов.")
    if len(set(media_ids)) != len(media_ids):
        raise BusinessError("Один файл указан несколько раз.")
    items: list[Media] = []
    for value in media_ids:
        item = await s.get(Media, uuid.UUID(value))
        if item is None or item.user_id != uid or item.deleted or item.expires_at <= now():
            raise BusinessError("Исходный файл недоступен. Загрузите его повторно.")
        items.append(item)
    adapter = REGISTRY[model_key]
    images = [i for i in items if i.kind == "photo"]
    audios = [i for i in items if i.kind == "audio"]
    if (
        len(images) > adapter.info.max_images
        or len(audios) > 1
        or (audios and not adapter.info.audio)
    ):
        raise BusinessError("Количество или тип файлов не поддерживается моделью.")
    seconds = (
        audios[0].seconds
        if audios
        else Decimal(
            str(
                params.get(
                    "duration",
                    model.defaults.get(
                        "duration",
                        model.api_schema["components"]["schemas"]["Input"]
                        .get("properties", {})
                        .get("duration", {})
                        .get("default", 0),
                    ),
                )
            )
        )
    )
    if seconds < 0:
        raise BusinessError("Для фиксированной цены выберите явную длительность.")
    if model_key == "seedance":
        seconds = seconds.to_integral_value(rounding="ROUND_CEILING")
    if adapter.info.max_audio_seconds and seconds > adapter.info.max_audio_seconds:
        raise BusinessError(
            f"Аудио слишком длинное для модели: максимум {adapter.info.max_audio_seconds} секунд."
        )
    final_prompt = model.prompt.replace("{user_prompt}", prompt)
    combined = {**model.defaults, **params}
    data = adapter.build(
        final_prompt,
        combined,
        ["https://input.example/image.png"] * len(images),
        "https://input.example/audio.wav" if audios else None,
        seconds,
    )
    validate_input(model.api_schema, data)
    if model_key == "flux" and sum(i.megapixels for i in images) > 9:
        raise BusinessError("FLUX.2 Pro: суммарный размер референсов не должен превышать 9 МП.")
    pricing = dict(model.pricing)
    if pricing.get("markup_percent") is None:
        pricing["markup_percent"] = (await get_setting(s, "pricing")).get("markup_percent")
    try:
        effective = {
            k: v.get("default")
            for k, v in model.api_schema["components"]["schemas"]["Input"]
            .get("properties", {})
            .items()
        }
        effective.update(combined)
        formula = resolve_pricing(
            pricing, effective, (await get_setting(s, "pricing")).get("markup_percent")
        )
    except ValueError as exc:
        raise BusinessError("Администратор ещё не настроил цену модели.") from exc
    mp = output_megapixels(combined, model.api_schema) if model.kind == "photo" else Decimal(0)
    if (formula.per_second and not seconds) or (
        (formula.per_output_mp or formula.output_tiers or formula.first_output_mp_cost is not None)
        and not mp
    ):
        raise BusinessError("Для расчёта цены нужны явные длительность или разрешение.")
    input_mp = sum(
        (
            i.megapixels.to_integral_value(rounding="ROUND_CEILING")
            if formula.round_input_mp
            else i.megapixels
            for i in images
        ),
        Decimal(0),
    )
    estimate, price = formula.quote(seconds, mp, input_mp)
    if price <= 0:
        raise BusinessError(
            "Для этой комбинации исходников и параметров не настроена положительная цена."
        )
    balance = await s.get(Wallet, uid)
    awaiting_payment = (
        balance is not None
        and balance.balance - balance.reserved < price
        and await subscriptions.active(s, uid) is None
    )
    result = Job(
        status="awaiting_payment" if awaiting_payment else "created",
        user_id=uid,
        model_key=model_key,
        prompt=prompt,
        params=combined,
        input_ids=media_ids,
        seconds=seconds,
        estimated_cost=estimate,
        price=price,
        snapshot={
            "name": model.name,
            "kind": model.kind,
            "slug": model.slug,
            "version": model.version,
            "schema": model.api_schema,
            "template": model.prompt,
            "prompt_revision": model.prompt_revision,
            "final_prompt": final_prompt,
            "pricing": formula.model_dump(mode="json"),
        },
        expires_at=now() + timedelta(seconds=cfg.quote_ttl_seconds),
    )
    s.add(result)
    await s.flush()
    return result


async def owned_job(s: AsyncSession, uid: int, job_id: uuid.UUID, lock: bool = False) -> Job:
    job = await s.get(Job, job_id, with_for_update=lock)
    if job is None or job.user_id != uid:
        raise BusinessError("Задание не найдено.")
    return job


async def confirm(s: AsyncSession, cfg: Settings, uid: int, job_id: uuid.UUID, funding: str) -> Job:
    await allowed_user(s, uid, cfg)
    await maintenance(s, uid, cfg, "generations")
    job = await owned_job(s, uid, job_id, lock=True)
    if job.status not in {"created", "awaiting_payment"}:
        return job
    if job.expires_at <= now():
        raise BusinessError("Расчёт устарел. Создайте новый.")
    model = await s.get(AIModel, job.model_key)
    if model is None or not model.enabled:
        raise BusinessError("Модель отключена администратором.")
    await locked_wallet(s, uid)
    user = await s.get(User, uid)
    if user and user.restrictions.get(job.snapshot["kind"]) is False:
        raise BusinessError("Эта функция ограничена администратором.")
    cap = user.restrictions.get("max_active_jobs") if user else None
    if cap:
        count = await s.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.user_id == uid, Job.status.in_(["queued", "processing"]))
        )
        if (count or 0) >= cap:
            raise BusinessError("Достигнут ваш лимит одновременных генераций.")
    for mid in job.input_ids:
        media = await s.get(Media, uuid.UUID(mid), with_for_update=True)
        if media is None or media.deleted or media.expires_at <= now():
            raise BusinessError("Исходник истёк. Загрузите его заново.")
        media.expires_at = max(
            media.expires_at, now() + timedelta(seconds=cfg.job_timeout_seconds + 7200)
        )
    if funding == "subscription":
        if not await subscriptions.reserve_quota(s, job):
            raise BusinessError(
                "Подходящий лимит подписки отсутствует или исчерпан. Можно отдельно подтвердить оплату с баланса."
            )
    elif funding == "wallet":
        await wallet.reserve(s, uid, job.id, job.price)
    else:
        raise BusinessError("Неизвестный способ оплаты.")
    job.funding, job.status = funding, "queued"
    s.add(JobEvent(job_id=job.id, key=f"confirmed:{job.id}", kind="queued"))
    await enqueue(s, f"submit:{job.id}", "submit", {"job_id": str(job.id)})
    return job


async def finish(s: AsyncSession, job: Job, status: str, error: str | None = None) -> bool:
    if job.status in TERMINAL:
        return False
    if status not in TERMINAL:
        raise ValueError("Expected terminal status")
    success = status == "succeeded"
    if job.funding == "wallet":
        await wallet.settle(s, job.id, success)
    elif job.funding == "subscription":
        await subscriptions.settle_quota(s, job, success)
    job.status, job.finished_at, job.error = status, now(), error
    s.add(
        JobEvent(
            job_id=job.id,
            key=f"terminal:{job.id}",
            kind=status,
            payload={"error": error} if error else {},
        )
    )
    await enqueue(s, f"notify:{job.id}", "notify", {"job_id": str(job.id)})
    return True


async def cancel(s: AsyncSession, uid: int, job_id: uuid.UUID) -> Job:
    job = await owned_job(s, uid, job_id, lock=True)
    if job.status in TERMINAL:
        return job
    if (
        job.status in {"created", "awaiting_payment"}
        or job.status == "queued"
        and not job.submit_started
    ):
        await finish(s, job, "canceled")
    else:
        job.cancel_requested = True
        await enqueue(s, f"cancel:{job.id}", "sync", {"job_id": str(job.id)})
    return job

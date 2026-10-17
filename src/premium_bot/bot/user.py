import uuid
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from sqlalchemy import select

from premium_bot.application import jobs, payments, subscriptions
from premium_bot.application.common import get_setting
from premium_bot.bot.ui import (
    answer_callback,
    keyboard,
    local_time,
    operation_label,
    page_rows,
    safe,
    show,
    status_label,
)
from premium_bot.db import AIModel, Invoice, Job, Ledger, Media, Plan, User, Wallet
from premium_bot.domain import BusinessError
from premium_bot.infrastructure.replicate import REGISTRY
from premium_bot.runtime import Runtime

router = Router()


def format_limits(limits: dict) -> str:
    lines = []
    for key, rules in limits.items():
        adapter = REGISTRY.get(key)
        name = adapter.info.name if adapter else key
        for rule in rules:
            duration = f" · до {rule['max_seconds']} с" if rule["max_seconds"] else ""
            lines.append(f"{name}{duration}: {rule['daily_count']} в день")
    return "\n".join(lines)


class Studio(StatesGroup):
    images = State()
    audio = State()
    prompt = State()
    parameters = State()
    topup = State()
    trim = State()


@router.message(CommandStart())
@router.message(Command("menu", "cancel"))
@router.callback_query(F.data == "home")
async def home(
    event: Message | CallbackQuery, state: FSMContext, rt: Runtime, uid: int, admin: bool
) -> None:
    await state.clear()
    async with rt.sessions() as s:
        brand = await get_setting(s, "branding")
        wallet = await s.get(Wallet, uid)
    rows = [
        [("Создать фото", "models:photo"), ("Создать видео", "models:video")],
        [("История", "history:0"), ("Баланс", "balance")],
        [("Подписка", "plans"), ("Мой профиль", "profile")],
        [("Помощь", "help")],
    ]
    if admin:
        rows.append([("Управление", "a:home")])
    await show(
        event,
        f"<b>{safe(brand.get('name', 'AI Студия'))}</b>\n\nФото и видео для вашей команды.\nДоступно: <b>${wallet.balance - wallet.reserved:.6f}</b>"
        if wallet
        else "AI Студия",
        keyboard(*rows, back=None),
    )


@router.callback_query(F.data.startswith("models:"))
async def models(event: CallbackQuery, rt: Runtime, state: FSMContext) -> None:
    await state.clear()
    kind = (event.data or "").split(":")[1]
    async with rt.sessions() as s:
        models = list(
            await s.scalars(
                select(AIModel)
                .where(AIModel.enabled.is_(True), AIModel.kind == kind)
                .order_by(AIModel.key)
            )
        )
    rows = [[(f"{m.tier} · {m.name}", f"model:{m.key}")] for m in models]
    await show(
        event,
        "<b>Выберите модель</b>" if rows else "Модели ещё не настроены администратором.",
        keyboard(*rows),
    )


@router.callback_query(F.data.startswith("model:"))
async def choose_model(event: CallbackQuery, state: FSMContext, rt: Runtime) -> None:
    key = (event.data or "").split(":")[1]
    async with rt.sessions() as s:
        model = await s.get(AIModel, key)
        if not model or not model.enabled:
            raise BusinessError("Модель недоступна.")
    await state.set_data({"model": key, "media": [], "params": {}})
    if REGISTRY[key].info.max_images:
        await state.set_state(Studio.images)
        info = REGISTRY[key].info
        await show(
            event,
            f"<b>{safe(model.name)}</b>\n\nОтправьте фото как изображение или файл. Максимум: {info.max_images}.\nЗатем нажмите «Далее»."
            + (
                "\nАудио на следующем шаге."
                if info.audio
                else "\nДля генерации по тексту можно пропустить."
            ),
            keyboard([("Далее", "images:done")]),
        )
    else:
        await state.set_state(Studio.prompt)
        await show(
            event,
            f"<b>{safe(model.name)}</b>\n\nОпишите желаемое изображение. Эта модель работает по тексту.",
            keyboard(),
        )


@router.message(Studio.images, F.photo | F.document)
@router.message(Studio.audio, F.audio | F.voice | F.document)
async def upload(message: Message, state: FSMContext, rt: Runtime, uid: int) -> None:
    data = await state.get_data()
    info = REGISTRY[data["model"]].info
    kind = "photo" if await state.get_state() == Studio.images.state else "audio"
    item = (
        message.photo[-1] if message.photo else message.audio or message.voice or message.document
    )
    if item is None or (item.file_size or 0) > rt.cfg.max_upload_bytes:
        raise BusinessError("Файл слишком большой.")
    if kind == "photo" and len(data["media"]) >= info.max_images:
        raise BusinessError("Достигнут лимит фотографий.")
    name = uuid.uuid4().hex + ".upload"
    path = rt.media.path(name)
    persisted = False
    async with rt.redis.lock("media-processing", timeout=180, blocking_timeout=5):
        await rt.media.ensure_space(rt.cfg.max_upload_bytes)
        try:
            await rt.bot.download(item.file_id, destination=path, timeout=60)
            media = await rt.media.inspect(path, uid, kind)
            async with rt.sessions.begin() as s:
                s.add(media)
                await s.flush()
            persisted = True
            data["media"].append(str(media.id))
            await state.update_data(media=data["media"])
        finally:
            if not persisted or kind == "audio":
                path.unlink(missing_ok=True)
    if kind == "audio":
        await state.set_state(Studio.prompt)
        await message.answer(
            f"Аудио принято: {media.seconds:.1f} с.\nПри необходимости его можно обрезать перед запуском.\n\nОпишите желаемую сцену и движения."
            + (
                "\nДля OmniHuman используйте английский промпт: русский не указан в опубликованной схеме."
                if data["model"] == "omni"
                else ""
            ),
            reply_markup=keyboard(),
        )
    else:
        await message.answer(
            f"Фото принято · {len(data['media'])}/{info.max_images}",
            reply_markup=keyboard([("Далее", "images:done")]),
        )


@router.callback_query(Studio.images, F.data == "images:done")
async def images_done(event: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    info = REGISTRY[data["model"]].info
    if info.audio:
        if not data["media"]:
            raise BusinessError("Сначала отправьте фотографию.")
        await state.set_state(Studio.audio)
        suffix = f" Лимит модели: {info.max_audio_seconds} с." if info.max_audio_seconds else ""
        await show(
            event,
            "Отправьте аудиофайл или голосовое сообщение."
            + suffix
            + "\n\nИсходная речь используется как вход модели; точность синхронизации зависит от результата.",
            keyboard(),
        )
    else:
        await state.set_state(Studio.prompt)
        await show(event, "Опишите, что нужно создать или изменить на фото.", keyboard())


@router.message(Studio.prompt, F.text)
async def enter_prompt(message: Message, state: FSMContext, rt: Runtime) -> None:
    await state.update_data(prompt=message.text)
    await state.set_state(Studio.parameters)
    await parameters(message, state, rt)


async def parameters(event: Message | CallbackQuery, state: FSMContext, rt: Runtime) -> None:
    data = await state.get_data()
    async with rt.sessions() as s:
        model = await s.get(AIModel, data["model"])
        if not model:
            raise BusinessError("Модель недоступна.")
        props = (
            model.api_schema.get("components", {})
            .get("schemas", {})
            .get("Input", {})
            .get("properties", {})
        )
    rows = []
    if "width" in props and "height" in props:
        width = data["params"].get(
            "width", model.defaults.get("width", props["width"].get("default", 1024))
        )
        height = data["params"].get(
            "height", model.defaults.get("height", props["height"].get("default", 1024))
        )
        rows.append([(f"Размер: {width} × {height}", "dimensions")])
    if REGISTRY[model.key].info.audio:
        rows.append([("Обрезать длительность аудио", "trim")])
    rows.append([("Изменить промпт", "editprompt")])
    for field, title in [
        ("aspect_ratio", "Формат"),
        ("resolution", "Разрешение"),
        ("output_format", "Файл"),
        ("duration", "Длительность"),
    ]:
        if field in props and props[field].get("enum") and field != "duration":
            value = data["params"].get(
                field, model.defaults.get(field, props[field].get("default", "по умолчанию"))
            )
            rows.append([(f"{title}: {value}", f"param:{field}")])
    await show(
        event,
        f"<b>{safe(model.name)}</b>\n\n{safe(str(data.get('prompt', ''))[:1800])}\n\nПроверьте параметры и рассчитайте стоимость.",
        keyboard(*rows, [("Рассчитать", "quote")]),
    )


@router.callback_query(Studio.parameters, F.data == "editprompt")
async def edit_prompt(event: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Studio.prompt)
    await show(event, "Введите новый промпт. Загруженные исходники сохранятся.", keyboard())


@router.callback_query(Studio.parameters, F.data == "dimensions")
async def dimensions(event: CallbackQuery, state: FSMContext, rt: Runtime) -> None:
    data = await state.get_data()
    async with rt.sessions() as s:
        model = await s.get(AIModel, data["model"])
        if not model:
            raise BusinessError("Модель недоступна.")
        props = model.api_schema["components"]["schemas"]["Input"]["properties"]
    if "width" not in props or "height" not in props:
        raise BusinessError("Выбор размера недоступен.")
    candidates = [(512, 512), (768, 1024), (1024, 768), (1024, 1024), (1536, 1024)]
    allowed = [
        (w, h)
        for w, h in candidates
        if props["width"].get("minimum", 1) <= w <= props["width"].get("maximum", 4096)
        and props["height"].get("minimum", 1) <= h <= props["height"].get("maximum", 4096)
    ]
    current = (
        data["params"].get("width", model.defaults.get("width", 1024)),
        data["params"].get("height", model.defaults.get("height", 1024)),
    )
    if not allowed:
        raise BusinessError("Нет совместимых размеров.")
    index = allowed.index(current) if current in allowed else -1
    width, height = allowed[(index + 1) % len(allowed)]
    data["params"].update(width=width, height=height)
    if "aspect_ratio" in props and "custom" in props["aspect_ratio"].get("enum", []):
        data["params"]["aspect_ratio"] = "custom"
    await state.update_data(params=data["params"])
    await parameters(event, state, rt)


@router.callback_query(Studio.parameters, F.data == "trim")
async def trim_prompt(event: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Studio.trim)
    await show(
        event,
        "Введите длительность в секундах. Будет использовано начало аудио указанной длины; остальная часть речи в результат не войдёт.",
        keyboard(),
    )


@router.message(Studio.trim, F.text)
async def trim_audio(message: Message, state: FSMContext, rt: Runtime, uid: int) -> None:
    from premium_bot.infrastructure.media import run_media

    data = await state.get_data()
    seconds = Decimal((message.text or "").replace(",", "."))
    async with rt.sessions() as s:
        items = [await s.get(Media, uuid.UUID(mid)) for mid in data["media"]]
    audio = next(
        (
            i
            for i in items
            if i is not None and i.kind == "audio" and i.user_id == uid and not i.deleted
        ),
        None,
    )
    if audio is None or not seconds.is_finite() or not 0 < seconds < audio.seconds:
        raise BusinessError("Введите положительную длительность короче исходного аудио.")
    path = rt.media.path(uuid.uuid4().hex + ".wav")
    async with rt.redis.lock("media-processing", timeout=180, blocking_timeout=5):
        await rt.media.ensure_space(rt.cfg.max_upload_bytes)
        try:
            await run_media(
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(rt.media.path(audio.path)),
                "-t",
                str(seconds),
                "-c:a",
                "pcm_s16le",
                "-threads",
                "1",
                "-y",
                str(path),
            )
            item = await rt.media.inspect(path, uid, "audio")
            async with rt.sessions.begin() as s:
                s.add(item)
                await s.flush()
        finally:
            path.unlink(missing_ok=True)
    await state.update_data(
        media=[mid for mid in data["media"] if mid != str(audio.id)] + [str(item.id)]
    )
    await state.set_state(Studio.parameters)
    await parameters(message, state, rt)


@router.callback_query(Studio.parameters, F.data.startswith("param:"))
async def change_parameter(event: CallbackQuery, state: FSMContext, rt: Runtime) -> None:
    field = (event.data or "").split(":")[1]
    data = await state.get_data()
    async with rt.sessions() as s:
        model = await s.get(AIModel, data["model"])
        if not model or field not in {"aspect_ratio", "resolution", "output_format"}:
            raise BusinessError("Параметр недоступен.")
        prop = model.api_schema["components"]["schemas"]["Input"]["properties"][field]
        options = prop.get("enum", [])
        if not options:
            raise BusinessError("Параметр не поддерживает выбор.")
        current = data["params"].get(field, model.defaults.get(field, prop.get("default")))
        index = options.index(current) if current in options else -1
        data["params"][field] = options[(index + 1) % len(options)]
    await state.update_data(params=data["params"])
    await parameters(event, state, rt)


@router.callback_query(Studio.parameters, F.data == "quote")
async def quote(event: CallbackQuery, state: FSMContext, rt: Runtime, uid: int) -> None:
    data = await state.get_data()
    async with rt.sessions.begin() as s:
        job = await jobs.quote(
            s, rt.cfg, uid, data["model"], data["prompt"], data["params"], data["media"]
        )
    await show(
        event,
        f"<b>Подтверждение</b>\n\nМодель: {safe(job.snapshot['name'])}\n{'Длительность: ' + str(job.seconds) + ' с' if job.snapshot['kind'] == 'video' else ''}\nСтоимость с баланса: <b>${job.price:.6f}</b>\n\nЦена зафиксирована на {rt.cfg.quote_ttl_seconds // 60} мин. Можно использовать подходящий лимит подписки.",
        keyboard(
            [("Оплатить с баланса", f"confirm:w:{job.id.hex}")],
            [("Использовать подписку", f"confirm:s:{job.id.hex}")],
            [("Отменить", f"cancel:{job.id.hex}")],
        ),
    )


@router.callback_query(F.data.startswith("confirm:"))
async def confirm(event: CallbackQuery, state: FSMContext, rt: Runtime, uid: int) -> None:
    _, mode, value = (event.data or "").split(":")
    async with rt.sessions.begin() as s:
        job = await jobs.confirm(
            s, rt.cfg, uid, uuid.UUID(value), "subscription" if mode == "s" else "wallet"
        )
    await state.clear()
    await show(
        event,
        f"<b>Задание {str(job.id)[:8]}</b>\n\nСтатус: {safe(status_label(job.status))}\nРезультат придёт отдельным сообщением.",
        keyboard([("Обновить", f"job:{job.id.hex}")], [("Отменить", f"cancel:{job.id.hex}")]),
    )


@router.callback_query(F.data.startswith("cancel:"))
async def cancel(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    async with rt.sessions.begin() as s:
        job = await jobs.cancel(s, uid, uuid.UUID((event.data or "").split(":")[1]))
    await show(
        event,
        "Запрос отмены принят."
        if job.cancel_requested
        else "Статус: " + safe(status_label(job.status)),
        keyboard([("Задание", f"job:{job.id.hex}")]),
    )


@router.callback_query(F.data.startswith("history:"))
async def history(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    page = max(0, min(100000, int((event.data or "").split(":")[1])))
    async with rt.sessions() as s:
        items = list(
            await s.scalars(
                select(Job)
                .where(Job.user_id == uid)
                .order_by(Job.created_at.desc())
                .offset(page * 8)
                .limit(8)
            )
        )
    rows = [
        [
            (
                f"{local_time(item.created_at):%d.%m %H:%M} · {item.snapshot['name']} · {status_label(item.status)}",
                f"job:{item.id.hex}",
            )
        ]
        for item in items
    ]
    await show(
        event,
        "<b>История генераций</b>" if items else "История пуста.",
        keyboard(*rows, page_rows("history", page, len(items) == 8)),
    )


@router.callback_query(F.data.startswith("job:"))
async def job_details(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    async with rt.sessions() as s:
        job = await jobs.owned_job(s, uid, uuid.UUID((event.data or "").split(":")[1]))
    rows = [[("Повторить с настройками", f"repeat:{job.id.hex}")]]
    if job.status == "succeeded":
        rows.append([("Скачать результат", f"download:{job.id.hex}")])
        if job.snapshot["kind"] == "photo" and REGISTRY[job.model_key].info.max_images:
            rows.append([("Редактировать результат", f"variation:{job.id.hex}")])
    elif job.status in {"queued", "processing"}:
        rows.append([("Обновить", f"job:{job.id.hex}"), ("Отменить", f"cancel:{job.id.hex}")])
    await show(
        event,
        f"<b>{safe(job.snapshot['name'])}</b>\n\nСтатус: {safe(status_label(job.status))}\n{safe(job.error or '')}\n\n{safe(job.prompt[:2000])}",
        keyboard(*rows, back="history:0"),
    )


@router.callback_query(F.data.startswith("repeat:"))
async def repeat(event: CallbackQuery, state: FSMContext, rt: Runtime, uid: int) -> None:
    async with rt.sessions() as s:
        job = await jobs.owned_job(s, uid, uuid.UUID((event.data or "").split(":")[1]))
        available = []
        for mid in job.input_ids:
            item = await s.get(Media, uuid.UUID(mid))
            from premium_bot.domain import now

            if item and not item.deleted and item.expires_at > now():
                available.append(mid)
        if len(available) != len(job.input_ids):
            raise BusinessError(
                "Исходники уже удалены. Начните новую генерацию и загрузите их повторно."
            )
    await state.set_data(
        {"model": job.model_key, "media": available, "params": job.params, "prompt": job.prompt}
    )
    await state.set_state(Studio.parameters)
    await parameters(event, state, rt)


@router.callback_query(F.data.startswith("variation:"))
async def variation(event: CallbackQuery, state: FSMContext, rt: Runtime, uid: int) -> None:
    from premium_bot.domain import now

    async with rt.sessions() as s:
        job = await jobs.owned_job(s, uid, uuid.UUID((event.data or "").split(":")[1]))
        if not REGISTRY[job.model_key].info.max_images or job.snapshot["kind"] != "photo":
            raise BusinessError("Модель не поддерживает редактирование результата.")
        output = await s.scalar(
            select(Media)
            .where(
                Media.job_id == job.id,
                Media.output.is_(True),
                Media.deleted.is_(False),
                Media.expires_at > now(),
            )
            .limit(1)
        )
        if not output:
            raise BusinessError("Срок хранения результата истёк.")
    await state.set_data(
        {"model": job.model_key, "media": [str(output.id)], "params": {}, "prompt": job.prompt}
    )
    await state.set_state(Studio.prompt)
    await show(
        event, "Опишите изменения. Готовое изображение будет использовано как референс.", keyboard()
    )


@router.callback_query(F.data.startswith("download:"))
async def download(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    from aiogram.types import FSInputFile

    async with rt.sessions() as s:
        job = await jobs.owned_job(s, uid, uuid.UUID((event.data or "").split(":")[1]))
        items = list(
            await s.scalars(select(Media).where(Media.job_id == job.id, Media.output.is_(True)))
        )
    await answer_callback(event)
    for item in items:
        if item.deleted or not rt.media.path(item.path).exists():
            raise BusinessError("Срок хранения исходного результата истёк.")
        await rt.bot.send_document(
            uid,
            FSInputFile(
                rt.media.path(item.path),
                filename=f"{job.model_key}{'.mp4' if item.kind == 'video' else '.png' if item.mime == 'image/png' else '.webp' if item.mime == 'image/webp' else '.jpg'}",
            ),
        )


@router.callback_query(F.data == "balance")
async def balance(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    async with rt.sessions() as s:
        wallet = await s.get(Wallet, uid)
        config = await get_setting(s, "payments")
    rows = [[("История операций", "ledger:0")], [("Мои счета", "invoices:0")]]
    for provider, title in [("stars", "Telegram Stars"), ("crypto", "Crypto Pay")]:
        if config.get(provider + "_enabled") and (
            provider != "crypto" or rt.cfg.crypto_sales_enabled
        ):
            rows.append([("Пополнить · " + title, "topup:" + provider)])
    if wallet:
        await show(
            event,
            f"<b>Баланс</b>\n\nВсего: ${wallet.balance:.6f}\nРезерв: ${wallet.reserved:.6f}\nДоступно: <b>${wallet.balance - wallet.reserved:.6f}</b>",
            keyboard(*rows),
        )


@router.callback_query(F.data.startswith("ledger:"))
async def ledger(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    page = max(0, int((event.data or "").split(":")[1]))
    async with rt.sessions() as s:
        items = list(
            await s.scalars(
                select(Ledger)
                .where(Ledger.user_id == uid)
                .order_by(Ledger.id.desc())
                .offset(page * 8)
                .limit(8)
            )
        )
    text = "<b>Операции</b>\n\n" + "\n".join(
        f"{local_time(i.created_at):%d.%m} · {safe(operation_label(i.kind))} · ${i.amount:+.6f} · резерв {i.reserve_delta:+.6f}"
        for i in items
    )
    await show(event, text, keyboard(page_rows("ledger", page, len(items) == 8), back="balance"))


@router.callback_query(F.data.startswith("topup:"))
async def topup(event: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Studio.topup)
    await state.set_data({"provider": (event.data or "").split(":")[1]})
    await show(
        event,
        "Введите сумму пополнения в USD. Итоговая сумма оплаты будет показана в счёте.",
        keyboard(back="balance"),
    )


@router.message(Studio.topup, F.text)
async def create_topup(message: Message, state: FSMContext, rt: Runtime, uid: int) -> None:
    data = await state.get_data()
    try:
        amount = Decimal((message.text or "").replace(",", "."))
    except InvalidOperation as exc:
        raise BusinessError("Введите число, например 10.50.") from exc
    async with rt.sessions.begin() as s:
        invoice = await payments.create_invoice(s, rt.cfg, uid, data["provider"], amount)
        from premium_bot.application.common import enqueue

        await enqueue(s, f"invoice:{invoice.id}", "invoice", {"invoice_id": str(invoice.id)})
    await state.clear()
    await message.answer(
        "Счёт создаётся. Он придёт отдельным сообщением.",
        reply_markup=keyboard([("Мои счета", "invoices:0")]),
    )


@router.pre_checkout_query()
async def checkout(event: PreCheckoutQuery, rt: Runtime, uid: int) -> None:
    async with rt.sessions.begin() as s:
        await payments.verify_stars_checkout(
            s, uid, event.invoice_payload, event.currency, event.total_amount
        )
    await event.answer(ok=True)


@router.message(F.successful_payment)
async def successful(message: Message, rt: Runtime, uid: int) -> None:
    receipt = message.successful_payment
    if receipt is None:
        return
    async with rt.sessions.begin() as s:
        invoice = await s.get(Invoice, uuid.UUID(receipt.invoice_payload))
        if invoice is None or receipt.currency != "XTR" or invoice.stars != receipt.total_amount:
            raise BusinessError("Платёж требует сверки администратором.")
        await payments.credit(
            s,
            invoice.id,
            "stars",
            "stars:" + receipt.telegram_payment_charge_id,
            invoice.amount,
            uid,
        )


@router.callback_query(F.data.startswith("invoices:"))
async def invoices(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    page = max(0, int((event.data or "").split(":")[1]))
    async with rt.sessions() as s:
        items = await payments.invoice_history(s, uid, page)
    text = "<b>Счета</b>\n\n" + "\n".join(
        f"{local_time(i.created_at):%d.%m %H:%M} · ${i.amount:.6f} · {safe(status_label(i.status))}"
        for i in items
    )
    await show(event, text, keyboard(page_rows("invoices", page, len(items) == 8), back="balance"))


@router.callback_query(F.data == "plans")
async def plans(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    async with rt.sessions() as s:
        current = await subscriptions.active(s, uid)
        plans = list(
            await s.scalars(select(Plan).where(Plan.enabled.is_(True)).order_by(Plan.months))
        )
    text = "<b>Подписки</b>\n\n" + (
        f"Действует до {local_time(current.expires_at):%d.%m.%Y}.\nЛимиты:\n{safe(format_limits(current.limits))}\n\n"
        if current
        else "Активной подписки нет.\n\n"
    )
    await show(
        event,
        text,
        keyboard(
            *[[(f"{p.name} · {p.months} мес. · ${p.price:.2f}", f"plan:{p.id}")] for p in plans]
        ),
    )


@router.callback_query(F.data.startswith("plan:"))
async def plan_details(event: CallbackQuery, rt: Runtime, state: FSMContext) -> None:
    async with rt.sessions() as s:
        plan = await s.get(Plan, int((event.data or "").split(":")[1]))
        if not plan or not plan.enabled:
            raise BusinessError("Тариф недоступен.")
    token = uuid.uuid4().hex
    await state.update_data(
        purchase={
            "plan": plan.id,
            "price": str(plan.price),
            "limits": plan.limits,
            "months": plan.months,
            "token": token,
        }
    )
    await show(
        event,
        f"<b>{safe(plan.name)}</b>\n\nСрок: {plan.months} мес.\nЦена: ${plan.price:.6f}\nЛимиты в сутки по Москве:\n{safe(format_limits(plan.limits))}\n\nОстатки не переносятся. Продление через администратора.",
        keyboard([("Оплатить с баланса", "buy:" + token)], back="plans"),
    )


@router.callback_query(F.data.startswith("buy:"))
async def buy_plan(event: CallbackQuery, state: FSMContext, rt: Runtime, uid: int) -> None:
    data = (await state.get_data()).get("purchase", {})
    if data.get("token") != (event.data or "").split(":")[1]:
        raise BusinessError("Подтверждение устарело. Откройте тариф заново.")
    async with rt.sessions.begin() as s:
        plan = await s.get(Plan, data["plan"], with_for_update=True)
        if not plan or plan.limits != data["limits"] or plan.months != data["months"]:
            raise BusinessError("Условия изменились. Откройте тариф заново.")
        item = await subscriptions.buy(s, uid, data["plan"], Decimal(data["price"]), data["token"])
    await state.clear()
    await show(event, f"Подписка активна до {local_time(item.expires_at):%d.%m.%Y}.", keyboard())


@router.callback_query(F.data.in_({"profile", "notifications"}))
async def profile(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    async with rt.sessions.begin() as s:
        user = await s.get(User, uid, with_for_update=True)
        if not user:
            return
        if event.data == "notifications":
            user.notifications = not user.notifications
    await show(
        event,
        f"<b>Мой профиль</b>\n\nID: <code>{uid}</code>\nУведомления о рассылках: {'включены' if user.notifications else 'выключены'}\nСообщения о платежах и заданиях отправляются всегда.",
        keyboard([("Переключить рассылки", "notifications")]),
    )


@router.message(Command("help", "support", "paysupport"))
@router.callback_query(F.data == "help")
async def help_message(event: Message | CallbackQuery, rt: Runtime) -> None:
    async with rt.sessions() as s:
        support = await get_setting(s, "support")
    await show(
        event,
        "<b>Помощь</b>\n\n"
        + safe(support.get("text", ""))
        + "\n\n/start — главное меню\n/cancel — прервать ввод\n\nДля качественного видео используйте чёткое фото и аудио без фонового шума.",
        keyboard(),
    )

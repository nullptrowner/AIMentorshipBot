import csv
import io
import json
import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import select

from premium_bot.application import admin as service
from premium_bot.application import payments, subscriptions
from premium_bot.application.common import audit, enqueue, get_setting, require_admin
from premium_bot.bot.ui import answer_callback, keyboard, local_time, page_rows, safe, show
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
    Task,
    User,
    Wallet,
)
from premium_bot.domain import BusinessError, Json
from premium_bot.runtime import Runtime

router = Router()


class Editor(StatesGroup):
    value = State()
    confirmation = State()


async def check(rt: Runtime, uid: int) -> None:
    async with rt.sessions() as s:
        await require_admin(s, uid, rt.cfg)


@router.message(Command("admin"))
@router.callback_query(F.data == "a:home")
async def home(event: Message | CallbackQuery, state: FSMContext, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    await state.clear()
    async with rt.sessions() as s:
        text = await service.dashboard(s)
    await show(
        event,
        text,
        keyboard(
            [("Пользователи", "a:list:users:0"), ("Поиск", "a:edit:search")],
            [("Модели и промпты", "a:models"), ("Генерации", "a:list:jobs:0")],
            [("Платежи", "a:list:payments:0"), ("Подписки", "a:plans")],
            [("Доступ по чатам", "a:setting:access"), ("Рассылки", "a:broadcasts")],
            [("Настройки", "a:settings"), ("Журнал", "a:list:audit:0")],
            [("Отчёты CSV", "a:exports"), ("Администраторы", "a:admins")],
            [("Очередь и ошибки", "a:queue:0")],
        ),
    )


@router.callback_query(F.data.startswith("a:queue:"))
async def queue_screen(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    page = max(0, int((event.data or "").split(":")[2]))
    async with rt.sessions() as s:
        items = list(
            await s.scalars(
                select(Task)
                .where((Task.done.is_(False)) | (Task.last_error.is_not(None)))
                .order_by(Task.created_at.desc())
                .offset(page * 8)
                .limit(8)
            )
        )
    await show(
        event,
        "<b>Очередь и ошибки</b>",
        keyboard(
            *[
                [
                    (
                        f"{t.kind} · попыток {t.attempts} · {t.last_error or 'ожидает'}",
                        "a:task:" + t.id.hex,
                    )
                ]
                for t in items
            ],
            page_rows("a:queue", page, len(items) == 8),
            back="a:home",
        ),
    )


@router.callback_query(F.data.startswith("a:task:"))
async def task_screen(event: CallbackQuery, state: FSMContext, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    target = (event.data or "").split(":")[2]
    async with rt.sessions() as s:
        item = await s.get(Task, uuid.UUID(target))
        if not item:
            raise BusinessError("Задача не найдена.")
    if item.done and item.last_error:
        await preview(
            event,
            state,
            {"action": "retry_task", "target": target},
            f"Повторить задачу {item.kind} ({item.id}) после ошибки {item.last_error}? Платные генерации с сохранённым намерением запуска повторно не создаются.",
        )
    else:
        await show(
            event,
            f"Задача {item.id}\n{safe(item.kind)}\nПопыток: {item.attempts}\nВ очереди до: {item.due_at}\n{safe(item.last_error or '')}",
            keyboard(back="a:queue:0"),
        )


@router.callback_query(F.data.startswith("a:history:"))
async def user_history(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    _, _, target, entity, raw_page = (event.data or "").split(":")
    page = max(0, int(raw_page))
    entities: dict[str, type[Ledger] | type[Job] | type[Invoice]] = {
        "ledger": Ledger,
        "jobs": Job,
        "payments": Invoice,
    }
    selected = entities.get(entity)
    if not selected:
        raise BusinessError("Неизвестный раздел.")
    async with rt.sessions() as s:
        items = list(
            await s.scalars(
                select(selected)
                .where(selected.user_id == int(target))
                .order_by(selected.created_at.desc())
                .offset(page * 8)
                .limit(8)
            )
        )
    lines, rows = [], []
    for item in items:
        if isinstance(item, Ledger):
            lines.append(
                f"{local_time(item.created_at):%d.%m %H:%M} · {safe(item.kind)} · ${item.amount} · {safe(item.reason)}"
            )
        elif isinstance(item, Job):
            rows.append([(f"{item.model_key} · {item.status}", "a:job:" + item.id.hex)])
        elif isinstance(item, Invoice):
            rows.append([(f"${item.amount} · {item.status}", "a:invoice:" + item.id.hex)])
    await show(
        event,
        f"<b>История пользователя {target}</b>\n\n" + "\n".join(lines),
        keyboard(
            *rows,
            page_rows(f"a:history:{target}:{entity}", page, len(items) == 8),
            back="a:user:" + target,
        ),
    )


@router.callback_query(F.data.startswith("a:list:"))
async def listing(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    _, _, entity, page_text = (event.data or "").split(":")
    page = max(0, min(100000, int(page_text)))
    entity_types: dict[str, type[User] | type[Job] | type[Invoice] | type[Audit]] = {
        "users": User,
        "jobs": Job,
        "payments": Invoice,
        "audit": Audit,
    }
    entity_type = entity_types.get(entity)
    if not entity_type:
        raise BusinessError("Неизвестный список.")
    async with rt.sessions() as s:
        order = entity_type.id if entity == "audit" else entity_type.created_at
        items = list(
            await s.scalars(select(entity_type).order_by(order.desc()).offset(page * 8).limit(8))
        )
    rows = []
    texts = []
    for item in items:
        if isinstance(item, User):
            rows.append([(f"{item.id} · {item.username or 'без username'}", f"a:user:{item.id}")])
        elif isinstance(item, Job):
            rows.append(
                [(f"{str(item.id)[:8]} · {item.model_key} · {item.status}", f"a:job:{item.id.hex}")]
            )
        elif isinstance(item, Invoice):
            rows.append(
                [
                    (
                        f"${item.amount:.2f} · {item.provider} · {item.status}",
                        f"a:invoice:{item.id.hex}",
                    )
                ]
            )
        elif isinstance(item, Audit):
            texts.append(
                f"{local_time(item.created_at):%d.%m %H:%M} · {item.actor_id} · {safe(item.action)} · {safe(item.target)}"
            )
    await show(
        event,
        "<b>"
        + {
            "users": "Пользователи",
            "jobs": "Генерации",
            "payments": "Платежи",
            "audit": "Журнал действий",
        }[entity]
        + "</b>\n\n"
        + "\n".join(texts),
        keyboard(*rows, page_rows("a:list:" + entity, page, len(items) == 8), back="a:home"),
    )


@router.callback_query(F.data == "a:models")
async def models(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    async with rt.sessions() as s:
        items = list(await s.scalars(select(AIModel).order_by(AIModel.key)))
    await show(
        event,
        "<b>Модели</b>\n\nПеред включением проверьте API, задайте параметры и цену.",
        keyboard(
            *[[(f"{'●' if m.enabled else '○'} {m.name}", "a:model:" + m.key)] for m in items],
            back="a:home",
        ),
    )


@router.callback_query(F.data.startswith("a:model:"))
async def model(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    key = (event.data or "").split(":")[2]
    async with rt.sessions() as s:
        item = await s.get(AIModel, key)
        if item is None:
            raise BusinessError("Модель не найдена.")
    text = f"<b>{safe(item.name)}</b>\n\nВключена: {item.enabled}\nAPI проверен: {item.verified_at or 'нет'}\nВерсия настроек: {item.prompt_revision}\n\nШаблон:\n<pre>{safe(item.prompt[:1600])}</pre>\n\nЦена:\n<pre>{safe(json.dumps(item.pricing, ensure_ascii=False))}</pre>"
    await show(
        event,
        text,
        keyboard(
            [("Проверить API", "a:verify:" + key), ("Вкл / выкл", "a:toggle:" + key)],
            [("Доступные параметры API", "a:schema:" + key)],
            [("Системный промпт", "a:edit:prompt:" + key)],
            [("Цена и наценка", "a:edit:pricing:" + key), ("Параметры", "a:edit:defaults:" + key)],
            [("Название", "a:edit:name:" + key), ("Версии промпта", "a:revisions:" + key)],
            back="a:models",
        ),
    )


@router.callback_query(F.data.startswith("a:schema:"))
async def schema_screen(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    key = (event.data or "").split(":")[2]
    async with rt.sessions() as s:
        item = await s.get(AIModel, key)
        if not item or not item.api_schema:
            raise BusinessError("Сначала проверьте API.")
    properties = (
        item.api_schema.get("components", {})
        .get("schemas", {})
        .get("Input", {})
        .get("properties", {})
    )
    lines = []
    for field, details in properties.items():
        lines.append(
            field
            + ": "
            + str(details.get("enum", details.get("type", "см. схему")))
            + " · default="
            + str(details.get("default", "—"))
        )
    await show(
        event,
        "<b>Параметры API</b>\n\n<pre>" + safe("\n".join(lines)) + "</pre>",
        keyboard(back="a:model:" + key),
    )


@router.callback_query(F.data.startswith("a:verify:"))
async def verify(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await answer_callback(event, "Проверяю API…")
    async with rt.sessions.begin() as s:
        await service.verify_model(s, rt.cfg, uid, (event.data or "").split(":")[2], rt.replicate)
    if isinstance(event.message, Message):
        await event.message.answer(
            "Схема и версия получены. Модель выключена до проверки параметров и цены.",
            reply_markup=keyboard(back="a:models"),
        )


@router.callback_query(F.data.startswith("a:toggle:"))
async def toggle_model(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    key = (event.data or "").split(":")[2]
    async with rt.sessions.begin() as s:
        item = await s.get(AIModel, key)
        if not item:
            raise BusinessError("Модель не найдена.")
        await service.update_model(s, rt.cfg, uid, key, "enabled", not item.enabled)
    await show(event, "Доступность обновлена.", keyboard(back="a:model:" + key))


@router.callback_query(F.data.startswith("a:revisions:"))
async def revisions(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    key = (event.data or "").split(":")[2]
    async with rt.sessions() as s:
        items = list(
            await s.scalars(
                select(ModelRevision)
                .where(ModelRevision.model_key == key)
                .order_by(ModelRevision.revision.desc())
                .limit(8)
            )
        )
    await show(
        event,
        "Выберите версию для восстановления промпта.",
        keyboard(
            *[
                [
                    (
                        f"Версия {i.revision} · {local_time(i.created_at):%d.%m %H:%M}",
                        f"a:restore:{key}:{i.revision}",
                    )
                ]
                for i in items
            ],
            back="a:model:" + key,
        ),
    )


@router.callback_query(F.data.startswith("a:restore:"))
async def restore(event: CallbackQuery, state: FSMContext, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    _, _, key, revision = (event.data or "").split(":")
    await preview(
        event,
        state,
        {"action": "restore", "key": key, "revision": int(revision)},
        "Восстановить системный промпт из версии " + revision,
    )


@router.callback_query(F.data == "a:settings")
async def settings_menu(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    await show(
        event,
        "<b>Настройки</b>",
        keyboard(
            *[
                [(name, "a:setting:" + key)]
                for key, name in [
                    ("payments", "Способы оплаты и пополнения"),
                    ("pricing", "Общая наценка"),
                    ("maintenance", "Технические работы"),
                    ("support", "Поддержка"),
                    ("branding", "Название студии"),
                ]
            ],
            back="a:home",
        ),
    )


@router.callback_query(F.data.startswith("a:setting:"))
async def setting(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    key = (event.data or "").split(":")[2]
    async with rt.sessions() as s:
        value = await get_setting(s, key)
    rows = [[("Изменить", "a:edit:setting:" + key)]]
    if key == "payments":
        rows += [
            [
                ("Переключить Stars", "a:paytoggle:stars"),
                ("Переключить Crypto Pay", "a:paytoggle:crypto"),
            ]
        ]
    if key == "maintenance":
        rows += [[("Включить / завершить работы", "a:maintenance")]]
    await show(
        event,
        "<pre>" + safe(json.dumps(value, ensure_ascii=False, indent=2)) + "</pre>",
        keyboard(*rows, back="a:settings"),
    )


@router.callback_query(F.data.startswith("a:paytoggle:"))
@router.callback_query(F.data == "a:maintenance")
async def setting_toggle(event: CallbackQuery, state: FSMContext, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    key = "maintenance" if event.data == "a:maintenance" else "payments"
    async with rt.sessions() as s:
        value = await get_setting(s, key)
    field = "enabled" if key == "maintenance" else (event.data or "").split(":")[2] + "_enabled"
    value[field] = not value.get(field)
    await preview(
        event,
        state,
        {"action": "setting", "key": key, "value": value},
        json.dumps(value, ensure_ascii=False, indent=2),
    )


@router.callback_query(F.data.startswith("a:user:"))
async def user_card(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    target = int((event.data or "").split(":")[2])
    async with rt.sessions() as s:
        user = await s.get(User, target)
        bal = await s.get(Wallet, target)
        if not user or not bal:
            raise BusinessError("Пользователь не найден.")
        sub = await subscriptions.active(s, target)
        recent_jobs = list(
            await s.scalars(
                select(Job).where(Job.user_id == target).order_by(Job.created_at.desc()).limit(5)
            )
        )
        ledger = list(
            await s.scalars(
                select(Ledger).where(Ledger.user_id == target).order_by(Ledger.id.desc()).limit(5)
            )
        )
    rows = [
        [("Корректировка баланса", f"a:edit:adjust:{target}")],
        [("Разблокировать" if user.blocked else "Блокировать", f"a:block:{target}")],
        [("Заметка", f"a:edit:note:{target}"), ("Выдать подписку", f"a:edit:grant:{target}")],
        [("Функции и параллельные задачи", f"a:edit:restrictions:{target}")],
        [
            ("Все операции", f"a:history:{target}:ledger:0"),
            ("Все счета", f"a:history:{target}:payments:0"),
        ],
        [("Все генерации", f"a:history:{target}:jobs:0")],
    ]
    if sub:
        rows += [
            [
                ("Продлить", f"a:edit:extend:{sub.id.hex}"),
                ("Отменить подписку", f"a:subcancel:{sub.id.hex}"),
            ],
            [("Изменить личные лимиты", f"a:edit:limits:{sub.id.hex}")],
        ]
    text = (
        f"<b>{target} · @{safe(user.username or '')}</b>\n\nБаланс: ${bal.balance} · резерв ${bal.reserved}\nБлокировка: {user.blocked}\nАктивность: {local_time(user.last_seen):%d.%m %H:%M}\nПодписка: {sub.expires_at if sub else 'нет'}\nЗаметка: {safe(user.note)}\n\nПоследние операции:\n"
        + "\n".join(f"{i.kind}: {i.amount}" for i in ledger)
    )
    rows += [[(f"{j.model_key} · {j.status}", "a:job:" + j.id.hex)] for j in recent_jobs]
    await show(event, text, keyboard(*rows, back="a:list:users:0"))


@router.callback_query(F.data.startswith("a:block:"))
async def block(event: CallbackQuery, state: FSMContext, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    target = int((event.data or "").split(":")[2])
    async with rt.sessions() as s:
        user = await s.get(User, target)
        if not user:
            raise BusinessError("Пользователь не найден.")
    await preview(
        event,
        state,
        {"action": "blocked", "target": target, "value": not user.blocked},
        f"Изменить блокировку пользователя {target}: {not user.blocked}",
    )


@router.callback_query(F.data == "a:plans")
async def plans(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    async with rt.sessions() as s:
        items = list(await s.scalars(select(Plan).order_by(Plan.id)))
    await show(
        event,
        "<b>Тарифы подписок</b>\n\nИзменение плана применяется к новым покупкам.",
        keyboard(
            *[
                [
                    (
                        f"{p.id} · {p.name} · ${p.price} · {'вкл' if p.enabled else 'выкл'}",
                        f"a:plan:{p.id}",
                    )
                ]
                for p in items
            ],
            [("Создать тариф", "a:edit:plan:new")],
            back="a:home",
        ),
    )


@router.callback_query(F.data.startswith("a:plan:"))
async def plan_card(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    value = int((event.data or "").split(":")[2])
    async with rt.sessions() as s:
        plan = await s.get(Plan, value)
        if not plan:
            raise BusinessError("Тариф не найден.")
    await show(
        event,
        f"<b>{safe(plan.name)}</b>\nСрок: {plan.months} мес.\nЦена: ${plan.price}\n<pre>{safe(json.dumps(plan.limits, ensure_ascii=False, indent=2))}</pre>",
        keyboard(
            [("Изменить", f"a:edit:plan:{plan.id}"), ("Вкл / выкл", f"a:plantoggle:{plan.id}")],
            back="a:plans",
        ),
    )


@router.callback_query(F.data.startswith("a:plantoggle:"))
@router.callback_query(F.data.startswith("a:subcancel:"))
async def plan_toggle(event: CallbackQuery, state: FSMContext, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    _, action, value = (event.data or "").split(":")
    await preview(
        event,
        state,
        {"action": action, "target": value},
        "Подтвердить изменение подписки / доступности тарифа?",
    )


@router.callback_query(F.data.startswith("a:invoice:"))
async def invoice_card(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    async with rt.sessions() as s:
        item = await s.get(Invoice, uuid.UUID((event.data or "").split(":")[2]))
        if not item:
            raise BusinessError("Счёт не найден.")
    await show(
        event,
        f"<b>Счёт {item.id}</b>\n\nПользователь: {item.user_id}\nПровайдер: {item.provider}\nСумма: ${item.amount}\nСтатус: {item.status}\nВнешний ID: {safe(item.provider_id or 'не получен')}",
        keyboard(
            [("Сверить через API", "a:reconcile:" + item.id.hex)],
            [("Пользователь", f"a:user:{item.user_id}")],
            back="a:list:payments:0",
        ),
    )


@router.callback_query(F.data.startswith("a:reconcile:"))
async def reconcile(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    invoice_id = uuid.UUID((event.data or "").split(":")[2])
    async with rt.sessions() as s:
        item = await s.get(Invoice, invoice_id)
        if not item:
            raise BusinessError("Счёт не найден.")
    if item.provider == "crypto" and item.provider_id:
        remote = await rt.crypto.get(item.provider_id)
        if remote and remote.get("status") == "paid":
            async with rt.sessions.begin() as s:
                await payments.credit_crypto(s, remote)
                await audit(s, uid, "invoice.reconcile", str(invoice_id))
        await show(
            event,
            "Проверка выполнена. Статус провайдера: "
            + safe(remote.get("status") if remote else "не найден"),
            keyboard(back="a:invoice:" + invoice_id.hex),
        )
    elif item.provider == "stars":
        from premium_bot.workers.billing import reconcile_stars

        count = await reconcile_stars(rt)
        await show(
            event,
            f"Проверены транзакции Stars. Начислений при сверке: {count}.",
            keyboard(back="a:invoice:" + invoice_id.hex),
        )
    else:
        raise BusinessError(
            "Внешний ID отсутствует. Проверьте провайдера; новый счёт автоматически не создаётся."
        )


@router.callback_query(F.data.startswith("a:job:"))
async def job_card(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    async with rt.sessions() as s:
        item = await s.get(Job, uuid.UUID((event.data or "").split(":")[2]))
        if not item:
            raise BusinessError("Задание не найдено.")
    await show(
        event,
        f"<b>Задание {item.id}</b>\n\nПользователь: {item.user_id}\nМодель: {safe(item.model_key)}\nСтатус: {safe(item.status)}\nЦена: ${item.price}\nОценка расходов: ${item.estimated_cost}\nФактические расходы: {item.actual_cost if item.actual_cost is not None else 'не подтверждены'}\nPrediction: {safe(item.prediction_id or 'не получен')}\n{safe(item.error or '')}",
        keyboard(
            [("Сверить статус", "a:syncjob:" + item.id.hex)],
            [("Привязать prediction ID", "a:edit:attach:" + item.id.hex)],
            [("Зафиксировать расходы", "a:edit:cost:" + item.id.hex)],
            [("Завершить со сверкой", "a:edit:resolve:" + item.id.hex)],
            [("Пользователь", f"a:user:{item.user_id}")],
            back="a:list:jobs:0",
        ),
    )


@router.callback_query(F.data.startswith("a:syncjob:"))
async def sync_job(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    value = uuid.UUID((event.data or "").split(":")[2])
    async with rt.sessions.begin() as s:
        await enqueue(s, f"admin-sync:{uuid.uuid4()}", "sync", {"job_id": str(value)})
        await audit(s, uid, "job.sync", str(value))
    await show(event, "Проверка поставлена в очередь.", keyboard(back="a:job:" + value.hex))


@router.callback_query(F.data == "a:exports")
async def exports(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    await show(
        event,
        "Выберите отчёт. Один файл содержит до 10000 последних записей; следующие страницы доступны отдельно.",
        keyboard(
            *[
                [(name, "a:export:" + key + ":0")]
                for key, name in [
                    ("users", "Пользователи"),
                    ("payments", "Платежи"),
                    ("ledger", "Операции"),
                    ("jobs", "Генерации"),
                    ("audit", "Действия администраторов"),
                    ("subscriptions", "Подписки"),
                ]
            ],
            back="a:home",
        ),
    )


@router.callback_query(F.data.startswith("a:export:"))
async def export(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    _, _, key, page = (event.data or "").split(":")
    entity = service.EXPORTS.get(key)
    if not entity:
        raise BusinessError("Отчёт не найден.")
    columns = [
        c
        for c in entity.__table__.columns
        if c.name not in {"snapshot", "params", "input_ids", "url"}
    ]
    order = entity.__table__.c.get("created_at")
    if order is None:
        order = columns[0]
    async with rt.sessions() as s:
        rows = (
            await s.execute(
                select(*columns)
                .order_by(order.desc())
                .offset(max(0, int(page)) * 10000)
                .limit(10000)
            )
        ).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([c.name for c in columns])
    for row in rows:
        # Prevent spreadsheet formula injection in exported user/admin text.
        writer.writerow(
            [
                "'" + str(v)
                if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r"))
                else str(v)
                if v is not None
                else ""
                for v in row
            ]
        )
    await answer_callback(event)
    await rt.bot.send_document(
        uid,
        BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename=f"{key}-{page}.csv"),
        reply_markup=keyboard(
            [("Следующая страница", f"a:export:{key}:{int(page) + 1}")], back="a:exports"
        ),
    )


@router.callback_query(F.data == "a:admins")
async def admins(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    async with rt.sessions() as s:
        ids = list(await s.scalars(select(Admin.user_id)))
    await show(
        event,
        "<b>Администраторы</b>\n\n"
        + "\n".join(str(i) for i in sorted(set(ids + rt.cfg.admin_ids))),
        keyboard([("Добавить / удалить", "a:edit:admins")], back="a:home"),
    )


@router.callback_query(F.data == "a:broadcasts")
async def broadcasts(event: CallbackQuery, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    async with rt.sessions() as s:
        items = list(
            await s.scalars(select(Broadcast).order_by(Broadcast.scheduled_at.desc()).limit(8))
        )
    await show(
        event,
        "<b>Рассылки</b>\n\n"
        + "\n".join(
            f"{str(i.id)[:8]} · {i.status} · отправлено {i.sent}, ошибок {i.failed}" for i in items
        ),
        keyboard(
            [("Создать рассылку", "a:edit:broadcast")],
            *[
                [("Остановить " + str(i.id)[:8], "a:bcancel:" + i.id.hex)]
                for i in items
                if i.status in {"scheduled", "sending"}
            ],
            back="a:home",
        ),
    )


@router.callback_query(F.data.startswith("a:bcancel:"))
async def broadcast_cancel(event: CallbackQuery, state: FSMContext, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    await preview(
        event,
        state,
        {"action": "bcancel", "target": (event.data or "").split(":")[2]},
        "Остановить рассылку? Уже отправленные сообщения сохранятся.",
    )


EDIT_HELP = {
    "search": "Введите ID или @username пользователя.",
    "prompt": "Введите системный шаблон. Вставьте {user_prompt} ровно один раз в нужном месте.",
    "name": "Введите отображаемое название модели.",
    "pricing": "Введите JSON: fixed (USD за запуск), per_second, per_output_mp, per_input_mp, safety_factor, markup_percent, source. Ненужные коэффициенты можно пропустить; хотя бы один должен быть положительным. source — ссылка/основание цены. markup_percent: null использует общую наценку.",
    "defaults": "Введите JSON с параметрами из API-схемы модели. Исходники и промпт задаются пользователем.",
    "setting": "Отправьте обновлённые настройки JSON, сохранив показанные имена полей.",
    "adjust": 'Введите JSON: {"amount":"сумма со знаком в USD","reason":"причина"}. Положительная — начислить, отрицательная — списать.',
    "note": "Введите заметку о пользователе.",
    "restrictions": "Введите JSON: photo и video (true / false), max_active_jobs (1–100). Пустой объект {} снимает индивидуальные ограничения.",
    "grant": "Введите ID тарифа подписки, который нужно выдать бесплатно.",
    "extend": "Введите число дней продления (1–366).",
    "limits": 'Введите JSON лимитов: ключ модели → список правил {"max_seconds":длительность,"daily_count":количество}. Для фото max_seconds = 0.',
    "plan": 'Введите JSON: name, months (1 или 3), price (USD), limits. limits: ключ модели → список правил {"max_seconds":длительность,"daily_count":количество}. Ключи: z_image, flux, nano, wan, seedance, omni. Для фото max_seconds = 0.',
    "attach": "Введите prediction ID из Replicate. Версия и промпт будут проверены через API.",
    "cost": 'Введите JSON: {"amount":"фактический расход USD","reason":"источник подтверждения"}.',
    "resolve": 'Только после ручной сверки отсутствующего prediction ID. Введите JSON: {"reason":"почему подтверждено отсутствие генерации"}. Резерв будет возвращён.',
    "admins": 'Введите JSON: {"user_id":Telegram_ID,"enabled":true или false}. Администраторов из окружения удалить нельзя.',
    "broadcast": "Отправьте текст, фото или видео рассылки. Сначала покажу предпросмотр; отправка только после подтверждения.",
}


@router.callback_query(F.data.startswith("a:edit:"))
async def edit(event: CallbackQuery, state: FSMContext, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    parts = (event.data or "").split(":")
    action, target = parts[2], parts[3] if len(parts) > 3 else ""
    if action not in EDIT_HELP:
        raise BusinessError("Неизвестная операция.")
    await state.set_state(Editor.value)
    await state.set_data({"action": action, "target": target})
    current = ""
    async with rt.sessions() as s:
        if action == "setting":
            current = json.dumps(await get_setting(s, target), ensure_ascii=False, indent=2)
        elif action in {"prompt", "name", "pricing", "defaults"}:
            model = await s.get(AIModel, target)
            if model:
                value = getattr(model, action)
                current = (
                    json.dumps(value, ensure_ascii=False, indent=2)
                    if isinstance(value, dict)
                    else str(value)
                )
        elif action == "restrictions":
            user = await s.get(User, int(target))
            if user:
                current = json.dumps(user.restrictions, ensure_ascii=False)
    await show(
        event,
        safe(EDIT_HELP[action])
        + ("\n\nСейчас:\n<pre>" + safe(current[:2000]) + "</pre>" if current else ""),
        keyboard(back="a:home"),
    )


async def preview(
    event: Message | CallbackQuery, state: FSMContext, operation: Json, text: str
) -> None:
    token = uuid.uuid4().hex
    await state.set_state(Editor.confirmation)
    await state.set_data({"operation": operation, "token": token})
    await show(
        event,
        "<b>Проверьте изменение</b>\n\n<pre>" + safe(text[:3000]) + "</pre>",
        keyboard([("Подтвердить", "a:apply:" + token)], back="a:home"),
    )


@router.message(Editor.value, ~F.text.startswith("/"))
async def edit_value(message: Message, state: FSMContext, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    data = await state.get_data()
    action, target = data["action"], data["target"]
    if action == "search":
        async with rt.sessions() as s:
            user = await service.user_lookup(s, message.text or "")
        if not user:
            raise BusinessError("Пользователь не найден.")
        await state.clear()
        await message.answer(
            f"Найден: {user.id}",
            reply_markup=keyboard([("Открыть профиль", f"a:user:{user.id}")], back="a:home"),
        )
        return
    if action == "broadcast":
        payload: Json = {"text": message.text or message.caption or "", "kind": "text"}
        if message.photo:
            payload.update(kind="photo", file_id=message.photo[-1].file_id)
        elif message.video:
            payload.update(kind="video", file_id=message.video.file_id)
        elif not message.text:
            raise BusinessError("Нужны текст, фото или видео.")
        await state.set_state(Editor.confirmation)
        async with rt.sessions.begin() as s:
            item = Broadcast(actor_id=uid, payload=payload)
            s.add(item)
            await s.flush()
        await state.set_data(
            {
                "operation": {"action": "broadcast", "target": str(item.id)},
                "token": uuid.uuid4().hex,
            }
        )
        await message.answer("Предпросмотр:")
        await rt.bot.copy_message(uid, message.chat.id, message.message_id)
        token = (await state.get_data())["token"]
        await message.answer(
            "Отправка учитывает настройки уведомлений. Можно настроить сегмент, время и URL-кнопки.",
            reply_markup=keyboard(
                [("Запустить сейчас", "a:apply:" + token)],
                [("Настроить рассылку", "a:bcsettings:" + item.id.hex)],
                back="a:home",
            ),
        )
        return
    value: object = message.text or ""
    if action in {
        "restrictions",
        "pricing",
        "defaults",
        "setting",
        "adjust",
        "plan",
        "limits",
        "cost",
        "resolve",
        "admins",
        "bcsettings",
    }:
        try:
            value = json.loads(str(value))
        except ValueError as exc:
            raise BusinessError("Некорректный JSON. Проверьте кавычки и запятые.") from exc
        if not isinstance(value, dict):
            raise BusinessError("Ожидается JSON-объект.")
    await preview(
        message,
        state,
        {"action": action, "target": target, "value": value},
        json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, dict) else str(value),
    )


@router.callback_query(F.data.startswith("a:bcsettings:"))
async def broadcast_settings(
    event: CallbackQuery, state: FSMContext, rt: Runtime, uid: int
) -> None:
    await check(rt, uid)
    await state.set_state(Editor.value)
    await state.set_data({"action": "bcsettings", "target": (event.data or "").split(":")[2]})
    await show(
        event,
        "Введите JSON: segment = all, active или paying; scheduled_at = дата ISO 8601 с часовым поясом; buttons = список объектов text и url. Поля необязательны.",
        keyboard(back="a:broadcasts"),
    )


@router.callback_query(Editor.confirmation, F.data.startswith("a:apply:"))
async def apply(event: CallbackQuery, state: FSMContext, rt: Runtime, uid: int) -> None:
    await check(rt, uid)
    data = await state.get_data()
    if data.get("token") != (event.data or "").split(":")[2]:
        raise BusinessError("Подтверждение устарело.")
    operation = data["operation"]
    action, target, value = (
        operation["action"],
        operation.get("target", operation.get("key", "")),
        operation.get("value"),
    )
    if action == "attach":
        remote = await rt.replicate.get(str(value))
    else:
        remote = None
    from premium_bot.application.admin_commands import apply_operation

    await apply_operation(rt, uid, operation, data["token"], remote)
    await state.clear()
    if action == "bcsettings":
        await preview(
            event,
            state,
            {"action": "broadcast", "target": target},
            "Настройки сохранены. Подтвердите запуск рассылки.",
        )
    else:
        await show(event, "Изменения сохранены.", keyboard(back="a:home"))

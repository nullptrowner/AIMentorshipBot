"""Confirmed administrative mutations, separate from Telegram presentation."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from premium_bot.application import admin as service
from premium_bot.application import jobs, subscriptions, wallet
from premium_bot.application.common import audit, enqueue, require_admin
from premium_bot.db import Admin, Broadcast, Job, Plan, Subscription, Task
from premium_bot.domain import TERMINAL, BusinessError, Json, PlanInput, money, now
from premium_bot.infrastructure.replicate import REGISTRY
from premium_bot.runtime import Runtime


async def apply_operation(
    rt: Runtime, uid: int, operation: Json, token: str, remote: Json | None = None
) -> None:
    data = {"token": token}
    action = str(operation["action"])
    target = str(operation.get("target", operation.get("key", "")))
    value: Any = operation.get("value")  # JSON command boundary; validated per operation below.
    async with rt.sessions.begin() as s:
        await require_admin(s, uid, rt.cfg)
        if action in {"prompt", "name", "pricing", "defaults"}:
            await service.update_model(s, rt.cfg, uid, target, action, value)
        elif action == "restore":
            await service.restore_revision(s, rt.cfg, uid, target, operation["revision"])
        elif action == "setting":
            if target == "access":
                parsed = service.AccessConfig.model_validate(value)
                bot_id = rt.bot.id
                for cid in parsed.chat_ids:
                    membership = await rt.bot.get_chat_member(cid, bot_id)
                    if membership.status not in {"administrator", "creator"}:
                        raise BusinessError(f"Бот должен быть администратором чата {cid}.")
            await service.save_setting(s, rt.cfg, uid, target, value)
        elif action in {"blocked", "note", "restrictions"}:
            await service.record_user_change(s, rt.cfg, uid, int(target), action, value)
        elif action == "adjust":
            await wallet.adjust(
                s,
                int(target),
                money(value["amount"]),
                "admin:" + data["token"],
                str(value["reason"]),
                actor=uid,
            )
            await audit(s, uid, "wallet.adjust", str(target), value)
        elif action == "grant":
            plan = await s.get(Plan, int(value))
            if not plan:
                raise BusinessError("Тариф не найден.")
            await subscriptions.buy(s, int(target), plan.id, plan.price, data["token"], admin=True)
            await audit(s, uid, "subscription.grant", str(target), {"plan": plan.id})
        elif action in {"extend", "subcancel"}:
            await subscriptions.change_subscription(
                s, uuid.UUID(target), int(value) if action == "extend" else None
            )
            await audit(s, uid, "subscription." + action, target, {"value": value})
        elif action == "plan":
            validated = PlanInput.model_validate(value)
            if set(validated.limits) - REGISTRY.keys():
                raise BusinessError("Неизвестная модель в лимитах.")
            await subscriptions.save_plan(s, validated, None if target == "new" else int(target))
            await audit(s, uid, "plan.save", target, validated.model_dump(mode="json"))
        elif action == "limits":
            sub = await s.get(Subscription, uuid.UUID(target), with_for_update=True)
            if not sub:
                raise BusinessError("Подписка не найдена.")
            validated = PlanInput(name="validation", months=1, price=Decimal(1), limits=value)
            if set(validated.limits) - REGISTRY.keys():
                raise BusinessError("Неизвестная модель.")
            sub.limits = {
                k: [r.model_dump() for r in rules] for k, rules in validated.limits.items()
            }
            await audit(s, uid, "subscription.limits", target, sub.limits)
        elif action == "plantoggle":
            plan = await s.get(Plan, int(target), with_for_update=True)
            if not plan:
                raise BusinessError("Тариф не найден.")
            plan.enabled = not plan.enabled
            await audit(s, uid, "plan.toggle", target)
        elif action in {"cost", "attach", "resolve"}:
            job = await s.get(Job, uuid.UUID(target), with_for_update=True)
            if not job:
                raise BusinessError("Задание не найдено.")
            if action == "cost":
                amount = money(value["amount"])
                if amount < 0 or not str(value.get("reason", "")).strip():
                    raise BusinessError("Расход неотрицательный; укажите источник подтверждения.")
                job.actual_cost = amount
            elif action == "attach" and remote:
                if (
                    job.status in TERMINAL
                    or job.prediction_id
                    or remote.get("version") != job.snapshot["version"]
                    or remote.get("input", {}).get("prompt") != job.snapshot["final_prompt"]
                ):
                    raise BusinessError(
                        "Prediction не соответствует заданию или задание уже завершено/привязано."
                    )
                job.prediction_id = str(remote["id"])
                await enqueue(s, "manual-sync:" + data["token"], "sync", {"job_id": str(job.id)})
            elif action == "resolve":
                if job.prediction_id or not str(value.get("reason", "")).strip():
                    raise BusinessError(
                        "Нужна причина сверки; задание с prediction ID завершается через API."
                    )
                await jobs.finish(s, job, "failed", "Завершено администратором после сверки.")
            await audit(s, uid, "job." + action, target, {"value": value})
        elif action == "admins":
            admin_id = int(value["user_id"])
            item = await s.get(Admin, admin_id)
            if value["enabled"] is True and not item:
                s.add(Admin(user_id=admin_id))
            elif value["enabled"] is False and item:
                if admin_id in rt.cfg.admin_ids or admin_id == uid:
                    raise BusinessError("Нельзя удалить себя или администратора из окружения.")
                await s.delete(item)
            await audit(s, uid, "admin.access", str(admin_id), value)
        elif action == "retry_task":
            task = await s.get(Task, uuid.UUID(target), with_for_update=True)
            if task is None or not task.done or not task.last_error:
                raise BusinessError("Повтор доступен только для завершённых задач с ошибкой.")
            task.done, task.attempts, task.last_error, task.due_at = False, 0, None, now()
            await audit(s, uid, "task.retry", target)
        elif action in {"broadcast", "bcancel", "bcsettings"}:
            broadcast_record = await s.get(Broadcast, uuid.UUID(target), with_for_update=True)
            if not broadcast_record:
                raise BusinessError("Рассылка не найдена.")
            if action == "bcancel":
                broadcast_record.status = "canceled"
            elif action == "bcsettings":
                if broadcast_record.status != "draft":
                    raise BusinessError("Настройки доступны только для черновика.")
                segment = value.get("segment", "all")
                if segment not in {"all", "active", "paying"}:
                    raise BusinessError("Сегмент: all, active, paying.")
                broadcast_record.segment = segment
                if value.get("scheduled_at"):
                    stamp = datetime.fromisoformat(value["scheduled_at"])
                    if stamp.tzinfo is None:
                        raise BusinessError("Укажите часовой пояс в дате.")
                    broadcast_record.scheduled_at = stamp
                buttons = value.get("buttons", [])
                if len(buttons) > 8 or any(
                    not isinstance(b.get("text"), str)
                    or not 1 <= len(b["text"]) <= 64
                    or not str(b.get("url", "")).startswith("https://")
                    for b in buttons
                ):
                    raise BusinessError("До восьми кнопок с текстом и HTTPS URL.")
                broadcast_record.payload = {**broadcast_record.payload, "buttons": buttons}
            elif broadcast_record.status == "draft":
                broadcast_record.status = "scheduled"
                await enqueue(
                    s,
                    f"broadcast:{broadcast_record.id}",
                    "broadcast",
                    {"broadcast_id": str(broadcast_record.id)},
                    delay=max(0, int((broadcast_record.scheduled_at - now()).total_seconds())),
                )
            await audit(s, uid, "broadcast." + action, target)
        else:
            raise BusinessError("Неизвестная операция.")

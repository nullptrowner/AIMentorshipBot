"""Apply the reviewed Replicate tariff snapshot using normal audited admin operations."""

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from premium_bot.application.admin import save_setting, update_model
from premium_bot.application.common import get_setting, require_admin
from premium_bot.config import Settings, settings
from premium_bot.db import AIModel, database
from premium_bot.domain import BusinessError, Json


async def apply_pricing(
    session: AsyncSession, cfg: Settings, bundle: Json, markup: str | None = None
) -> None:
    if not cfg.admin_ids:
        raise BusinessError("Заполните ADMIN_IDS в .env.")
    actor = cfg.admin_ids[0]
    await require_admin(session, actor, cfg)
    if markup is not None:
        await save_setting(session, cfg, actor, "pricing", {"markup_percent": markup})
    current = await get_setting(session, "pricing")
    if current.get("markup_percent") is None:
        raise BusinessError(
            "Сначала задайте общую наценку в админке или передайте --markup-percent ЧИСЛО. "
            "Для работы по себестоимости укажите 0."
        )
    for key, profile in bundle["profiles"].items():
        if await session.get(AIModel, key) is None:
            raise BusinessError("Нет модели " + key + "; сначала выполните team-ai seed.")
        await update_model(session, cfg, actor, key, "pricing", profile)
    for key in bundle.get("blocked", {}):
        if await session.get(AIModel, key) is not None:
            await update_model(session, cfg, actor, key, "enabled", False)


async def run(markup: str | None) -> None:
    cfg = settings()
    bundle = json.loads(Path(__file__).with_name("replicate-pricing.json").read_text())
    sessions, engine = database(cfg.database_url)
    try:
        async with sessions.begin() as session:
            await apply_pricing(session, cfg, bundle, markup)
            current = await get_setting(session, "pricing")
    finally:
        await engine.dispose()
    print("Тарифы сохранены: " + ", ".join(bundle["profiles"]))
    print("Общая наценка: " + str(current["markup_percent"]) + "%")
    for key, reason in bundle.get("blocked", {}).items():
        print("Модель выключена до уточнения тарифа: " + key + ". " + reason)
    print("Цены уже созданных заказов сохранены. Включение моделей выполняется в админке.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--markup-percent", help="Изменить общую наценку; без флага сохранить её")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.markup_percent))
    except BusinessError as exc:
        raise SystemExit(str(exc)) from None

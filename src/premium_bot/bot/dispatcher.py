from aiogram import Dispatcher, Router
from aiogram.fsm.storage.redis import RedisEventIsolation, RedisStorage
from aiogram.types import CallbackQuery, Message

from premium_bot.bot import admin, user
from premium_bot.bot.middleware import AccessMiddleware
from premium_bot.bot.ui import answer_callback
from premium_bot.runtime import Runtime


def dispatcher(rt: Runtime) -> Dispatcher:
    storage = RedisStorage(rt.redis, state_ttl=3600, data_ttl=3600)
    dp = Dispatcher(
        storage=storage,
        events_isolation=RedisEventIsolation(rt.redis, lock_kwargs={"timeout": 180}),
    )
    dp.message.outer_middleware(AccessMiddleware())
    dp.callback_query.outer_middleware(AccessMiddleware())
    dp.pre_checkout_query.outer_middleware(AccessMiddleware())
    # Handler definitions are reusable; Router instances themselves have a single parent.
    for source in (admin.router, user.router):
        fresh = Router(name=source.name)
        for name, observer in source.observers.items():
            fresh.observers[name].handlers.extend(observer.handlers)
        dp.include_router(fresh)

    fallback_router = Router(name="fallback")

    @fallback_router.callback_query()
    async def stale(event: CallbackQuery) -> None:
        await answer_callback(event, "Кнопка устарела. Откройте /start.", show_alert=True)

    @fallback_router.message()
    async def fallback(message: Message) -> None:
        await message.answer(
            "Не удалось распознать действие. Продолжите текущий шаг или откройте /start."
        )

    dp.include_router(fallback_router)

    return dp

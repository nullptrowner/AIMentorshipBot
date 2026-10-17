from dataclasses import dataclass

import httpx
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from premium_bot.config import Settings
from premium_bot.db import database
from premium_bot.infrastructure.crypto import CryptoPay
from premium_bot.infrastructure.media import MediaStore
from premium_bot.infrastructure.replicate import Replicate


@dataclass
class Runtime:
    cfg: Settings
    sessions: async_sessionmaker[AsyncSession]
    engine: AsyncEngine
    redis: Redis
    http: httpx.AsyncClient
    bot: Bot
    replicate: Replicate
    crypto: CryptoPay
    media: MediaStore

    @classmethod
    def create(cls, cfg: Settings) -> "Runtime":
        sessions, engine = database(cfg.database_url)
        client = httpx.AsyncClient(limits=httpx.Limits(max_connections=20), follow_redirects=False)
        return cls(
            cfg,
            sessions,
            engine,
            Redis.from_url(cfg.redis_url),
            client,
            Bot(cfg.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode="HTML")),
            Replicate(cfg.replicate_token.get_secret_value(), client),
            CryptoPay(cfg.crypto_token.get_secret_value(), cfg.crypto_testnet, client),
            MediaStore(cfg, client),
        )

    async def close(self) -> None:
        await self.bot.session.close()
        await self.http.aclose()
        await self.redis.aclose()
        await self.engine.dispose()

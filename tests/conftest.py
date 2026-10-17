import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text

from premium_bot.application.admin import seed
from premium_bot.application.common import register
from premium_bot.application.wallet import adjust
from premium_bot.config import Settings
from premium_bot.db import AIModel, Base
from premium_bot.runtime import Runtime


@pytest.fixture
def cfg(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=os.getenv(
            "TEST_DATABASE_URL",
            "postgresql+asyncpg://studio:test-only-password@127.0.0.1:55432/studio_test",
        ),
        redis_url=os.getenv("TEST_REDIS_URL", "redis://127.0.0.1:56379/15"),
        bot_token="123456:TEST_ONLY_NEVER_PRODUCTION_TOKEN",
        admin_ids=[1],
        public_url="https://studio.example",
        media_secret="m" * 32,
        telegram_secret="t" * 32,
        replicate_webhook_secret="whsec_dGVzdC1zZWNyZXQ=",
        crypto_token="test-crypto-token",
        input_retention_hours=24,
        output_retention_hours=72,
        media_dir=tmp_path / "media",
        media_quota_bytes=1_000_000_000,
    )


@pytest_asyncio.fixture
async def rt(cfg: Settings):
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    if not cfg.database_url.endswith("studio_test") or not cfg.redis_url.endswith("/15"):
        raise RuntimeError("Integration tests require a dedicated studio_test DB and Redis DB 15")
    instance = Runtime.create(cfg)
    async with instance.engine.begin() as connection:
        tables = ", ".join('"' + name + '"' for name in Base.metadata.tables)
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    await instance.redis.flushdb()
    async with instance.sessions.begin() as s:
        await seed(s, cfg)
        await register(s, 1, "admin")
        await register(s, 2, "employee")
        await register(s, 3, "other_employee")
        await adjust(s, 2, Decimal("10"), "test-credit", "Test fixture")
        model = await s.get(AIModel, "z_image")
        model.enabled = True
        model.version = "test-version"
        model.api_schema = image_schema()
        model.pricing = {"fixed": "1", "markup_percent": "0", "source": "test fixture only"}
        model.defaults = {"width": 512, "height": 512}
    try:
        yield instance
    finally:
        await instance.close()


def image_schema():
    return {
        "components": {
            "schemas": {
                "Input": {
                    "type": "object",
                    "required": ["prompt"],
                    "properties": {
                        "prompt": {"type": "string"},
                        "width": {
                            "type": "integer",
                            "default": 1024,
                            "minimum": 64,
                            "maximum": 2048,
                        },
                        "height": {
                            "type": "integer",
                            "default": 1024,
                            "minimum": 64,
                            "maximum": 2048,
                        },
                    },
                }
            }
        }
    }


@pytest.fixture
def bot_mock():
    return AsyncMock()

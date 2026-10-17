from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+asyncpg://studio:studio@localhost:5432/studio"
    redis_url: str = "redis://localhost:6379/0"
    bot_token: SecretStr = SecretStr("")
    admin_ids: list[int] = Field(default_factory=list)
    public_url: str = ""
    telegram_secret: SecretStr = SecretStr("")
    replicate_token: SecretStr = SecretStr("")
    replicate_webhook_secret: SecretStr = SecretStr("")
    crypto_token: SecretStr = SecretStr("")
    crypto_testnet: bool = False
    # Enabling an integration does not authorize selling digital goods with it in Telegram.
    crypto_sales_enabled: bool = False
    media_secret: SecretStr = SecretStr("")
    media_dir: Path = Path("data/media")
    media_quota_bytes: int = 10_000_000_000
    max_upload_bytes: int = 20_000_000
    max_output_bytes: int = 49_000_000
    max_audio_seconds: int = 120
    concurrent_generations: int = Field(default=10, ge=1, le=100)
    media_concurrency: int = Field(default=1, ge=1, le=1)
    poll_seconds: int = Field(default=15, ge=1, le=3600)
    job_timeout_seconds: int = Field(default=3600, ge=60, le=86400)
    quote_ttl_seconds: int = Field(default=600, ge=30, le=3600)
    input_retention_hours: int = 0
    output_retention_hours: int = 0
    log_level: str = "INFO"

    @field_validator("public_url")
    @classmethod
    def public_https(cls, value: str) -> str:
        if value and not value.startswith("https://"):
            raise ValueError("PUBLIC_URL must use HTTPS")
        return value.rstrip("/")

    def require_production(self) -> None:
        required = (self.bot_token, self.telegram_secret, self.media_secret)
        if not all(x.get_secret_value() for x in required) or not self.public_url:
            raise ValueError("Configure BOT_TOKEN, PUBLIC_URL, TELEGRAM_SECRET, MEDIA_SECRET")
        if (
            min(
                len(self.telegram_secret.get_secret_value()),
                len(self.media_secret.get_secret_value()),
            )
            < 32
        ):
            raise ValueError("Webhook and media secrets must contain at least 32 characters")
        if not self.admin_ids:
            raise ValueError("ADMIN_IDS must contain at least one Telegram user ID")
        if min(self.input_retention_hours, self.output_retention_hours) <= 0:
            raise ValueError("Set positive input/output retention hours before running the bot")


@lru_cache
def settings() -> Settings:
    return Settings()

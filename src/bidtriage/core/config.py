"""Typed settings (12-factor). Everything configurable lives here or in DB rows."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = Field(default="dev", alias="BIDTRIAGE_ENV")
    database_url: str = Field(
        default="postgresql+psycopg://bidtriage:bidtriage@localhost:5432/bidtriage",
        alias="DATABASE_URL",
    )
    blob_dir: str = Field(default="./var/blobs", alias="BLOB_DIR")
    secret_key: str = Field(
        default="dev-secret-key-dev-secret-key-dev-secret-key", alias="SECRET_KEY"
    )
    base_url: str = Field(default="http://localhost:8000", alias="BASE_URL")

    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    extraction_model: str = Field(default="claude-opus-5-5", alias="EXTRACTION_MODEL")
    extraction_effort: str = Field(default="medium", alias="EXTRACTION_EFFORT")
    llm_daily_spend_cap_usd: float = Field(default=25.0, alias="LLM_DAILY_SPEND_CAP_USD")

    digest_time: str = Field(default="06:30", alias="DIGEST_TIME")
    digest_timezone: str = Field(default="America/New_York", alias="DIGEST_TIMEZONE")
    digest_from: str = Field(default="bids@example.com", alias="DIGEST_FROM")
    smtp_url: str = Field(default="smtp://localhost:1025", alias="SMTP_URL")

    home_address: str = Field(
        default="250 Curry Hollow Road, Pittsburgh, PA 15236", alias="HOME_ADDRESS"
    )
    home_lat: float = Field(default=40.3462, alias="HOME_LAT")
    home_lon: float = Field(default=-79.9482, alias="HOME_LON")

    action_token_ttl_days: int = Field(default=7, alias="ACTION_TOKEN_TTL_DAYS")

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()

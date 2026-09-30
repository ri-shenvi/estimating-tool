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
    blob_bucket: str | None = Field(default=None, alias="BLOB_BUCKET")
    blob_endpoint_url: str | None = Field(default=None, alias="BLOB_ENDPOINT_URL")
    blob_prefix: str = Field(default="", alias="BLOB_PREFIX")
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

    # SPEC-01: ingestion
    ingest_backfill_days: int = Field(default=90, alias="INGEST_BACKFILL_DAYS")
    ingest_backfill_batch: int = Field(default=50, alias="INGEST_BACKFILL_BATCH")
    ingest_live_batch: int = Field(default=200, alias="INGEST_LIVE_BATCH")
    ingest_ocr: bool = Field(default=False, alias="INGEST_OCR")
    admin_alert_to: str | None = Field(default=None, alias="ADMIN_ALERT_TO")

    # SPEC-10: ingestion hardening
    ingest_max_batch_bytes: int = Field(default=256 * 1024 * 1024, alias="INGEST_MAX_BATCH_BYTES")
    ingest_max_fetch_attempts: int = Field(default=5, alias="INGEST_MAX_FETCH_ATTEMPTS")
    ingest_max_attachments_per_message: int = Field(
        default=50, alias="INGEST_MAX_ATTACHMENTS_PER_MESSAGE"
    )
    ingest_max_backfill_attempts: int = Field(default=20, alias="INGEST_MAX_BACKFILL_ATTEMPTS")
    ingest_poll_retention_days: int = Field(default=30, alias="INGEST_POLL_RETENTION_DAYS")
    ingest_max_upload_bytes: int = Field(default=64 * 1024 * 1024, alias="INGEST_MAX_UPLOAD_BYTES")
    ingest_metrics_cache_seconds: int = Field(default=60, alias="INGEST_METRICS_CACHE_SECONDS")

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()

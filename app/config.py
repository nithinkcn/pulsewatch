"""Runtime configuration, loaded from the environment once per process."""

from functools import lru_cache

from pydantic import Field, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- database -------------------------------------------------------
    # One URL in the environment; the sync/async drivers are derived from it
    # so a deployment can never point the API and the workers at different
    # databases by accident.
    database_url: str = Field(
        default="postgresql://pulsewatch:pulsewatch@localhost:5432/pulsewatch",
        description="Base PostgreSQL DSN, without a driver suffix.",
    )

    @property
    def async_database_url(self) -> str:
        return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    @property
    def sync_database_url(self) -> str:
        return self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)

    # --- redis ----------------------------------------------------------
    redis_url: RedisDsn = Field(default=RedisDsn("redis://localhost:6379/0"))

    @property
    def celery_broker_url(self) -> str:
        return str(self.redis_url)

    # --- scheduling -----------------------------------------------------
    # How often Beat wakes the dispatcher. This is *not* the check interval:
    # each target carries its own interval and the dispatcher decides which
    # ones are due. See app/workers/tasks.py for why.
    dispatch_interval_seconds: int = Field(default=10, ge=1, le=300)

    # Ceiling on how long a single probe may hold a worker slot.
    max_probe_timeout_seconds: float = Field(default=30.0, gt=0)

    # --- alerting defaults ----------------------------------------------
    # Consecutive results required before a target changes state. Defaults
    # are per-target overridable; these are the fallbacks.
    default_failure_threshold: int = Field(default=3, ge=1)
    default_recovery_threshold: int = Field(default=2, ge=1)

    # --- housekeeping ---------------------------------------------------
    check_retention_days: int = Field(default=30, ge=1)

    log_level: str = Field(default="INFO")
    environment: str = Field(default="local")


@lru_cache
def get_settings() -> Settings:
    return Settings()

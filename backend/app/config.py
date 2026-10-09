from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator, EmailStr, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR = Path(__file__).resolve().parent
DEV_SECRET_KEY = "dev-only-change-me-dev-only-change-me"


def _empty_as_none(value):
    """An env var that is set but empty (e.g. an unset CI variable) means "not configured"."""
    return None if value == "" else value


class Settings(BaseSettings):
    """Infrastructure settings, from environment variables (or a local .env file).

    Provider settings (API keys, models, the summary prompt, limits) are not here: admins
    manage them at runtime and they are stored in the database (see app/runtime_settings.py).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Meetings API"
    environment: str = "development"

    # PostgreSQL only. Production: Neon's pooled connection string with the psycopg driver,
    # e.g. postgresql+psycopg://user:pass@ep-xxx-pooler.eu-central-1.aws.neon.tech/meetings?sslmode=require
    database_url: str

    # Signs login tokens and encrypts the API keys stored in the database. Changing it signs
    # everyone out and means the API keys have to be entered again in the admin settings.
    secret_key: str = Field(default=DEV_SECRET_KEY, min_length=32)
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 30

    # Seeded admin: created at startup if no account with this email exists yet. The
    # password is only used for that first creation; change it in the app afterwards.
    admin_email: Annotated[EmailStr | None, BeforeValidator(_empty_as_none)] = None
    # SecretStr: never shown in logs or validation errors. Its length is checked when the
    # account is created (a short one is logged as an error; it doesn't stop the app).
    admin_password: Annotated[SecretStr | None, BeforeValidator(_empty_as_none)] = None

    # Hard cap on any request body. Cloud Run rejects HTTP/1 bodies over 32 MiB anyway;
    # the (lower) per-upload limit is an admin setting.
    max_request_mb: int = 32

    # A meeting stuck in a processing state longer than this is reported as failed
    # (e.g. the instance was shut down mid-job). The user can then retry.
    processing_timeout_minutes: int = 30

    work_dir: Path = Path("/tmp/meetings-work")


@lru_cache
def get_settings() -> Settings:
    return Settings()

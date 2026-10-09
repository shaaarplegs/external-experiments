from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR = Path(__file__).resolve().parent
DEV_JWT_SECRET = "dev-only-change-me-dev-only-change-me"


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Meetings API"
    environment: str = "development"

    # Database: SQLite for local dev/tests, Neon (Postgres) in production.
    # Neon example: postgresql+psycopg://user:pass@ep-xxx-pooler.eu-central-1.aws.neon.tech/meetings?sslmode=require
    database_url: str = "sqlite:///./meetings.db"

    # Auth
    jwt_secret: str = Field(default=DEV_JWT_SECRET, min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 30

    # Uploads. Cloud Run rejects HTTP/1 request bodies over 32 MiB, so stay under that.
    max_upload_mb: int = 30
    max_audio_minutes: int = 180

    # Speech-to-text: any OpenAI-compatible transcription endpoint (Groq by default).
    transcription_base_url: str = "https://api.groq.com/openai/v1"
    transcription_api_key: str = ""
    transcription_model: str = "whisper-large-v3"
    transcription_language: str | None = "ar"  # default hint; can be overridden per upload
    transcription_chunk_seconds: int = 600
    transcription_timeout_seconds: float = 300

    # Summarizer: any OpenAI-compatible chat endpoint (DeepSeek by default).
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 8000
    llm_timeout_seconds: float = 300
    summary_prompt_path: Path = APP_DIR / "prompts" / "meeting_summary.md"
    notes_prompt_path: Path = APP_DIR / "prompts" / "transcript_notes.md"
    # Transcripts longer than this are condensed into notes chunk by chunk before summarizing.
    summary_max_transcript_chars: int = 60_000

    # A meeting stuck in a processing state longer than this is reported as failed
    # (e.g. the instance was shut down mid-job). The user can then retry.
    processing_timeout_minutes: int = 30

    work_dir: Path = Path("/tmp/meetings-work")


@lru_cache
def get_settings() -> Settings:
    return Settings()

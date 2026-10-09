"""Admin-managed settings, stored in the database (app_settings table).

Defaults are defined here; each row in app_settings overrides one field. API keys are
encrypted with SECRET_KEY before they are stored, and the API only ever shows their last
four characters. Settings are read from the database whenever they are used, so a change
made in the admin panel applies to the next upload on every Cloud Run instance.
"""

import logging
import uuid
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    StringConstraints,
    ValidationError,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import APP_DIR
from app.models import AppSetting
from app.security import SecretBox

logger = logging.getLogger(__name__)

DEFAULT_SUMMARY_PROMPT = (APP_DIR / "prompts" / "meeting_summary.md").read_text(encoding="utf-8")
DEFAULT_NOTES_PROMPT = (APP_DIR / "prompts" / "transcript_notes.md").read_text(encoding="utf-8")
SECRET_FIELDS = frozenset({"transcription_api_key", "llm_api_key"})


def _require_json_word(prompt: str) -> str:
    if "json" not in prompt.lower():
        raise ValueError('must ask for a json object (DeepSeek\'s JSON mode requires the word "json")')
    return prompt


Url = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^https?://\S+$", max_length=500)]
ModelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ApiKey = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
Language = Annotated[
    str,
    BeforeValidator(lambda v: v.strip().lower() if isinstance(v, str) else v),
    StringConstraints(pattern=r"^([a-z]{2,3}|auto)$"),
]
ChunkSeconds = Annotated[int, Field(ge=60, le=1200)]
Temperature = Annotated[float, Field(ge=0, le=2)]
MaxTokens = Annotated[int, Field(ge=256, le=64000)]
SummaryPrompt = Annotated[str, StringConstraints(min_length=50, max_length=50_000), AfterValidator(_require_json_word)]
NotesPrompt = Annotated[str, StringConstraints(min_length=20, max_length=50_000)]
TranscriptChars = Annotated[int, Field(ge=5_000, le=500_000)]
UploadMb = Annotated[int, Field(ge=1, le=32, description="Cloud Run rejects request bodies over 32 MiB")]
AudioMinutes = Annotated[int, Field(ge=1, le=600)]


class RuntimeSettings(BaseModel):
    # Speech-to-text: any OpenAI-compatible /audio/transcriptions endpoint.
    transcription_base_url: Url = "https://api.groq.com/openai/v1"
    transcription_api_key: ApiKey = ""
    transcription_model: ModelName = "whisper-large-v3"
    transcription_language: Language = Field(
        "ar", description='Default speech language hint, or "auto"; uploads can override it'
    )
    transcription_chunk_seconds: ChunkSeconds = 600

    # Summaries: any OpenAI-compatible chat completions endpoint.
    llm_base_url: Url = "https://api.deepseek.com"
    llm_api_key: ApiKey = ""
    llm_model: ModelName = "deepseek-chat"
    llm_temperature: Temperature = 0.3
    llm_max_tokens: MaxTokens = 8000
    summary_prompt: SummaryPrompt = Field(DEFAULT_SUMMARY_PROMPT, description="The minutes format")
    notes_prompt: NotesPrompt = Field(DEFAULT_NOTES_PROMPT, description="Condenses very long transcripts")
    summary_max_transcript_chars: TranscriptChars = 60_000

    # Limits
    max_upload_mb: UploadMb = 30
    max_audio_minutes: AudioMinutes = 180

    @property
    def language_hint(self) -> str | None:
        return None if self.transcription_language == "auto" else self.transcription_language


class RuntimeSettingsUpdate(BaseModel):
    """PATCH body: only the fields sent are changed. For API keys, an empty string or null
    removes the key; for any other field, null resets it to the default."""

    transcription_base_url: Url | None = None
    transcription_api_key: ApiKey | None = None
    transcription_model: ModelName | None = None
    transcription_language: Language | None = None
    transcription_chunk_seconds: ChunkSeconds | None = None
    llm_base_url: Url | None = None
    llm_api_key: ApiKey | None = None
    llm_model: ModelName | None = None
    llm_temperature: Temperature | None = None
    llm_max_tokens: MaxTokens | None = None
    summary_prompt: SummaryPrompt | None = None
    notes_prompt: NotesPrompt | None = None
    summary_max_transcript_chars: TranscriptChars | None = None
    max_upload_mb: UploadMb | None = None
    max_audio_minutes: AudioMinutes | None = None


class SecretStatus(BaseModel):
    configured: bool
    hint: str | None = Field(None, description="Last 4 characters, to tell keys apart")


class RuntimeSettingsOut(BaseModel):
    """What the admin panel sees: everything except the API keys themselves."""

    transcription_base_url: str
    transcription_api_key: SecretStatus
    transcription_model: str
    transcription_language: str
    transcription_chunk_seconds: int
    llm_base_url: str
    llm_api_key: SecretStatus
    llm_model: str
    llm_temperature: float
    llm_max_tokens: int
    summary_prompt: str
    notes_prompt: str
    summary_max_transcript_chars: int
    max_upload_mb: int
    max_audio_minutes: int

    @classmethod
    def from_settings(cls, runtime: RuntimeSettings) -> "RuntimeSettingsOut":
        data: dict[str, Any] = runtime.model_dump()
        for key in SECRET_FIELDS:
            secret = data[key]
            data[key] = SecretStatus(configured=bool(secret), hint=f"…{secret[-4:]}" if len(secret) >= 8 else None)
        return cls.model_validate(data)


def load_runtime_settings(db: Session, box: SecretBox) -> RuntimeSettings:
    stored: dict[str, Any] = {}
    for row in db.scalars(select(AppSetting)):
        if row.key not in RuntimeSettings.model_fields:
            continue  # left over from an older version
        value = row.value
        if row.key in SECRET_FIELDS:
            value = box.decrypt(value) if isinstance(value, str) else None
            if value is None:
                logger.warning("Stored %s cannot be decrypted (was SECRET_KEY changed?)", row.key)
                continue
        stored[row.key] = value
    try:
        return RuntimeSettings.model_validate(stored)
    except ValidationError:
        # Keep every stored value that is still valid; fall back to defaults for the rest.
        valid = {}
        for key, value in stored.items():
            try:
                RuntimeSettings.model_validate({key: value})
                valid[key] = value
            except ValidationError:
                logger.warning("Ignoring invalid stored setting %s", key)
        return RuntimeSettings.model_validate(valid)


def update_runtime_settings(
    db: Session, box: SecretBox, changes: RuntimeSettingsUpdate, admin_id: uuid.UUID
) -> RuntimeSettings:
    for key in changes.model_fields_set:
        value = getattr(changes, key)
        default = RuntimeSettings.model_fields[key].default
        row = db.get(AppSetting, key)
        if value in (None, "") or value == default:
            if row is not None:
                db.delete(row)  # back to the default (or, for API keys, unset)
            continue
        stored = box.encrypt(value) if key in SECRET_FIELDS else value
        if row is None:
            db.add(AppSetting(key=key, value=stored, updated_by_id=admin_id))
        else:
            row.value = stored
            row.updated_by_id = admin_id
    db.commit()
    return load_runtime_settings(db, box)

from pathlib import Path
from typing import Protocol

from openai import OpenAI

from app.config import Settings


class TranscriptionError(Exception):
    pass


class Transcriber(Protocol):
    def transcribe_file(self, path: Path, language: str | None) -> str:
        """Transcribe one audio file (at most ~25 MB) and return plain text."""
        ...


class OpenAICompatibleTranscriber:
    """Works with any OpenAI-compatible /audio/transcriptions endpoint.

    Defaults to Groq's hosted Whisper large-v3; point TRANSCRIPTION_BASE_URL at
    https://api.openai.com/v1 (model "whisper-1" or "gpt-4o-transcribe") to switch.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client: OpenAI | None = None

    def _get_client(self) -> OpenAI:
        if not self._settings.transcription_api_key:
            raise TranscriptionError("TRANSCRIPTION_API_KEY is not configured on the server")
        if self._client is None:
            self._client = OpenAI(
                base_url=self._settings.transcription_base_url,
                api_key=self._settings.transcription_api_key,
                timeout=self._settings.transcription_timeout_seconds,
                max_retries=3,
            )
        return self._client

    def transcribe_file(self, path: Path, language: str | None) -> str:
        client = self._get_client()
        kwargs = {
            "model": self._settings.transcription_model,
            "response_format": "json",
            "temperature": 0,
        }
        if language:
            kwargs["language"] = language
        with path.open("rb") as audio:
            result = client.audio.transcriptions.create(file=audio, **kwargs)
        return (result.text or "").strip()

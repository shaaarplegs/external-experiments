from pathlib import Path
from typing import Protocol

from openai import OpenAI

from app.runtime_settings import RuntimeSettings

TIMEOUT_SECONDS = 300


class TranscriptionError(Exception):
    pass


class Transcriber(Protocol):
    def transcribe_file(self, path: Path, language: str | None) -> str:
        """Transcribe one audio file (at most ~25 MB) and return plain text."""
        ...


class OpenAICompatibleTranscriber:
    """Works with any OpenAI-compatible /audio/transcriptions endpoint.

    Defaults to Groq's hosted Whisper large-v3; an admin can switch to OpenAI by setting
    the base URL to https://api.openai.com/v1 and the model to "whisper-1".
    """

    def __init__(self, runtime: RuntimeSettings):
        self._runtime = runtime
        self._client: OpenAI | None = None

    def _get_client(self) -> OpenAI:
        if not self._runtime.transcription_api_key:
            raise TranscriptionError(
                "The speech-to-text API key has not been set. An admin can add it in the admin settings."
            )
        if self._client is None:
            self._client = OpenAI(
                base_url=self._runtime.transcription_base_url,
                api_key=self._runtime.transcription_api_key,
                timeout=TIMEOUT_SECONDS,
                max_retries=3,
            )
        return self._client

    def transcribe_file(self, path: Path, language: str | None) -> str:
        client = self._get_client()
        kwargs = {"model": self._runtime.transcription_model, "response_format": "json", "temperature": 0}
        if language:
            kwargs["language"] = language
        with path.open("rb") as audio:
            result = client.audio.transcriptions.create(file=audio, **kwargs)
        return (result.text or "").strip()

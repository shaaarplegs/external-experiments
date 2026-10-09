"""Meeting processing: audio -> transcript -> structured summary + action items."""

import logging
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

import openai
from sqlalchemy.orm import Session

from app.config import Settings
from app.deps import Services
from app.models import (
    PROCESSING_STATUSES,
    ActionItem,
    ActionItemSource,
    Meeting,
    MeetingStatus,
    as_aware,
    utcnow,
)
from app.schemas import LLMMeetingSummary, MeetingSummaryContent
from app.services.audio import AudioError, iter_chunks
from app.services.summarizer import MeetingContext, SummarizerError
from app.services.transcription import Transcriber, TranscriptionError

logger = logging.getLogger(__name__)

INTERRUPTED_MESSAGE = "Processing was interrupted. Please retry."


class NoSpeechError(Exception):
    pass


def mark_queued(meeting: Meeting) -> None:
    meeting.status = MeetingStatus.QUEUED
    meeting.error_message = None
    meeting.processing_started_at = utcnow()


def is_processing(meeting: Meeting) -> bool:
    return meeting.status in PROCESSING_STATUSES


def expire_stale(db: Session, meetings: list[Meeting], settings: Settings) -> None:
    """Jobs run in-process; if the instance died mid-job, report the meeting as failed."""
    cutoff = utcnow() - timedelta(minutes=settings.processing_timeout_minutes)
    changed = False
    for meeting in meetings:
        started = meeting.processing_started_at
        if is_processing(meeting) and started and as_aware(started) < cutoff:
            meeting.status = MeetingStatus.FAILED
            meeting.error_message = INTERRUPTED_MESSAGE
            changed = True
    if changed:
        db.commit()


def transcribe_audio(
    transcriber: Transcriber, source: Path, work_dir: Path, language: str | None, chunk_seconds: int
) -> str:
    texts = []
    for index, chunk in enumerate(iter_chunks(source, work_dir / "chunks", chunk_seconds), start=1):
        logger.info("Transcribing chunk %s", index)
        text = transcriber.transcribe_file(chunk, language)
        chunk.unlink()
        if text:
            texts.append(text)
    return "\n".join(texts)


def build_context(meeting: Meeting) -> MeetingContext:
    attendees = [f"{a.name} ({a.role})" if a.role else a.name for a in meeting.attendees]
    return MeetingContext(
        title=meeting.title,
        meeting_date=as_aware(meeting.meeting_date).date() if meeting.meeting_date else None,
        category=meeting.category.name if meeting.category else None,
        attendees=attendees,
        duration_minutes=round(meeting.duration_seconds / 60) if meeting.duration_seconds else None,
    )


def apply_summary(meeting: Meeting, summary: LLMMeetingSummary) -> None:
    content = MeetingSummaryContent.model_validate(summary.model_dump(exclude={"action_items"}))
    meeting.summary = content.model_dump(mode="json")
    if not meeting.title and summary.title.strip():
        meeting.title = summary.title.strip()[:300]

    # Re-summarizing replaces the AI-generated items; items the user added stay.
    user_items = [item for item in meeting.action_items if item.source == ActionItemSource.USER]
    ai_items = [
        ActionItem(
            description=item.task.strip(),
            assignee=(item.owner or "").strip()[:200] or None,
            due_date=item.due_date,
            due_text=(item.due_text or "").strip()[:200] or None,
            priority=item.priority,
            source=ActionItemSource.AI,
        )
        for item in summary.action_items
        if item.task.strip()
    ]
    meeting.action_items = ai_items + user_items
    for position, item in enumerate(meeting.action_items):
        item.position = position


def describe_failure(exc: Exception, stage: str) -> str:
    provider = "The speech-to-text service" if stage == "transcription" else "The summary service"
    if isinstance(exc, (AudioError, NoSpeechError, TranscriptionError, SummarizerError)):
        return str(exc)
    if isinstance(exc, openai.AuthenticationError):
        return f"{provider} rejected the server's API key."
    if isinstance(exc, openai.RateLimitError):
        return f"{provider} is rate limited or out of quota. Please retry later."
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return f"{provider} could not be reached. Please retry."
    if isinstance(exc, openai.APIStatusError):
        return f"{provider} returned an error (HTTP {exc.status_code}). Please retry."
    return f"Unexpected error during {stage}. Please retry."


def run_pipeline(services: Services, meeting_id: uuid.UUID, audio_path: Path | None) -> None:
    """Background job. With audio: transcribe then summarize. Without: re-summarize."""
    settings = services.settings
    stage = "transcription" if audio_path else "summary"
    db = services.session_factory()
    try:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            return

        if audio_path is not None:
            meeting.status = MeetingStatus.TRANSCRIBING
            db.commit()
            transcript = transcribe_audio(
                services.transcriber,
                audio_path,
                audio_path.parent,
                meeting.language,
                settings.transcription_chunk_seconds,
            )
            if not transcript.strip():
                raise NoSpeechError("No speech was detected in the recording.")
            meeting.transcript = transcript
            db.commit()

        if not (meeting.transcript or "").strip():
            raise NoSpeechError("This meeting has no transcript to summarize.")

        stage = "summary"
        meeting.status = MeetingStatus.SUMMARIZING
        db.commit()
        summary = services.summarizer.summarize(meeting.transcript, build_context(meeting))
        apply_summary(meeting, summary)
        meeting.status = MeetingStatus.COMPLETED
        meeting.error_message = None
        meeting.processed_at = utcnow()
        db.commit()
        logger.info("Meeting %s processed", meeting_id)
    except Exception as exc:
        logger.exception("Meeting %s failed during %s", meeting_id, stage)
        db.rollback()
        meeting = db.get(Meeting, meeting_id)
        if meeting is not None:  # it may have been deleted meanwhile
            meeting.status = MeetingStatus.FAILED
            meeting.error_message = describe_failure(exc, stage)
            db.commit()
    finally:
        db.close()
        if audio_path is not None:
            shutil.rmtree(audio_path.parent, ignore_errors=True)

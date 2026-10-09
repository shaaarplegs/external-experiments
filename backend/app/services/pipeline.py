"""Meeting processing: audio -> transcript -> structured summary + action items."""

import logging
import shutil
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import openai
from sqlalchemy import case, func, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.deps import Services
from app.models import (
    PROCESSING_STATUSES,
    ActionItem,
    ActionItemSource,
    Meeting,
    MeetingStatus,
    ProcessingRun,
    RunKind,
    RunStatus,
    utcnow,
)
from app.runtime_settings import RuntimeSettings
from app.schemas import LLMMeetingSummary, MeetingSummaryContent
from app.services.audio import AudioError, iter_chunks, normalize
from app.services.summarizer import MeetingContext, Summarizer, SummarizerError
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


def _fail_running_runs(db: Session, *conditions) -> None:
    """Close RUNNING runs as failed. If audio was already sent to the speech-to-text API the
    run failed after transcription (and still counts toward quotas)."""
    db.execute(
        update(ProcessingRun)
        .where(ProcessingRun.status == RunStatus.RUNNING, *conditions)
        .values(
            status=RunStatus.FAILED,
            failed_stage=case(
                (func.coalesce(ProcessingRun.transcribed_seconds, 0) > 0, "summary"), else_="transcription"
            ),
            finished_at=utcnow(),
        )
    )


def close_runs_for_meeting(db: Session, meeting_id: uuid.UUID) -> None:
    """Before a meeting is deleted: its usage log entries must not stay "running" forever.
    If the job is in fact still alive, it records the real outcome when it ends."""
    _fail_running_runs(db, ProcessingRun.meeting_id == meeting_id)


def expire_stale(db: Session, meetings: list[Meeting], settings: Settings) -> None:
    """Jobs run in-process; if the instance died mid-job, report the meeting as failed."""
    cutoff = utcnow() - timedelta(minutes=settings.processing_timeout_minutes)
    changed = False
    for meeting in meetings:
        started = meeting.processing_started_at
        if is_processing(meeting) and started and started < cutoff:
            _fail_running_runs(db, ProcessingRun.meeting_id == meeting.id)
            meeting.status = MeetingStatus.FAILED
            meeting.error_message = INTERRUPTED_MESSAGE
            changed = True
    if changed:
        db.commit()


def sweep_stale(db: Session, settings: Settings) -> None:
    """expire_stale for everything at once: run before counting usage (quotas, dashboard), so
    jobs that died with their instance don't count as running forever."""
    cutoff = utcnow() - timedelta(minutes=settings.processing_timeout_minutes)
    _fail_running_runs(db, ProcessingRun.created_at < cutoff)
    db.execute(
        update(Meeting)
        .where(Meeting.status.in_(PROCESSING_STATUSES), Meeting.processing_started_at < cutoff)
        .values(status=MeetingStatus.FAILED, error_message=INTERRUPTED_MESSAGE)
    )
    db.commit()


def transcribe_audio(
    transcriber: Transcriber,
    source: Path,
    work_dir: Path,
    language: str | None,
    chunk_seconds: int,
    on_chunk_done: Callable[[float], None] | None = None,
) -> tuple[str, float]:
    """Returns the transcript and the recording's length in seconds. on_chunk_done gets the
    seconds of audio in each chunk the API has transcribed (i.e. billed)."""
    audio = normalize(source, work_dir / "chunks")
    texts = []
    for index, chunk in enumerate(iter_chunks(audio, chunk_seconds)):
        logger.info("Transcribing chunk %s", index + 1)
        text = transcriber.transcribe_file(chunk, language)
        chunk.unlink()
        if on_chunk_done is not None:
            on_chunk_done(min(chunk_seconds, audio.duration_seconds - index * chunk_seconds))
        if text:
            texts.append(text)
    return "\n".join(texts), audio.duration_seconds


def build_context(meeting: Meeting) -> MeetingContext:
    attendees = [f"{a.name} ({a.role})" if a.role else a.name for a in meeting.attendees]
    return MeetingContext(
        title=meeting.title,
        meeting_date=meeting.meeting_date.date() if meeting.meeting_date else None,
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


def finish_run(
    services: Services,
    run_id: uuid.UUID,
    status: RunStatus,
    failed_stage: str | None,
    runtime: RuntimeSettings | None,
    audio_seconds: float | None,
    summarizer: Summarizer | None,
) -> None:
    """Record the outcome in the usage log (in its own session, whatever happened above)."""
    with services.session_factory() as db:
        run = db.get(ProcessingRun, run_id)
        if run is None:
            return
        run.status = status
        run.failed_stage = failed_stage
        run.finished_at = utcnow()
        if audio_seconds is not None:
            run.audio_seconds = audio_seconds
        if runtime is not None:
            run.llm_model = runtime.llm_model
            if run.kind != RunKind.RESUMMARIZE:
                run.transcription_model = runtime.transcription_model
        if summarizer is not None:
            run.llm_prompt_tokens, run.llm_completion_tokens = summarizer.tokens_used
        db.commit()


def run_pipeline(services: Services, meeting_id: uuid.UUID, run_id: uuid.UUID, audio_path: Path | None) -> None:
    """Background job. With audio: transcribe then summarize. Without: re-summarize."""
    stage = "transcription" if audio_path else "summary"
    status, runtime, audio_seconds, summarizer = RunStatus.FAILED, None, None, None
    db = services.session_factory()
    try:
        meeting = db.get(Meeting, meeting_id)
        if meeting is None:
            return  # deleted before the job started
        # The admin settings as they are now: keys, models and prompt changes apply to the next job.
        runtime = services.load_runtime_settings(db)

        if audio_path is not None:
            meeting.status = MeetingStatus.TRANSCRIBING
            db.commit()
            run = db.get(ProcessingRun, run_id)

            def record_progress(seconds: float) -> None:
                # Committed per chunk, so audio the API billed is counted even if the
                # instance dies before the job finishes.
                if run is not None:
                    run.transcribed_seconds = (run.transcribed_seconds or 0) + seconds
                    db.commit()

            transcript, audio_seconds = transcribe_audio(
                services.make_transcriber(runtime),
                audio_path,
                audio_path.parent,
                meeting.language,
                runtime.transcription_chunk_seconds,
                record_progress,
            )
            if not transcript.strip():
                raise NoSpeechError("No speech was detected in the recording.")
            meeting.transcript = transcript
            if meeting.duration_seconds is None:
                meeting.duration_seconds = round(audio_seconds)
            db.commit()

        if not (meeting.transcript or "").strip():
            raise NoSpeechError("This meeting has no transcript to summarize.")

        stage = "summary"
        meeting.status = MeetingStatus.SUMMARIZING
        db.commit()
        summarizer = services.make_summarizer(runtime)
        apply_summary(meeting, summarizer.summarize(meeting.transcript, build_context(meeting)))
        meeting.status = MeetingStatus.COMPLETED
        meeting.error_message = None
        meeting.processed_at = utcnow()
        db.commit()
        status = RunStatus.COMPLETED
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
        failed_stage = None if status == RunStatus.COMPLETED else stage
        finish_run(services, run_id, status, failed_stage, runtime, audio_seconds, summarizer)

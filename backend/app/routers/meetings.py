import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.deps import AppServices, AppSettings, CurrentUser, DbSession
from app.exports.pdf import render_pdf
from app.exports.view import build_view, export_filename
from app.exports.word import render_docx
from app.models import (
    ActionItem,
    ActionItemSource,
    Meeting,
    MeetingAttendee,
    MeetingStatus,
    RunKind,
    User,
)
from app.routers.categories import get_owned_category
from app.runtime_settings import RuntimeSettings
from app.schemas import (
    ActionItemCreate,
    ActionItemOut,
    ActionItemUpdate,
    AttendeeIn,
    CategoryRef,
    MeetingDetail,
    MeetingListItem,
    MeetingPage,
    MeetingUpdate,
    UTCDateTime,
)
from app.services.audio import AudioError, measure_duration, probe
from app.services.pipeline import (
    expire_stale,
    is_processing,
    mark_queued,
    run_pipeline,
    sweep_stale,
)
from app.services.usage import QuotaExceeded, check_quota, start_run

router = APIRouter(prefix="/meetings", tags=["meetings"])

_attendee_list = TypeAdapter(list[AttendeeIn])
_LANGUAGE = re.compile(r"^[a-z]{2,3}$")
_LOAD = (selectinload(Meeting.attendees), selectinload(Meeting.action_items), selectinload(Meeting.category))


# ------------------------------------------------------------------- helpers


def get_owned_meeting(db: Session, user: User, meeting_id: uuid.UUID) -> Meeting:
    meeting = db.get(Meeting, meeting_id, options=_LOAD)
    if meeting is None or meeting.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    return meeting


def to_list_item(meeting: Meeting) -> MeetingListItem:
    summary = meeting.summary or {}
    return MeetingListItem(
        id=meeting.id,
        title=meeting.title,
        category=CategoryRef.model_validate(meeting.category) if meeting.category else None,
        meeting_date=meeting.meeting_date,
        duration_seconds=meeting.duration_seconds,
        status=meeting.status,
        error_message=meeting.error_message,
        attendee_count=len(meeting.attendees),
        action_items_total=len(meeting.action_items),
        action_items_done=sum(1 for item in meeting.action_items if item.is_done),
        overview=summary.get("overview") or None,
        created_at=meeting.created_at,
        updated_at=meeting.updated_at,
    )


def to_detail(meeting: Meeting) -> MeetingDetail:
    return MeetingDetail.model_validate(meeting)


def parse_attendees(raw: str | None) -> list[AttendeeIn]:
    if not raw or not raw.strip():
        return []
    try:
        return _attendee_list.validate_python(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            'attendees must be a JSON list like [{"name": "Ahmed", "role": "CFO"}]',
        ) from exc


def resolve_language(raw: str | None, runtime: RuntimeSettings) -> str | None:
    if raw is None or not raw.strip():
        return runtime.language_hint
    value = raw.strip().lower()
    if value == "auto":
        return None
    if not _LANGUAGE.match(value):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            'language must be an ISO 639-1 code such as "ar" or "en", or "auto"',
        )
    return value


def set_attendees(meeting: Meeting, attendees: list[AttendeeIn]) -> None:
    meeting.attendees = [
        MeetingAttendee(name=a.name, email=a.email, role=a.role or None, position=index)
        for index, a in enumerate(attendees)
    ]


def save_upload(upload: UploadFile, work_dir: Path, runtime: RuntimeSettings) -> tuple[Path, float | None]:
    """Copy the upload to a private work dir and check that it really contains audio."""
    job_dir = work_dir / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.filename or "").suffix.lower()
    path = job_dir / f"source{suffix if re.fullmatch(r'\.[a-z0-9]{1,5}', suffix) else ''}"
    try:
        with path.open("wb") as out:
            shutil.copyfileobj(upload.file, out, length=1024 * 1024)
        size = path.stat().st_size
        if size == 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The uploaded file is empty")
        if size > runtime.max_upload_mb * 1024 * 1024:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE, f"Upload is larger than {runtime.max_upload_mb} MB"
            )
        try:
            info = probe(path)
        except AudioError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        if not info.has_audio:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The file has no audio track")
        if not info.duration_seconds:
            # Limits and quotas need the length; measure it if the header doesn't say.
            try:
                info.duration_seconds = measure_duration(path)
            except AudioError:  # e.g. decoding timed out
                info.duration_seconds = None
        if not info.duration_seconds:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Could not determine the length of the recording"
            )
        if info.duration_seconds > runtime.max_audio_minutes * 60:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Recordings longer than {runtime.max_audio_minutes} minute{'' if runtime.max_audio_minutes == 1 else 's'} are not supported",
            )
        return path, info.duration_seconds
    except BaseException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise


def enforce_quota(db: Session, user: User, runtime: RuntimeSettings, audio_seconds: float | None) -> None:
    """Locks the user's row until the caller commits, so concurrent uploads are counted in turn."""
    db.execute(select(User.id).where(User.id == user.id).with_for_update())
    try:
        check_quota(db, user, runtime, audio_seconds)
    except QuotaExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc


def precheck_quota(db: Session, user: User, runtime: RuntimeSettings, settings: Settings) -> None:
    """Cheap check before receiving the file: is any upload (and any minute) left this month?"""
    sweep_stale(db, settings)  # jobs that died with their instance must not count as running
    try:
        check_quota(db, user, runtime, None)
    except QuotaExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc


def ensure_idle(meeting: Meeting) -> None:
    if is_processing(meeting):
        raise HTTPException(status.HTTP_409_CONFLICT, "This meeting is still being processed")


def lock_idle_meeting(db: Session, meeting: Meeting) -> None:
    """Lock the meeting row and re-check it, so two retries sent at once start one job."""
    db.execute(select(Meeting.id).where(Meeting.id == meeting.id).with_for_update())
    db.refresh(meeting)
    ensure_idle(meeting)


# ------------------------------------------------------------------ meetings


@router.post("", response_model=MeetingDetail, status_code=status.HTTP_202_ACCEPTED)
def create_meeting(
    audio: Annotated[UploadFile, File(description="Recording: mp4, m4a, mp3, wav, ogg, webm, 3gp, ...")],
    background: BackgroundTasks,
    user: CurrentUser,
    db: DbSession,
    services: AppServices,
    title: Annotated[str | None, Form(max_length=300)] = None,
    category_id: Annotated[uuid.UUID | None, Form()] = None,
    meeting_date: Annotated[UTCDateTime | None, Form(description="ISO 8601; defaults to now")] = None,
    duration_seconds: Annotated[int | None, Form(ge=0, description="Defaults to the audio length")] = None,
    attendees: Annotated[str | None, Form(description='JSON list: [{"name": "Ahmed", "role": "CFO"}]')] = None,
    language: Annotated[str | None, Form(description='Speech language hint ("ar", "en"), or "auto"')] = None,
):
    """Upload a recording. Returns immediately with status "queued"; poll GET /meetings/{id}
    until status is "completed" (or "failed")."""
    runtime = services.load_runtime_settings(db)
    attendee_list = parse_attendees(attendees)
    language_hint = resolve_language(language, runtime)
    if category_id is not None:
        get_owned_category(db, user, category_id)
    precheck_quota(db, user, runtime, services.settings)

    audio_path, audio_seconds = save_upload(audio, services.settings.work_dir, runtime)
    try:
        enforce_quota(db, user, runtime, audio_seconds)
        meeting = Meeting(
            owner_id=user.id,
            title=(title or "").strip() or None,
            category_id=category_id,
            duration_seconds=duration_seconds
            if duration_seconds is not None
            else (round(audio_seconds) if audio_seconds else None),
            language=language_hint,
            source_filename=(audio.filename or "")[:255] or None,
        )
        if meeting_date is not None:
            meeting.meeting_date = meeting_date
        set_attendees(meeting, attendee_list)
        mark_queued(meeting)
        db.add(meeting)
        db.flush()
        run = start_run(
            db, user_id=user.id, meeting_id=meeting.id, kind=RunKind.UPLOAD, audio_seconds=audio_seconds
        )
        db.commit()
    except BaseException:
        db.rollback()
        shutil.rmtree(audio_path.parent, ignore_errors=True)
        raise
    background.add_task(run_pipeline, services, meeting.id, run.id, audio_path)
    return to_detail(get_owned_meeting(db, user, meeting.id))


@router.get("", response_model=MeetingPage)
def list_meetings(
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
    q: Annotated[str | None, Query(max_length=200, description="Search title and transcript")] = None,
    category_id: uuid.UUID | None = None,
    uncategorized: bool = False,
    status_filter: Annotated[MeetingStatus | None, Query(alias="status")] = None,
    date_from: UTCDateTime | None = None,
    date_to: UTCDateTime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    filters = [Meeting.owner_id == user.id]
    if q and q.strip():
        term = q.strip()
        filters.append(
            or_(
                Meeting.title.icontains(term, autoescape=True),
                Meeting.transcript.icontains(term, autoescape=True),
            )
        )
    if uncategorized:
        filters.append(Meeting.category_id.is_(None))
    elif category_id is not None:
        filters.append(Meeting.category_id == category_id)
    if status_filter is not None:
        filters.append(Meeting.status == status_filter)
    if date_from is not None:
        filters.append(Meeting.meeting_date >= date_from)
    if date_to is not None:
        filters.append(Meeting.meeting_date <= date_to)

    total = db.scalar(select(func.count()).select_from(Meeting).where(*filters))
    meetings = list(
        db.scalars(
            select(Meeting)
            .where(*filters)
            .options(*_LOAD)
            .order_by(Meeting.meeting_date.desc(), Meeting.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    expire_stale(db, meetings, settings)
    return MeetingPage(items=[to_list_item(m) for m in meetings], total=total, limit=limit, offset=offset)


@router.get("/{meeting_id}", response_model=MeetingDetail)
def get_meeting(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession, settings: AppSettings):
    meeting = get_owned_meeting(db, user, meeting_id)
    expire_stale(db, [meeting], settings)
    return to_detail(meeting)


@router.patch("/{meeting_id}", response_model=MeetingDetail)
def update_meeting(meeting_id: uuid.UUID, body: MeetingUpdate, user: CurrentUser, db: DbSession):
    meeting = get_owned_meeting(db, user, meeting_id)
    fields = body.model_fields_set
    if "title" in fields:
        meeting.title = (body.title or "").strip() or None
    if "category_id" in fields:
        if body.category_id is not None:
            get_owned_category(db, user, body.category_id)
        meeting.category_id = body.category_id
    if "meeting_date" in fields:
        if body.meeting_date is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "meeting_date cannot be null")
        meeting.meeting_date = body.meeting_date
    if "duration_seconds" in fields:
        meeting.duration_seconds = body.duration_seconds
    if "attendees" in fields:
        set_attendees(meeting, body.attendees or [])
    if "summary" in fields:
        meeting.summary = body.summary.model_dump(mode="json") if body.summary else None
    db.commit()
    db.expire(meeting)
    return to_detail(get_owned_meeting(db, user, meeting_id))


@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_meeting(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession):
    meeting = get_owned_meeting(db, user, meeting_id)
    db.delete(meeting)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{meeting_id}/audio", response_model=MeetingDetail, status_code=status.HTTP_202_ACCEPTED)
def replace_audio(
    meeting_id: uuid.UUID,
    audio: Annotated[UploadFile, File()],
    background: BackgroundTasks,
    user: CurrentUser,
    db: DbSession,
    services: AppServices,
    language: Annotated[str | None, Form()] = None,
):
    """Upload the recording again (e.g. after a failed transcription) and reprocess it."""
    meeting = get_owned_meeting(db, user, meeting_id)
    expire_stale(db, [meeting], services.settings)
    ensure_idle(meeting)
    runtime = services.load_runtime_settings(db)
    language_hint = resolve_language(language, runtime) if language is not None else meeting.language
    precheck_quota(db, user, runtime, services.settings)
    audio_path, audio_seconds = save_upload(audio, services.settings.work_dir, runtime)
    try:
        lock_idle_meeting(db, meeting)
        enforce_quota(db, user, runtime, audio_seconds)
        meeting.language = language_hint
        meeting.duration_seconds = round(audio_seconds) if audio_seconds else None
        meeting.source_filename = (audio.filename or "")[:255] or None
        mark_queued(meeting)
        run = start_run(
            db, user_id=user.id, meeting_id=meeting.id, kind=RunKind.REUPLOAD, audio_seconds=audio_seconds
        )
        db.commit()
    except BaseException:
        db.rollback()
        shutil.rmtree(audio_path.parent, ignore_errors=True)
        raise
    background.add_task(run_pipeline, services, meeting.id, run.id, audio_path)
    return to_detail(meeting)


@router.post("/{meeting_id}/summarize", response_model=MeetingDetail, status_code=status.HTTP_202_ACCEPTED)
def resummarize(meeting_id: uuid.UUID, background: BackgroundTasks, user: CurrentUser, db: DbSession, services: AppServices):
    """Regenerate the summary from the stored transcript (e.g. after changing the prompt).
    AI-generated action items are replaced; ones the user added are kept."""
    meeting = get_owned_meeting(db, user, meeting_id)
    expire_stale(db, [meeting], services.settings)
    ensure_idle(meeting)
    if not (meeting.transcript or "").strip():
        raise HTTPException(status.HTTP_409_CONFLICT, "This meeting has no transcript yet")
    lock_idle_meeting(db, meeting)
    mark_queued(meeting)
    run = start_run(db, user_id=user.id, meeting_id=meeting.id, kind=RunKind.RESUMMARIZE)
    db.commit()
    background.add_task(run_pipeline, services, meeting.id, run.id, None)
    return to_detail(meeting)


# --------------------------------------------------------------- action items


def get_owned_item(meeting: Meeting, item_id: uuid.UUID) -> ActionItem:
    for item in meeting.action_items:
        if item.id == item_id:
            return item
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Action item not found")


@router.post(
    "/{meeting_id}/action-items", response_model=ActionItemOut, status_code=status.HTTP_201_CREATED
)
def add_action_item(meeting_id: uuid.UUID, body: ActionItemCreate, user: CurrentUser, db: DbSession):
    meeting = get_owned_meeting(db, user, meeting_id)
    item = ActionItem(
        description=body.description,
        assignee=body.assignee,
        due_date=body.due_date,
        priority=body.priority,
        is_done=body.is_done,
        source=ActionItemSource.USER,
        position=len(meeting.action_items),
    )
    meeting.action_items.append(item)
    db.commit()
    return item


@router.patch("/{meeting_id}/action-items/{item_id}", response_model=ActionItemOut)
def update_action_item(
    meeting_id: uuid.UUID, item_id: uuid.UUID, body: ActionItemUpdate, user: CurrentUser, db: DbSession
):
    item = get_owned_item(get_owned_meeting(db, user, meeting_id), item_id)
    fields = body.model_fields_set
    if "description" in fields and body.description is not None:
        item.description = body.description
    if "is_done" in fields and body.is_done is not None:
        item.is_done = body.is_done
    for name in ("assignee", "due_date", "priority"):
        if name in fields:
            setattr(item, name, getattr(body, name))
    if "due_date" in fields:
        item.due_text = None  # the user picked a concrete date; drop the spoken wording
    db.commit()
    return item


@router.delete("/{meeting_id}/action-items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_action_item(meeting_id: uuid.UUID, item_id: uuid.UUID, user: CurrentUser, db: DbSession):
    meeting = get_owned_meeting(db, user, meeting_id)
    meeting.action_items.remove(get_owned_item(meeting, item_id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------- export

EXPORT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def content_disposition(filename: str) -> str:
    stem, _, ext = filename.rpartition(".")
    ascii_name = re.sub(r"[^A-Za-z0-9 _.-]+", "", stem).strip() or "meeting"
    return f"attachment; filename=\"{ascii_name}.{ext}\"; filename*=UTF-8''{quote(filename)}"


def export_response(meeting: Meeting, format: Literal["pdf", "docx"], include_transcript: bool) -> Response:
    view = build_view(meeting, include_transcript=include_transcript)
    content = render_pdf(view) if format == "pdf" else render_docx(view)
    return Response(
        content=content,
        media_type=EXPORT_TYPES[format],
        headers={"Content-Disposition": content_disposition(export_filename(view, format))},
    )


EXPORT_RESPONSES = {200: {"content": {mime: {} for mime in EXPORT_TYPES.values()}}}


@router.get("/{meeting_id}/export", response_class=Response, responses=EXPORT_RESPONSES)
def export_meeting(
    meeting_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    format: Literal["pdf", "docx"] = "pdf",
    include_transcript: bool = False,
):
    return export_response(get_owned_meeting(db, user, meeting_id), format, include_transcript)

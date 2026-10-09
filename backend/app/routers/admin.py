"""Admin API: settings, dashboard, accounts, all meetings, and the audit log.

Admins have full access to every meeting (including transcripts) so they can act on misuse,
for example by revoking an account. Every action that reads private content or changes an
account or setting is written to the audit log.
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.deps import AppServices, AppSettings, CurrentAdmin, DbSession
from app.models import AdminAuditLog, Meeting, MeetingStatus, User, UserRole, utcnow
from app.routers.auth import revoke_all_refresh_tokens
from app.routers.meetings import (
    EXPORT_RESPONSES,
    export_response,
    to_detail,
    to_list_item,
)
from app.runtime_settings import (
    SECRET_FIELDS,
    RuntimeSettingsOut,
    RuntimeSettingsUpdate,
    update_runtime_settings,
)
from app.schemas import (
    AdminMeetingDetail,
    AdminMeetingListItem,
    AdminMeetingPage,
    AdminUserOut,
    AdminUserPage,
    AdminUserUpdate,
    AuditLogEntry,
    AuditLogPage,
    Dashboard,
    OwnerRef,
    UTCDateTime,
)
from app.services.dashboard import build_dashboard
from app.services.pipeline import close_runs_for_meeting, expire_stale, sweep_stale
from app.services.usage import usage_by_user

router = APIRouter(prefix="/admin", tags=["admin"])

_MEETING_LOAD = (
    selectinload(Meeting.attendees),
    selectinload(Meeting.action_items),
    selectinload(Meeting.category),
    selectinload(Meeting.owner),
)


def audit(
    db: Session,
    admin: User,
    action: str,
    target_type: str | None = None,
    target_id: Any = None,
    details: dict | None = None,
) -> None:
    """Add an audit entry; it is saved with the caller's commit."""
    db.add(
        AdminAuditLog(
            admin_id=admin.id,
            admin_email=admin.email,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            details=details,
        )
    )


# ------------------------------------------------------------------- settings


@router.get("/settings", response_model=RuntimeSettingsOut)
def get_settings(admin: CurrentAdmin, db: DbSession, services: AppServices):
    """Global technical settings: provider URLs, models, API keys (never returned, only
    whether they are set), the summary prompt, upload limits, quotas and prices."""
    return RuntimeSettingsOut.from_settings(services.load_runtime_settings(db))


@router.patch("/settings", response_model=RuntimeSettingsOut)
def update_settings(body: RuntimeSettingsUpdate, admin: CurrentAdmin, db: DbSession, services: AppServices):
    """Change only the fields sent. Applies to the next upload or summary on every instance."""
    changed = sorted(body.model_fields_set)
    audit(db, admin, "settings.update", details={"fields": changed, "secrets_changed": sorted(SECRET_FIELDS & set(changed))})
    return RuntimeSettingsOut.from_settings(update_runtime_settings(db, services.secret_box, body, admin.id))


# ------------------------------------------------------------------ dashboard


@router.get("/dashboard", response_model=Dashboard)
def get_dashboard(
    admin: CurrentAdmin,
    db: DbSession,
    services: AppServices,
    days: Annotated[int, Query(ge=1, le=366, description="Period: the last N days, including today")] = 30,
):
    """KPIs for the period: users, meetings, usage (uploads, minutes, tokens, failures,
    estimated cost), a daily series, and what the app is used for (categories, topics,
    languages), plus the heaviest users."""
    sweep_stale(db, services.settings)  # jobs that died with their instance count as failed
    return build_dashboard(db, services.load_runtime_settings(db), days)


# ---------------------------------------------------------------------- users


def _meeting_counts(db: Session, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    rows = db.execute(
        select(Meeting.owner_id, func.count()).where(Meeting.owner_id.in_(user_ids)).group_by(Meeting.owner_id)
    )
    return dict(rows.all())


def _users_out(db: Session, users: list[User]) -> list[AdminUserOut]:
    ids = [u.id for u in users]
    counts, usage = _meeting_counts(db, ids), usage_by_user(db, ids)
    return [
        AdminUserOut.model_validate(
            {
                "id": u.id,
                "email": u.email,
                "full_name": u.full_name,
                "role": u.role,
                "created_at": u.created_at,
                "is_active": u.is_active,
                "disabled_reason": u.disabled_reason,
                "disabled_at": u.disabled_at,
                "last_login_at": u.last_login_at,
                "last_active_at": u.last_active_at,
                "meeting_count": counts.get(u.id, 0),
                "uploads_this_month": usage.get(u.id, (0, 0.0))[0],
                "audio_minutes_this_month": usage.get(u.id, (0, 0.0))[1],
                "quota_uploads_per_month": u.quota_uploads_per_month,
                "quota_audio_minutes_per_month": u.quota_audio_minutes_per_month,
            }
        )
        for u in users
    ]


def ensure_not_last_admin(db: Session, user: User) -> None:
    """Refuse changes that would leave nobody able to manage the app."""
    if not (user.is_admin and user.is_active):
        return
    other_admins = db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == UserRole.ADMIN, User.is_active.is_(True), User.id != user.id)
    )
    if not other_admins:
        raise HTTPException(status.HTTP_409_CONFLICT, "At least one active admin is required")


def _get_user(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


@router.get("/users", response_model=AdminUserPage)
def list_users(
    admin: CurrentAdmin,
    db: DbSession,
    settings: AppSettings,
    q: Annotated[str | None, Query(max_length=200, description="Search email and name")] = None,
    role: Literal["user", "admin"] | None = None,
    is_active: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    filters = []
    if q and q.strip():
        term = q.strip()
        filters.append(or_(User.email.icontains(term, autoescape=True), User.full_name.icontains(term, autoescape=True)))
    if role is not None:
        filters.append(User.role == role)
    if is_active is not None:
        filters.append(User.is_active.is_(is_active))

    sweep_stale(db, settings)  # so dead jobs don't inflate "uploads this month"
    total = db.scalar(select(func.count()).select_from(User).where(*filters))
    users = list(
        db.scalars(select(User).where(*filters).order_by(User.created_at.desc()).limit(limit).offset(offset))
    )
    return AdminUserPage(items=_users_out(db, users), total=total, limit=limit, offset=offset)


@router.get("/users/{user_id}", response_model=AdminUserOut)
def get_user(user_id: uuid.UUID, admin: CurrentAdmin, db: DbSession, settings: AppSettings):
    sweep_stale(db, settings)
    return _users_out(db, [_get_user(db, user_id)])[0]


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def update_user(user_id: uuid.UUID, body: AdminUserUpdate, admin: CurrentAdmin, db: DbSession):
    """Change a user's role, revoke or restore access (with a reason shown to the user at
    login), set per-user monthly quotas, or fix the name. Accounts can't be deleted here."""
    user = _get_user(db, user_id)
    fields = body.model_fields_set
    if ("role" in fields and body.role == UserRole.USER) or ("is_active" in fields and body.is_active is False):
        ensure_not_last_admin(db, user)
    revoking = "is_active" in fields and body.is_active is False and user.is_active
    if revoking:
        # Tokens first, then the user row: the same lock order as /auth/refresh (no deadlock).
        revoke_all_refresh_tokens(db, user.id)  # signed out everywhere, immediately

    details: dict[str, Any] = {}
    if "role" in fields and body.role is not None and body.role != user.role:
        details["role"] = [user.role, body.role]
        user.role = body.role
    if "full_name" in fields:
        user.full_name = body.full_name
        details["full_name"] = True
    for name in ("quota_uploads_per_month", "quota_audio_minutes_per_month"):
        if name in fields:
            details[name] = [getattr(user, name), getattr(body, name)]
            setattr(user, name, getattr(body, name))
    if "is_active" in fields and body.is_active is not None and body.is_active != user.is_active:
        user.is_active = body.is_active
        if body.is_active:
            user.disabled_reason, user.disabled_at = None, None
            details["access"] = "restored"
        else:
            user.disabled_reason, user.disabled_at = body.disabled_reason or None, utcnow()
            details["access"] = "revoked"
            details["reason"] = user.disabled_reason
    elif "disabled_reason" in fields and not user.is_active:
        user.disabled_reason = body.disabled_reason or None
        details["reason"] = user.disabled_reason

    if details:
        audit(db, admin, "user.update", "user", user.id, {"email": user.email, **details})
    db.commit()
    return _users_out(db, [user])[0]


# ------------------------------------------------------------------- meetings


def _owner(meeting: Meeting) -> OwnerRef:
    owner = meeting.owner
    return OwnerRef(id=owner.id, email=owner.email, full_name=owner.full_name, is_active=owner.is_active)


def _get_meeting(db: Session, meeting_id: uuid.UUID) -> Meeting:
    meeting = db.get(Meeting, meeting_id, options=_MEETING_LOAD)
    if meeting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    return meeting


@router.get("/meetings", response_model=AdminMeetingPage)
def list_all_meetings(
    admin: CurrentAdmin,
    db: DbSession,
    settings: AppSettings,
    user_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=200, description="Search title and transcript")] = None,
    status_filter: Annotated[MeetingStatus | None, Query(alias="status")] = None,
    date_from: UTCDateTime | None = None,
    date_to: UTCDateTime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Every user's meetings, newest first (by meeting date)."""
    filters = []
    if user_id is not None:
        filters.append(Meeting.owner_id == user_id)
    if q and q.strip():
        term = q.strip()
        filters.append(or_(Meeting.title.icontains(term, autoescape=True), Meeting.transcript.icontains(term, autoescape=True)))
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
            .options(*_MEETING_LOAD)
            .order_by(Meeting.meeting_date.desc(), Meeting.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    expire_stale(db, meetings, settings)
    # The list shows titles and summary overviews (and can search transcripts), so it is audited.
    filters_used = {
        "user_id": str(user_id) if user_id else None,
        "q": q or None,
        "status": status_filter,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
    }
    audit(
        db, admin, "meeting.list", details={
            "filters": {k: v for k, v in filters_used.items() if v is not None},
            "offset": offset,
            "returned": [str(m.id) for m in meetings],
        },
    )
    db.commit()
    items = [AdminMeetingListItem(**to_list_item(m).model_dump(), owner=_owner(m)) for m in meetings]
    return AdminMeetingPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/meetings/{meeting_id}", response_model=AdminMeetingDetail)
def get_any_meeting(meeting_id: uuid.UUID, admin: CurrentAdmin, db: DbSession, settings: AppSettings):
    """The full meeting, including the transcript. Logged in the audit log."""
    meeting = _get_meeting(db, meeting_id)
    expire_stale(db, [meeting], settings)
    audit(db, admin, "meeting.view", "meeting", meeting.id, {"owner": meeting.owner.email, "title": meeting.title})
    db.commit()
    return AdminMeetingDetail(**to_detail(meeting).model_dump(), owner=_owner(meeting))


@router.get("/meetings/{meeting_id}/export", response_class=Response, responses=EXPORT_RESPONSES)
def export_any_meeting(
    meeting_id: uuid.UUID,
    admin: CurrentAdmin,
    db: DbSession,
    format: Literal["pdf", "docx"] = "pdf",
    include_transcript: bool = False,
):
    meeting = _get_meeting(db, meeting_id)
    audit(
        db, admin, "meeting.export", "meeting", meeting.id,
        {"owner": meeting.owner.email, "format": format, "include_transcript": include_transcript},
    )
    db.commit()
    return export_response(meeting, format, include_transcript)


@router.delete("/meetings/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_any_meeting(meeting_id: uuid.UUID, admin: CurrentAdmin, db: DbSession):
    meeting = _get_meeting(db, meeting_id)
    audit(db, admin, "meeting.delete", "meeting", meeting.id, {"owner": meeting.owner.email, "title": meeting.title})
    close_runs_for_meeting(db, meeting.id)
    db.delete(meeting)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------ audit log


@router.get("/audit-log", response_model=AuditLogPage)
def list_audit_log(
    admin: CurrentAdmin,
    db: DbSession,
    action: Annotated[str | None, Query(max_length=50, description='e.g. "meeting.view"')] = None,
    admin_id: uuid.UUID | None = None,
    target_id: Annotated[str | None, Query(max_length=64)] = None,
    since: UTCDateTime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Who did what, newest first."""
    filters = []
    if action:
        filters.append(AdminAuditLog.action == action)
    if admin_id is not None:
        filters.append(AdminAuditLog.admin_id == admin_id)
    if target_id:
        filters.append(AdminAuditLog.target_id == target_id)
    if since is not None:
        filters.append(AdminAuditLog.created_at >= since)
    total = db.scalar(select(func.count()).select_from(AdminAuditLog).where(*filters))
    entries = db.scalars(
        select(AdminAuditLog).where(*filters).order_by(AdminAuditLog.created_at.desc()).limit(limit).offset(offset)
    )
    return AuditLogPage(
        items=[AuditLogEntry.model_validate(e) for e in entries], total=total, limit=limit, offset=offset
    )

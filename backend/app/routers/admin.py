import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.deps import AppServices, CurrentAdmin, DbSession
from app.models import Meeting, User, UserRole
from app.routers.auth import revoke_all_refresh_tokens
from app.runtime_settings import (
    RuntimeSettingsOut,
    RuntimeSettingsUpdate,
    update_runtime_settings,
)
from app.schemas import AdminUserOut, AdminUserPage, AdminUserUpdate

router = APIRouter(prefix="/admin", tags=["admin"])


# ------------------------------------------------------------------- settings


@router.get("/settings", response_model=RuntimeSettingsOut)
def get_settings(admin: CurrentAdmin, db: DbSession, services: AppServices):
    """Provider settings, the summary prompt and limits. API keys are never returned."""
    return RuntimeSettingsOut.from_settings(services.load_runtime_settings(db))


@router.patch("/settings", response_model=RuntimeSettingsOut)
def update_settings(body: RuntimeSettingsUpdate, admin: CurrentAdmin, db: DbSession, services: AppServices):
    """Change only the fields sent. Applies to the next upload or summary on every instance."""
    return RuntimeSettingsOut.from_settings(update_runtime_settings(db, services.secret_box, body, admin.id))


# ---------------------------------------------------------------------- users


def _meeting_counts(db: Session, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    rows = db.execute(
        select(Meeting.owner_id, func.count()).where(Meeting.owner_id.in_(user_ids)).group_by(Meeting.owner_id)
    )
    return dict(rows.all())


def _out(user: User, meeting_count: int) -> AdminUserOut:
    return AdminUserOut.model_validate(
        {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "created_at": user.created_at,
            "is_active": user.is_active,
            "last_login_at": user.last_login_at,
            "meeting_count": meeting_count,
        }
    )


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

    total = db.scalar(select(func.count()).select_from(User).where(*filters))
    users = list(
        db.scalars(select(User).where(*filters).order_by(User.created_at.desc()).limit(limit).offset(offset))
    )
    counts = _meeting_counts(db, [u.id for u in users])
    return AdminUserPage(
        items=[_out(u, counts.get(u.id, 0)) for u in users], total=total, limit=limit, offset=offset
    )


@router.get("/users/{user_id}", response_model=AdminUserOut)
def get_user(user_id: uuid.UUID, admin: CurrentAdmin, db: DbSession):
    user = _get_user(db, user_id)
    return _out(user, _meeting_counts(db, [user.id]).get(user.id, 0))


@router.patch("/users/{user_id}", response_model=AdminUserOut)
def update_user(user_id: uuid.UUID, body: AdminUserUpdate, admin: CurrentAdmin, db: DbSession):
    """Change a user's role, disable or re-enable the account, or fix the name."""
    user = _get_user(db, user_id)
    fields = body.model_fields_set
    if ("role" in fields and body.role == UserRole.USER) or ("is_active" in fields and body.is_active is False):
        ensure_not_last_admin(db, user)

    if "role" in fields and body.role is not None:
        user.role = body.role
    if "full_name" in fields:
        user.full_name = body.full_name
    if "is_active" in fields and body.is_active is not None:
        user.is_active = body.is_active
        if not body.is_active:
            revoke_all_refresh_tokens(db, user.id)  # signed out everywhere, immediately
    db.commit()
    return _out(user, _meeting_counts(db, [user.id]).get(user.id, 0))

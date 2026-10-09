from fastapi import APIRouter, HTTPException, Response, status

from app.deps import AppServices, CurrentUser, DbSession
from app.routers.admin import ensure_not_last_admin
from app.routers.auth import revoke_all_refresh_tokens
from app.schemas import PasswordChange, UsageOut, UserOut, UserUpdate
from app.security import hash_password, verify_password
from app.services.pipeline import sweep_stale
from app.services.usage import monthly_usage

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
def get_me(user: CurrentUser):
    return user


@router.get("/me/usage", response_model=UsageOut)
def get_my_usage(user: CurrentUser, db: DbSession, services: AppServices):
    """Uploads and audio minutes used this month, and the limits (null = unlimited)."""
    sweep_stale(db, services.settings)
    usage = monthly_usage(db, user, services.load_runtime_settings(db))
    return UsageOut(
        period_start=usage.period_start,
        period_end=usage.period_end,
        uploads=usage.uploads,
        uploads_limit=usage.uploads_limit,
        audio_minutes=usage.audio_minutes,
        audio_minutes_limit=usage.audio_minutes_limit,
    )


@router.patch("/me", response_model=UserOut)
def update_me(body: UserUpdate, user: CurrentUser, db: DbSession):
    if "full_name" in body.model_fields_set:
        user.full_name = body.full_name
    db.commit()
    return user


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(body: PasswordChange, user: CurrentUser, db: DbSession):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    # Tokens first, then the user row: the same lock order as /auth/refresh (no deadlock).
    revoke_all_refresh_tokens(db, user.id)  # sign out other devices
    user.password_hash = hash_password(body.new_password)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_me(user: CurrentUser, db: DbSession):
    """Deletes the account and all of its meetings (Google Play requires in-app deletion)."""
    ensure_not_last_admin(db, user)
    db.delete(user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

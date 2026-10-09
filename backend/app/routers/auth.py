from datetime import timedelta

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.deps import AppSettings, DbSession
from app.models import RefreshToken, User, utcnow
from app.schemas import (
    AuthResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    UserOut,
)
from app.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# A refresh token reused within this window is treated as a client retry, not theft.
REUSE_GRACE = timedelta(seconds=60)
# How long expired refresh tokens are kept (for reuse detection) before they are deleted.
TOKEN_RETENTION = timedelta(days=60)
# Verifying against a dummy hash keeps response times similar for unknown emails.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def issue_tokens(db: Session, user: User, settings: Settings) -> AuthResponse:
    now = utcnow()
    user.last_active_at = now
    # Housekeeping: drop this user's tokens that expired long ago. Recently expired (and
    # rotated) ones are kept a while so reusing a stolen old token is still detected.
    db.execute(
        delete(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.expires_at < now - TOKEN_RETENTION
        )
    )
    raw = new_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(raw),
            expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
        )
    )
    db.commit()
    return AuthResponse(
        access_token=create_access_token(user.id, settings),
        refresh_token=raw,
        expires_in=settings.access_token_ttl_minutes * 60,
        user=UserOut.model_validate(user),
    )


def revoke_all_refresh_tokens(db: Session, user_id) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, db: DbSession, settings: AppSettings):
    email = body.email.lower()
    conflict = HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")
    if db.scalar(select(User.id).where(User.email == email)):
        raise conflict
    user = User(
        email=email, password_hash=hash_password(body.password), full_name=body.full_name, last_login_at=utcnow()
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise conflict from None
    return issue_tokens(db, user, settings)


@router.post("/login", response_model=AuthResponse)
def login(body: LoginRequest, db: DbSession, settings: AppSettings):
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    password_ok = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)
    if user is None or not password_ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")
    if not user.is_active:
        detail = "This account has been disabled"
        if user.disabled_reason:
            detail += f": {user.disabled_reason}"
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail)
    user.last_login_at = utcnow()
    return issue_tokens(db, user, settings)


@router.post("/refresh", response_model=AuthResponse)
def refresh(body: RefreshRequest, db: DbSession, settings: AppSettings):
    invalid = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token")
    token = db.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_refresh_token(body.refresh_token))
        .with_for_update()  # two concurrent refreshes must not both succeed
    )
    if token is None:
        raise invalid
    now = utcnow()
    if token.revoked_at is not None:
        if now - token.revoked_at > REUSE_GRACE:
            # An old, already-rotated token came back: assume it leaked and end all sessions.
            revoke_all_refresh_tokens(db, token.user_id)
            db.commit()
        raise invalid
    if token.expires_at <= now:
        raise invalid
    user = db.get(User, token.user_id)
    if user is None or not user.is_active:
        raise invalid
    token.revoked_at = now  # rotation: each refresh token works once
    return issue_tokens(db, user, settings)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(body: RefreshRequest, db: DbSession):
    token = db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(body.refresh_token))
    )
    if token is not None and token.revoked_at is None:
        token.revoked_at = utcnow()
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

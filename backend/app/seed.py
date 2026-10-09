"""Creates the seeded admin account from ADMIN_EMAIL / ADMIN_PASSWORD at startup."""

import enum
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import User, UserRole
from app.security import hash_password

logger = logging.getLogger(__name__)


class SeedResult(enum.StrEnum):
    CREATED = "created"
    EXISTS = "exists"  # already an admin (e.g. seeded on an earlier start)
    CONFLICT = "conflict"  # the email belongs to a regular account


def seed_admin(db: Session, email: str, password: str, full_name: str = "Administrator") -> SeedResult:
    email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is not None:
        # Never promote an existing account automatically: whoever registered this email
        # first would become admin with their own password. Use the CLI for that.
        return SeedResult.EXISTS if user.is_admin else SeedResult.CONFLICT
    if len(password) < 8:
        raise ValueError("ADMIN_PASSWORD must be at least 8 characters")
    db.add(User(email=email, password_hash=hash_password(password), full_name=full_name, role=UserRole.ADMIN))
    try:
        db.commit()
    except IntegrityError:  # another instance created it at the same moment
        db.rollback()
        return SeedResult.EXISTS
    return SeedResult.CREATED


def seed_admin_from_settings(session_factory, email: str | None, password: str | None) -> None:
    if not email or not password:
        return
    try:
        with session_factory() as db:
            result = seed_admin(db, email, password)
    except Exception:
        logger.exception("Could not create the seeded admin account %s", email)
        return
    if result == SeedResult.CREATED:
        logger.info("Created the seeded admin account %s", email)
    elif result == SeedResult.CONFLICT:
        logger.warning(
            "ADMIN_EMAIL %s belongs to an existing non-admin account; it was not promoted. "
            "Run `python -m app.cli create-admin %s` to promote it.", email, email,
        )

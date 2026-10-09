"""The usage log (processing_runs) and per-user monthly quotas."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models import ProcessingRun, RunKind, RunStatus, User, utcnow
from app.runtime_settings import RuntimeSettings

QUOTA_KINDS = (RunKind.UPLOAD, RunKind.REUPLOAD)


class QuotaExceeded(Exception):
    pass


@dataclass
class MonthlyUsage:
    period_start: datetime
    period_end: datetime
    uploads: int
    audio_minutes: float
    uploads_limit: int | None
    audio_minutes_limit: int | None


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Start and end of the calendar month (UTC) containing `now`."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, (start + timedelta(days=32)).replace(day=1)


def counts_toward_quota():
    """Uploads count unless they failed before any audio was transcribed. Running ones
    count too, so several uploads sent at once can't slip past the limit together; jobs
    that died are closed by pipeline.sweep_stale before usage is counted."""
    return ProcessingRun.kind.in_(QUOTA_KINDS) & or_(
        ProcessingRun.status != RunStatus.FAILED,
        func.coalesce(ProcessingRun.transcribed_seconds, 0) > 0,
    )


# Minutes a run uses: failed runs only what the API billed; others the larger of the
# recording's length and what was billed (a header can understate the length).
_COUNTED_SECONDS = case(
    (ProcessingRun.status == RunStatus.FAILED, func.coalesce(ProcessingRun.transcribed_seconds, 0)),
    else_=func.greatest(
        func.coalesce(ProcessingRun.audio_seconds, 0), func.coalesce(ProcessingRun.transcribed_seconds, 0)
    ),
)


def effective_limits(user: User, runtime: RuntimeSettings) -> tuple[int | None, int | None]:
    uploads = user.quota_uploads_per_month
    minutes = user.quota_audio_minutes_per_month
    return (
        uploads if uploads is not None else runtime.quota_uploads_per_month,
        minutes if minutes is not None else runtime.quota_audio_minutes_per_month,
    )


def usage_by_user(db: Session, user_ids: list[uuid.UUID], now: datetime | None = None) -> dict[uuid.UUID, tuple[int, float]]:
    """{user_id: (uploads, audio_minutes)} for the current month."""
    start, end = month_bounds(now or utcnow())
    rows = db.execute(
        select(ProcessingRun.user_id, func.count(), func.coalesce(func.sum(_COUNTED_SECONDS), 0.0))
        .where(
            ProcessingRun.user_id.in_(user_ids),
            ProcessingRun.created_at >= start,
            ProcessingRun.created_at < end,
            counts_toward_quota(),
        )
        .group_by(ProcessingRun.user_id)
    )
    return {user_id: (uploads, round(seconds / 60, 1)) for user_id, uploads, seconds in rows}


def monthly_usage(db: Session, user: User, runtime: RuntimeSettings) -> MonthlyUsage:
    now = utcnow()
    start, end = month_bounds(now)
    uploads, minutes = usage_by_user(db, [user.id], now).get(user.id, (0, 0.0))
    uploads_limit, minutes_limit = effective_limits(user, runtime)
    return MonthlyUsage(start, end, uploads, minutes, uploads_limit, minutes_limit)


def check_quota(db: Session, user: User, runtime: RuntimeSettings, audio_seconds: float | None) -> None:
    """Raise QuotaExceeded if one more upload of this length goes over the user's quota.

    The caller should hold a lock on the user's row so concurrent uploads are checked one
    after the other.
    """
    usage = monthly_usage(db, user, runtime)
    resets = f"It resets on {usage.period_end:%Y-%m-%d}."
    if usage.uploads_limit is not None and usage.uploads >= usage.uploads_limit:
        raise QuotaExceeded(f"You have used all {usage.uploads_limit} uploads for this month. {resets}")
    if usage.audio_minutes_limit is not None:
        left = max(usage.audio_minutes_limit - usage.audio_minutes, 0)
        if left <= 0:
            raise QuotaExceeded(
                f"You have used all {usage.audio_minutes_limit} audio minutes for this month. {resets}"
            )
        minutes = (audio_seconds or 0) / 60
        if minutes > left:
            raise QuotaExceeded(
                f"This recording is {minutes:.1f} minutes long, but only {left:.1f} of your "
                f"{usage.audio_minutes_limit} monthly minutes are left. {resets}"
            )


def start_run(
    db: Session, *, user_id: uuid.UUID, meeting_id: uuid.UUID, kind: RunKind, audio_seconds: float | None = None
) -> ProcessingRun:
    run = ProcessingRun(user_id=user_id, meeting_id=meeting_id, kind=kind, audio_seconds=audio_seconds)
    db.add(run)
    return run

"""KPIs for the admin dashboard, computed from the database (PostgreSQL)."""

from datetime import date, datetime, timedelta

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.models import (
    PROCESSING_STATUSES,
    Category,
    Meeting,
    MeetingStatus,
    ProcessingRun,
    RunKind,
    RunStatus,
    User,
    UserRole,
    utcnow,
)
from app.runtime_settings import RuntimeSettings
from app.schemas import (
    CountItem,
    DailyPoint,
    Dashboard,
    DashboardMeetings,
    DashboardUsage,
    DashboardUsers,
    TopUser,
)
from app.services.usage import QUOTA_KINDS

TOP_N = 10


def _utc_day(column):
    return func.date(func.timezone("UTC", column))


# Audio actually sent to the speech-to-text API (what it bills), including jobs that found no
# speech or failed later; used for "minutes" and the transcription cost.
_BILLED_SECONDS = func.coalesce(func.sum(ProcessingRun.transcribed_seconds), 0.0)


def _users(db: Session, start: datetime) -> DashboardUsers:
    row = db.execute(
        select(
            func.count(),
            func.count().filter(User.created_at >= start),
            func.count().filter(User.is_active.is_(False)),
            func.count().filter(User.role == UserRole.ADMIN),
        ).select_from(User)
    ).one()
    uploaders = select(ProcessingRun.user_id).where(ProcessingRun.created_at >= start)
    active = db.scalar(
        select(func.count()).select_from(User).where(or_(User.last_active_at >= start, User.id.in_(uploaders)))
    )
    return DashboardUsers(total=row[0], new=row[1], disabled=row[2], admins=row[3], active=active)


def _meetings(db: Session, start: datetime) -> DashboardMeetings:
    row = db.execute(
        select(
            func.count(),
            func.count().filter(Meeting.created_at >= start),
            func.count().filter(Meeting.status == MeetingStatus.COMPLETED),
            func.count().filter(Meeting.status == MeetingStatus.FAILED),
            func.count().filter(Meeting.status.in_(PROCESSING_STATUSES)),
        ).select_from(Meeting)
    ).one()
    return DashboardMeetings(total=row[0], created=row[1], completed=row[2], failed=row[3], processing=row[4])


def _usage(db: Session, start: datetime, runtime: RuntimeSettings) -> DashboardUsage:
    in_period = ProcessingRun.created_at >= start
    duration = func.extract("epoch", ProcessingRun.finished_at - ProcessingRun.created_at)
    row = db.execute(
        select(
            func.count().filter(ProcessingRun.kind.in_(QUOTA_KINDS)),
            _BILLED_SECONDS,
            func.avg(ProcessingRun.audio_seconds).filter(ProcessingRun.transcribed_seconds > 0),
            func.count().filter(ProcessingRun.kind == RunKind.RESUMMARIZE),
            func.count(),
            func.count().filter(ProcessingRun.status == RunStatus.FAILED),
            func.count().filter(ProcessingRun.status != RunStatus.RUNNING),
            func.avg(duration).filter(ProcessingRun.status == RunStatus.COMPLETED),
            func.coalesce(func.sum(ProcessingRun.llm_prompt_tokens), 0),
            func.coalesce(func.sum(ProcessingRun.llm_completion_tokens), 0),
        ).where(in_period)
    ).one()
    uploads, seconds, avg_seconds, resummaries, jobs, failed, finished, avg_processing, prompt, completion = row

    hours = float(seconds) / 3600
    transcription_cost = (
        round(hours * runtime.price_transcription_per_hour_usd, 4)
        if runtime.price_transcription_per_hour_usd is not None
        else None
    )
    llm_prices = (runtime.price_llm_input_per_million_tokens_usd, runtime.price_llm_output_per_million_tokens_usd)
    llm_cost = (
        round(prompt / 1e6 * (llm_prices[0] or 0) + completion / 1e6 * (llm_prices[1] or 0), 4)
        if any(price is not None for price in llm_prices)
        else None
    )
    parts = [cost for cost in (transcription_cost, llm_cost) if cost is not None]
    return DashboardUsage(
        uploads=uploads,
        audio_minutes=round(float(seconds) / 60, 1),
        average_recording_minutes=round(float(avg_seconds) / 60, 1) if avg_seconds is not None else None,
        summaries_regenerated=resummaries,
        jobs=jobs,
        failed_jobs=failed,
        failure_rate=round(failed / finished, 3) if finished else None,
        average_processing_seconds=round(float(avg_processing), 1) if avg_processing is not None else None,
        llm_prompt_tokens=prompt,
        llm_completion_tokens=completion,
        estimated_transcription_cost_usd=transcription_cost,
        estimated_llm_cost_usd=llm_cost,
        estimated_cost_usd=round(sum(parts), 4) if parts else None,
    )


def _daily(db: Session, start: datetime, first_day: date, last_day: date) -> list[DailyPoint]:
    day = _utc_day(ProcessingRun.created_at)
    runs = {
        row[0]: row[1:]
        for row in db.execute(
            select(
                day,
                func.count().filter(ProcessingRun.kind.in_(QUOTA_KINDS)),
                _BILLED_SECONDS,
                func.count().filter(ProcessingRun.status == RunStatus.FAILED),
            )
            .where(ProcessingRun.created_at >= start)
            .group_by(day)
        )
    }
    user_day = _utc_day(User.created_at)
    signups = dict(
        db.execute(select(user_day, func.count()).where(User.created_at >= start).group_by(user_day)).all()
    )
    points = []
    current = first_day
    while current <= last_day:
        uploads, seconds, failed = runs.get(current, (0, 0.0, 0))
        points.append(
            DailyPoint(
                date=current,
                uploads=uploads,
                audio_minutes=round(float(seconds) / 60, 1),
                new_users=signups.get(current, 0),
                failed_jobs=failed,
            )
        )
        current += timedelta(days=1)
    return points


def _top_categories(db: Session, start: datetime) -> list[CountItem]:
    """Category names are per user, so group them case-insensitively across users."""
    key = func.lower(func.btrim(Category.name))
    rows = db.execute(
        select(func.min(func.btrim(Category.name)), func.count(Meeting.id))
        .select_from(Meeting)
        .join(Category, Meeting.category_id == Category.id)
        .where(Meeting.created_at >= start)
        .group_by(key)
        .order_by(func.count(Meeting.id).desc(), key)
        .limit(TOP_N)
    ).all()
    items = [CountItem(label=label, count=count) for label, count in rows]
    uncategorized = db.scalar(
        select(func.count()).select_from(Meeting).where(Meeting.created_at >= start, Meeting.category_id.is_(None))
    )
    if uncategorized:
        items.append(CountItem(label="(uncategorized)", count=uncategorized))
    return items


_TOPICS_SQL = text(
    """
    SELECT min(btrim(t.value)) AS label, count(*) AS n
    FROM meetings m
    CROSS JOIN LATERAL jsonb_array_elements_text(
        CASE WHEN jsonb_typeof(m.summary -> 'topics') = 'array' THEN m.summary -> 'topics' ELSE '[]'::jsonb END
    ) AS t(value)
    WHERE m.created_at >= :start AND btrim(t.value) <> ''
    GROUP BY lower(btrim(t.value))
    ORDER BY n DESC, label
    LIMIT :limit
    """
)


def _top_topics(db: Session, start: datetime) -> list[CountItem]:
    rows = db.execute(_TOPICS_SQL, {"start": start, "limit": TOP_N + 5}).all()
    return [CountItem(label=label, count=count) for label, count in rows]


def _languages(db: Session, start: datetime) -> list[CountItem]:
    language = func.lower(func.coalesce(func.nullif(Meeting.summary["language"].astext, ""), "unknown"))
    rows = db.execute(
        select(language, func.count())
        .where(Meeting.created_at >= start, func.jsonb_typeof(Meeting.summary) == "object")
        .group_by(language)
        .order_by(func.count().desc(), language)
    ).all()
    return [CountItem(label=label, count=count) for label, count in rows]


def _top_users(db: Session, start: datetime) -> list[TopUser]:
    uploads = func.count().filter(ProcessingRun.kind.in_(QUOTA_KINDS))
    minutes = _BILLED_SECONDS / 60
    rows = db.execute(
        select(ProcessingRun.user_id, User.email, uploads, minutes)
        .join(User, User.id == ProcessingRun.user_id)  # deleted accounts are left out
        .where(ProcessingRun.created_at >= start)
        .group_by(ProcessingRun.user_id, User.email)
        .having(uploads > 0)
        .order_by(uploads.desc(), minutes.desc())
        .limit(TOP_N)
    ).all()
    return [
        TopUser(user_id=user_id, email=email, uploads=count, audio_minutes=round(float(mins), 1))
        for user_id, email, count, mins in rows
    ]


def build_dashboard(db: Session, runtime: RuntimeSettings, days: int) -> Dashboard:
    now = utcnow()
    last_day = now.date()
    first_day = last_day - timedelta(days=days - 1)
    start = datetime.combine(first_day, datetime.min.time(), tzinfo=now.tzinfo)
    return Dashboard(
        period_start=start,
        period_end=now,
        days=days,
        users=_users(db, start),
        meetings=_meetings(db, start),
        usage=_usage(db, start, runtime),
        daily=_daily(db, start, first_day, last_day),
        top_categories=_top_categories(db, start),
        top_topics=_top_topics(db, start),
        languages=_languages(db, start),
        top_users=_top_users(db, start),
    )

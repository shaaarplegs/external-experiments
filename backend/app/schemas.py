import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
)

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Password = Annotated[str, StringConstraints(min_length=8, max_length=128)]
HexColor = Annotated[str, StringConstraints(pattern=r"^#([0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")]
Priority = Literal["high", "medium", "low"]


def _to_utc(value: datetime) -> datetime:
    """Everything is stored and returned in UTC. Naive values sent by clients are taken as UTC."""
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


UTCDateTime = Annotated[datetime, AfterValidator(_to_utc)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- auth & users


class RegisterRequest(BaseModel):
    email: EmailStr
    password: Password
    full_name: Name | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    role: Literal["user", "admin"]
    created_at: UTCDateTime


class UserUpdate(BaseModel):
    full_name: Name | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: Password


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds")
    user: UserOut


# ----------------------------------------------------------------------- admin


class UsageOut(BaseModel):
    """This calendar month's usage (UTC). A null limit means unlimited."""

    period_start: UTCDateTime
    period_end: UTCDateTime
    uploads: int
    uploads_limit: int | None
    audio_minutes: float
    audio_minutes_limit: int | None


class AdminUserOut(UserOut):
    is_active: bool
    disabled_reason: str | None
    disabled_at: UTCDateTime | None
    last_login_at: UTCDateTime | None
    last_active_at: UTCDateTime | None
    meeting_count: int
    uploads_this_month: int
    audio_minutes_this_month: float
    quota_uploads_per_month: int | None = Field(description="Override; null = the global default")
    quota_audio_minutes_per_month: int | None = Field(description="Override; null = the global default")


class AdminUserPage(BaseModel):
    items: list[AdminUserOut]
    total: int
    limit: int
    offset: int


class AdminUserUpdate(BaseModel):
    """Only the fields sent are changed."""

    role: Literal["user", "admin"] | None = None
    is_active: bool | None = Field(None, description="false revokes access: signed out everywhere at once")
    disabled_reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = Field(
        None, description="Shown to the user when they try to log in"
    )
    full_name: Name | None = None
    quota_uploads_per_month: Annotated[int, Field(ge=1, le=1_000_000)] | None = Field(
        None, description="Monthly upload limit for this user; null = the global default"
    )
    quota_audio_minutes_per_month: Annotated[int, Field(ge=1, le=1_000_000)] | None = Field(
        None, description="Monthly audio-minutes limit for this user; null = the global default"
    )


# ------------------------------------------------------------------ categories


class CategoryCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    color: HexColor | None = None


class CategoryUpdate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    color: HexColor | None = None


class CategoryRef(ORMModel):
    id: uuid.UUID
    name: str
    color: str | None


class CategoryOut(CategoryRef):
    meeting_count: int = 0
    created_at: UTCDateTime


# -------------------------------------------------------- LLM summary contract


def _lenient_date(value):
    """LLMs sometimes return 'next Sunday' or '' where we asked for YYYY-MM-DD."""
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


LenientDate = Annotated[date | None, BeforeValidator(_lenient_date)]


class SummaryParty(BaseModel):
    name: str
    role: str | None = None


class SummaryActionItem(BaseModel):
    task: str
    owner: str | None = None
    due_date: LenientDate = None
    due_text: str | None = None
    priority: Priority | None = None

    @field_validator("priority", mode="before")
    @classmethod
    def _normalize_priority(cls, value):
        value = (value or "").strip().lower() if isinstance(value, str) else value
        return value if value in ("high", "medium", "low") else None


class MeetingSummaryContent(BaseModel):
    """The summary sections shown to the user (action items are stored separately)."""

    title: str = ""
    language: str = ""
    overview: str = ""
    what_happened: list[str] = []
    decisions: list[str] = []
    parties: list[SummaryParty] = []
    mentioned_date: LenientDate = None
    open_questions: list[str] = []
    topics: list[str] = []


class LLMMeetingSummary(MeetingSummaryContent):
    """Exactly what the summarizer prompt asks the model to return."""

    action_items: list[SummaryActionItem] = []


# -------------------------------------------------------------------- meetings


class AttendeeIn(BaseModel):
    name: Name
    email: EmailStr | None = None
    role: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


class AttendeeOut(ORMModel):
    id: uuid.UUID
    name: str
    email: str | None
    role: str | None


class ActionItemCreate(BaseModel):
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    assignee: Name | None = None
    due_date: date | None = None
    priority: Priority | None = None
    is_done: bool = False


class ActionItemUpdate(BaseModel):
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)] | None = None
    assignee: Name | None = None
    due_date: date | None = None
    priority: Priority | None = None
    is_done: bool | None = None


class ActionItemOut(ORMModel):
    id: uuid.UUID
    description: str
    assignee: str | None
    due_date: date | None
    due_text: str | None
    priority: str | None
    is_done: bool
    source: str
    created_at: UTCDateTime
    updated_at: UTCDateTime


class MeetingUpdate(BaseModel):
    """PATCH semantics: only fields present in the request are changed; send null to clear."""

    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    category_id: uuid.UUID | None = None
    meeting_date: UTCDateTime | None = None
    duration_seconds: Annotated[int, Field(ge=0)] | None = None
    attendees: list[AttendeeIn] | None = Field(default=None, description="Replaces the whole list")
    summary: MeetingSummaryContent | None = Field(default=None, description="Replaces the summary sections")


class MeetingListItem(ORMModel):
    id: uuid.UUID
    title: str | None
    category: CategoryRef | None
    meeting_date: UTCDateTime
    duration_seconds: int | None
    status: str
    error_message: str | None
    attendee_count: int
    action_items_total: int
    action_items_done: int
    overview: str | None
    created_at: UTCDateTime
    updated_at: UTCDateTime


class MeetingDetail(ORMModel):
    id: uuid.UUID
    title: str | None
    category: CategoryRef | None
    meeting_date: UTCDateTime
    duration_seconds: int | None
    language: str | None
    status: str
    error_message: str | None
    source_filename: str | None
    processing_started_at: UTCDateTime | None
    processed_at: UTCDateTime | None
    attendees: list[AttendeeOut]
    transcript: str | None
    summary: MeetingSummaryContent | None
    action_items: list[ActionItemOut]
    created_at: UTCDateTime
    updated_at: UTCDateTime


class MeetingPage(BaseModel):
    items: list[MeetingListItem]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------- admin: meetings


class OwnerRef(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    is_active: bool


class AdminMeetingListItem(MeetingListItem):
    owner: OwnerRef


class AdminMeetingDetail(MeetingDetail):
    owner: OwnerRef


class AdminMeetingPage(BaseModel):
    items: list[AdminMeetingListItem]
    total: int
    limit: int
    offset: int


# ------------------------------------------------------------- admin: audit log


class AuditLogEntry(ORMModel):
    id: uuid.UUID
    admin_id: uuid.UUID | None
    admin_email: str
    action: str
    target_type: str | None
    target_id: str | None
    details: dict | None
    created_at: UTCDateTime


class AuditLogPage(BaseModel):
    items: list[AuditLogEntry]
    total: int
    limit: int
    offset: int


# ------------------------------------------------------------- admin: dashboard


class CountItem(BaseModel):
    label: str
    count: int


class DailyPoint(BaseModel):
    date: date
    uploads: int
    audio_minutes: float
    new_users: int
    failed_jobs: int


class TopUser(BaseModel):
    user_id: uuid.UUID
    email: str
    uploads: int
    audio_minutes: float


class DashboardUsers(BaseModel):
    total: int
    active: int = Field(description="Used the app (signed in, refreshed a session or uploaded) during the period")
    new: int
    disabled: int
    admins: int


class DashboardMeetings(BaseModel):
    total: int = Field(description="Meetings that exist now")
    created: int = Field(description="Created during the period")
    completed: int
    failed: int
    processing: int


class DashboardUsage(BaseModel):
    uploads: int
    audio_minutes: float = Field(description="Audio sent to the speech-to-text API (what it bills)")
    average_recording_minutes: float | None
    summaries_regenerated: int
    jobs: int
    failed_jobs: int
    failure_rate: float | None = Field(description="Failed / finished jobs, 0-1")
    average_processing_seconds: float | None
    llm_prompt_tokens: int
    llm_completion_tokens: int
    estimated_transcription_cost_usd: float | None = Field(description="null until a price is set")
    estimated_llm_cost_usd: float | None = Field(description="null until a price is set")
    estimated_cost_usd: float | None


class Dashboard(BaseModel):
    period_start: UTCDateTime
    period_end: UTCDateTime
    days: int
    users: DashboardUsers
    meetings: DashboardMeetings
    usage: DashboardUsage
    daily: list[DailyPoint]
    top_categories: list[CountItem] = Field(description="What people use the app for, by category name")
    top_topics: list[CountItem] = Field(description="Topics the summaries detected")
    languages: list[CountItem]
    top_users: list[TopUser]

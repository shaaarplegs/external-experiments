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
    """Everything is stored in UTC. Naive values (from SQLite, or clients) are taken as UTC."""
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

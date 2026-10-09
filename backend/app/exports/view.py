"""Builds a language-aware view of a meeting that the PDF and Word renderers share."""

import re
import unicodedata
from dataclasses import dataclass, field

from app.models import Meeting, as_aware
from app.schemas import MeetingSummaryContent

LABELS = {
    "ar": {
        "doc_title": "محضر اجتماع",
        "date": "التاريخ",
        "duration": "المدة",
        "category": "التصنيف",
        "attendees": "الحضور",
        "overview": "الملخص",
        "what_happened": "مجريات الاجتماع",
        "decisions": "القرارات",
        "action_items": "المهام المطلوبة",
        "task": "المهمة",
        "owner": "المسؤول",
        "due": "الموعد",
        "priority": "الأولوية",
        "status": "الحالة",
        "done": "منجزة",
        "pending": "قيد التنفيذ",
        "parties": "الأطراف المعنية",
        "open_questions": "نقاط مفتوحة",
        "topics": "المواضيع",
        "transcript": "النص الكامل للتسجيل",
        "no_summary": "لم يتم إنشاء ملخص لهذا الاجتماع بعد.",
        "untitled": "اجتماع بدون عنوان",
        "high": "عالية",
        "medium": "متوسطة",
        "low": "منخفضة",
    },
    "en": {
        "doc_title": "Meeting minutes",
        "date": "Date",
        "duration": "Duration",
        "category": "Category",
        "attendees": "Attendees",
        "overview": "Summary",
        "what_happened": "What happened",
        "decisions": "Decisions",
        "action_items": "Action items",
        "task": "Task",
        "owner": "Owner",
        "due": "Due",
        "priority": "Priority",
        "status": "Status",
        "done": "Done",
        "pending": "Open",
        "parties": "Parties involved",
        "open_questions": "Open questions",
        "topics": "Topics",
        "transcript": "Full transcript",
        "no_summary": "No summary has been generated for this meeting yet.",
        "untitled": "Untitled meeting",
        "high": "High",
        "medium": "Medium",
        "low": "Low",
    },
}

_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_LETTER = re.compile(r"[^\W\d_]")
# Numbers joined by separators, e.g. 2026-11-01, 10/15, 12:30, 1.5.
_SEPARATED_NUMBER = re.compile(r"(?<!\d)\d+(?:[-/.:]\d+)+")
LRM = "\u200e"


def keep_numbers_ltr(text: str) -> str:
    """In right-to-left text the bidi algorithm shows 2026-11-01 as 01-11-2026.

    A left-to-right mark in front keeps the whole number in reading order.
    """
    if not text:
        return text
    return _SEPARATED_NUMBER.sub(lambda m: LRM + m.group(0), text.replace(LRM, ""))


def looks_arabic(text: str) -> bool:
    letters = _LETTER.findall(text or "")
    if not letters:
        return False
    arabic = sum(1 for ch in letters if _ARABIC.match(ch))
    return arabic / len(letters) > 0.3


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


@dataclass
class ActionRow:
    task: str
    owner: str
    due: str
    priority: str
    status: str
    done: bool


@dataclass
class MeetingView:
    lang: str
    rtl: bool
    labels: dict[str, str]
    title: str
    date: str
    duration: str
    category: str
    attendees: list[str]
    summary: MeetingSummaryContent | None
    actions: list[ActionRow] = field(default_factory=list)
    transcript: str | None = None


def build_view(meeting: Meeting, include_transcript: bool = False) -> MeetingView:
    summary = MeetingSummaryContent.model_validate(meeting.summary) if meeting.summary else None
    sample = " ".join(
        filter(None, [meeting.title, summary.overview if summary else None, (meeting.transcript or "")[:2000]])
    )
    if summary and summary.language:
        lang = "ar" if summary.language.lower().startswith("ar") else "en"
    else:
        lang = "ar" if looks_arabic(sample) else "en"
    labels = LABELS[lang]
    rtl = lang == "ar"

    def fix(text: str | None) -> str:
        return keep_numbers_ltr(text or "") if rtl else (text or "")

    if summary and rtl:
        summary = summary.model_copy(
            update={
                "overview": fix(summary.overview),
                "what_happened": [fix(t) for t in summary.what_happened],
                "decisions": [fix(t) for t in summary.decisions],
                "open_questions": [fix(t) for t in summary.open_questions],
                "parties": [p.model_copy(update={"role": fix(p.role) or None}) for p in summary.parties],
            }
        )

    actions = [
        ActionRow(
            task=fix(item.description),
            owner=fix(item.assignee),
            due=item.due_date.isoformat() if item.due_date else fix(item.due_text),
            priority=labels.get(item.priority, "") if item.priority else "",
            status=labels["done"] if item.is_done else labels["pending"],
            done=item.is_done,
        )
        for item in meeting.action_items
    ]
    attendees = [fix(f"{a.name} ({a.role})" if a.role else a.name) for a in meeting.attendees]
    title = meeting.title or (summary.title if summary and summary.title else labels["untitled"])
    return MeetingView(
        lang=lang,
        rtl=rtl,
        labels=labels,
        title=fix(title),
        date=fix(as_aware(meeting.meeting_date).strftime("%Y-%m-%d")) if meeting.meeting_date else "",
        duration=fix(format_duration(meeting.duration_seconds)),
        category=fix(meeting.category.name) if meeting.category else "",
        attendees=attendees,
        summary=summary,
        actions=actions,
        transcript=fix(meeting.transcript) if include_transcript and meeting.transcript else None,
    )


def export_filename(view: MeetingView, extension: str) -> str:
    base = unicodedata.normalize("NFKC", view.title.replace(LRM, ""))
    base = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", base).strip()[:80] or "meeting"
    return f"{base} {view.date.replace(LRM, '')}".strip() + f".{extension}"

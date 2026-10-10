import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.exports.view import LRM, keep_numbers_ltr, looks_arabic
from app.runtime_settings import RuntimeSettings
from app.services.audio import iter_chunks, normalize, probe
from app.services.summarizer import (
    MeetingContext,
    OpenAICompatibleSummarizer,
    SummarizerError,
    build_user_message,
    split_text,
)
from app.services.transcription import OpenAICompatibleTranscriber, TranscriptionError
from tests.conftest import make_audio


def test_split_for_transcription_makes_16k_mono_flac_chunks(audio_dir, tmp_path):
    source = make_audio(audio_dir / "long.m4a", 25, ["-ac", "2", "-ar", "44100", "-c:a", "aac"])
    audio = normalize(source, tmp_path / "chunks")
    assert round(audio.duration_seconds) == 25
    chunks = list(iter_chunks(audio, chunk_seconds=10))
    assert [c.name for c in chunks] == ["chunk_0000.flac", "chunk_0001.flac", "chunk_0002.flac"]
    durations = [round(probe(c).duration_seconds) for c in chunks]
    assert durations == [10, 10, 5]
    assert not (tmp_path / "chunks" / "normalized.flac").exists()


def test_split_drops_a_near_empty_trailing_chunk(audio_dir, tmp_path):
    source = make_audio(audio_dir / "edge.wav", 20.4, [])
    chunks = list(iter_chunks(normalize(source, tmp_path / "chunks"), chunk_seconds=10))
    assert len(chunks) == 2


def test_probe_reports_missing_audio(audio_dir):
    silent_video = audio_dir / "no-audio.mp4"
    import subprocess

    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
         "-c:v", "libx264", str(silent_video)],
        check=True,
    )
    info = probe(silent_video)
    assert info.has_audio is False


def test_transcriber_requires_an_api_key(tmp_path):
    with pytest.raises(TranscriptionError, match="speech-to-text API key has not been set"):
        OpenAICompatibleTranscriber(RuntimeSettings()).transcribe_file(tmp_path / "x.flac", "ar")


class StubChat:
    """Stands in for openai.OpenAI().chat.completions."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        content, finish = self.replies.pop(0)
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)])


def make_summarizer(replies, **overrides):
    runtime = RuntimeSettings().model_copy(update={"llm_api_key": "test-key", **overrides})
    summarizer = OpenAICompatibleSummarizer(runtime)
    chat = StubChat(replies)
    summarizer._client = SimpleNamespace(chat=SimpleNamespace(completions=chat))
    return summarizer, chat


VALID = json.dumps(
    {
        "title": "Budget review",
        "language": "en",
        "overview": "We reviewed the budget.",
        "what_happened": ["Reviewed Q3 numbers"],
        "decisions": ["Cut travel by 10%"],
        "action_items": [{"task": "Send the report", "owner": "Omar", "due_date": "next week", "priority": "Medium"}],
        "parties": [{"name": "Omar", "role": None}],
        "mentioned_date": None,
        "open_questions": [],
        "topics": ["budget"],
    }
)


def test_summarizer_parses_json_mode_output():
    summarizer, chat = make_summarizer([(VALID, "stop")])
    context = MeetingContext(title="Budget", meeting_date=date(2026, 10, 9), attendees=["Omar"])
    result = summarizer.summarize("we reviewed the budget", context)

    assert result.decisions == ["Cut travel by 10%"]
    assert result.action_items[0].due_date is None  # "next week" is not a date
    assert result.action_items[0].priority == "medium"
    request = chat.requests[0]
    assert request["model"] == "deepseek-chat"
    assert request["response_format"] == {"type": "json_object"}
    assert "json" in request["messages"][0]["content"]  # DeepSeek's JSON mode requires the word
    assert "<transcript>\nwe reviewed the budget\n</transcript>" in request["messages"][1]["content"]


def test_summarizer_retries_once_on_invalid_json():
    summarizer, chat = make_summarizer([("", "stop"), (VALID, "stop")])
    assert summarizer.summarize("text", MeetingContext()).title == "Budget review"
    assert len(chat.requests) == 2

    summarizer, _ = make_summarizer([("{oops", "stop"), ("[]", "stop")])
    with pytest.raises(SummarizerError, match="valid summary"):
        summarizer.summarize("text", MeetingContext())


def test_summarizer_reports_truncated_output():
    summarizer, _ = make_summarizer([('{"title": "cut', "length")])
    with pytest.raises(SummarizerError, match="cut off"):
        summarizer.summarize("text", MeetingContext())


def test_long_transcripts_are_condensed_before_summarizing():
    transcript = " ".join(f"sentence number {i}." for i in range(30))  # ~600 chars
    parts = len(split_text(transcript, 50))  # condensing uses half the limit per part
    summarizer, chat = make_summarizer(
        [("- note", "stop")] * parts + [(VALID, "stop")], summary_max_transcript_chars=100
    )

    summarizer.summarize(transcript, MeetingContext())
    *note_calls, final = chat.requests
    assert len(note_calls) == parts > 1
    assert all("response_format" not in call for call in note_calls)
    assert "notes condensed from a long meeting transcript" in final["messages"][1]["content"]
    assert "[Part 1 of" in final["messages"][1]["content"]


def test_summarizer_requires_an_api_key():
    with pytest.raises(SummarizerError, match="LLM\\) API key has not been set"):
        OpenAICompatibleSummarizer(RuntimeSettings()).summarize("text", MeetingContext())


def test_summarizer_uses_the_admin_prompt_and_model():
    prompt = "Custom minutes format. Return a json object with the usual keys."
    summarizer, chat = make_summarizer([(VALID, "stop")], summary_prompt=prompt, llm_model="deepseek-v4")
    summarizer.summarize("text", MeetingContext())
    assert chat.requests[0]["model"] == "deepseek-v4"
    assert chat.requests[0]["messages"][0]["content"] == prompt


def test_split_text_prefers_sentence_boundaries():
    text = "أولاً نناقش الميزانية. ثانياً نناقش الموعد؟ ثالثاً نوزع المهام."
    parts = split_text(text, 30)
    assert all(len(p) <= 30 for p in parts)
    assert "".join(p.replace(" ", "") for p in parts) == text.replace(" ", "")
    assert parts[0].endswith(".")
    assert split_text("short", 100) == ["short"]


def test_user_message_includes_metadata():
    message = build_user_message(
        "النص",
        MeetingContext(title="اجتماع", meeting_date=date(2026, 10, 9), attendees=["أحمد (مدير)"], duration_minutes=45),
        condensed=False,
    )
    assert "- Title: اجتماع" in message
    assert "- Date: 2026-10-09 (Friday; use it to resolve relative dates)" in message
    assert "- Attendees: أحمد (مدير)" in message
    assert "- Category: (not provided)" in message
    assert "- Duration: 45 minutes" in message


def test_rtl_helpers():
    assert looks_arabic("اجتماع الفريق about Cloud Run")
    assert not looks_arabic("Team meeting with أحمد")
    assert not looks_arabic("12345")
    fixed = keep_numbers_ltr("الإطلاق يوم 2026-11-01 الساعة 10:30 والنسبة 1.5")
    assert fixed == f"الإطلاق يوم {LRM}2026-11-01 الساعة {LRM}10:30 والنسبة {LRM}1.5"
    assert keep_numbers_ltr(fixed) == fixed  # idempotent

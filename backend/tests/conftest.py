import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.config import Settings
from app.db import Base, make_engine, make_session_factory
from app.deps import Services
from app.main import create_app
from app.models import AppSetting, User, UserRole
from app.schemas import LLMMeetingSummary, SummaryActionItem, SummaryParty
from app.security import SecretBox

# Tests need PostgreSQL: `docker compose up -d db` provides this one.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/meetings_test"
)

ARABIC_TRANSCRIPT = (
    "السلام عليكم. نبدأ اجتماع مراجعة خطة الإطلاق. "
    "أحمد سيجهز نسخة التطبيق للاختبار يوم الأحد القادم. "
    "اتفقنا على أن يكون الإطلاق التجريبي في بداية نوفمبر."
)


class FakeTranscriber:
    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []
        self.text = ARABIC_TRANSCRIPT
        self.error: Exception | None = None

    def transcribe_file(self, path: Path, language: str | None) -> str:
        assert path.exists() and path.suffix == ".flac"
        self.calls.append((path.name, language))
        if self.error:
            raise self.error
        return self.text


def sample_summary(**overrides) -> LLMMeetingSummary:
    data = dict(
        title="مراجعة خطة الإطلاق",
        language="ar",
        overview="ناقش الفريق خطة إطلاق التطبيق واتفق على موعد الإطلاق التجريبي.",
        what_happened=["مراجعة جاهزية التطبيق", "تحديد موعد الإطلاق التجريبي"],
        decisions=["الإطلاق التجريبي في بداية نوفمبر"],
        action_items=[
            SummaryActionItem(task="تجهيز نسخة الاختبار", owner="أحمد", due_date="2026-10-11", priority="high"),
            SummaryActionItem(task="إبلاغ فريق التسويق", due_text="قريباً"),
        ],
        parties=[SummaryParty(name="أحمد", role="المطور")],
        open_questions=[],
        topics=["الإطلاق"],
    )
    data.update(overrides)
    return LLMMeetingSummary(**data)


class FakeSummarizer:
    def __init__(self):
        self.calls = []
        self.result = sample_summary()
        self.error: Exception | None = None
        self.tokens_used = (1200, 300)  # reported even when summarizing fails

    def summarize(self, transcript, context):
        self.calls.append((transcript, context))
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=TEST_DATABASE_URL,
        secret_key="test-secret-test-secret-test-secret-123",
        work_dir=tmp_path / "work",
        _env_file=None,
    )


@pytest.fixture
def fakes():
    """The fake providers, plus the admin settings each one was built with."""
    return SimpleNamespace(transcriber=FakeTranscriber(), summarizer=FakeSummarizer(), runtimes=[])


@pytest.fixture
def services(settings, fakes):
    engine = make_engine(settings.database_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    def make_transcriber(runtime):
        fakes.runtimes.append(runtime)
        return fakes.transcriber

    def make_summarizer(runtime):
        fakes.runtimes.append(runtime)
        return fakes.summarizer

    yield Services(
        settings=settings,
        session_factory=make_session_factory(engine),
        secret_box=SecretBox(settings.secret_key),
        make_transcriber=make_transcriber,
        make_summarizer=make_summarizer,
    )
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(services):
    with TestClient(create_app(services=services)) as test_client:
        yield test_client


def register(client: TestClient, email="sara@example.com", password="correct-horse-1") -> dict:
    response = client.post("/auth/register", json={"email": email, "password": password, "full_name": "Sara"})
    assert response.status_code == 201, response.text
    return response.json()


def bearer(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def promote(services, email: str) -> None:
    with services.session_factory() as db:
        db.execute(update(User).where(User.email == email).values(role=UserRole.ADMIN))
        db.commit()


def set_runtime_settings(services, **values) -> None:
    """Store admin settings directly (non-secret values only)."""
    with services.session_factory() as db:
        for key, value in values.items():
            db.merge(AppSetting(key=key, value=value))
        db.commit()


@pytest.fixture
def auth(client) -> dict:
    return bearer(register(client))


@pytest.fixture
def admin_auth(client, services) -> dict:
    tokens = register(client, email="admin@example.com")
    promote(services, "admin@example.com")
    return bearer(tokens)


def make_audio(path: Path, seconds: float, container_args: list[str]) -> Path:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         *container_args, str(path)],
        check=True,
    )
    return path


@pytest.fixture(scope="session")
def audio_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("audio")


@pytest.fixture(scope="session")
def m4a_file(audio_dir) -> Path:
    return make_audio(audio_dir / "voice.m4a", 3, ["-c:a", "aac", "-b:a", "32k"])

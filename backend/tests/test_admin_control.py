"""Quotas, the usage log, revoking access, admin access to meetings, the audit log, the
dashboard and the seeded admin account."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, update

from app.main import create_app
from app.models import Meeting, MeetingStatus, ProcessingRun, User, utcnow
from app.seed import SeedResult, seed_admin
from app.services.summarizer import SummarizerError
from app.services.transcription import TranscriptionError
from tests.conftest import (
    bearer,
    make_audio,
    register,
    sample_summary,
    set_runtime_settings,
)
from tests.test_meetings import upload


def runs(services) -> list[ProcessingRun]:
    with services.session_factory() as db:
        return list(db.scalars(select(ProcessingRun).order_by(ProcessingRun.created_at)))


def audit_actions(client, admin_auth, **params) -> list[str]:
    page = client.get("/admin/audit-log", params=params, headers=admin_auth).json()
    return [entry["action"] for entry in page["items"]]


# ------------------------------------------------------------------ usage log


def test_each_job_is_recorded_in_the_usage_log(client, auth, m4a_file, services, fakes):
    set_runtime_settings(services, llm_model="deepseek-v4", transcription_model="whisper-large-v3-turbo")
    meeting_id = upload(client, auth, m4a_file).json()["id"]
    client.post(f"/meetings/{meeting_id}/summarize", headers=auth)

    first, second = runs(services)
    assert (first.kind, first.status, first.failed_stage) == ("upload", "completed", None)
    assert first.audio_seconds == pytest.approx(3, abs=0.2)
    assert (first.llm_prompt_tokens, first.llm_completion_tokens) == (1200, 300)
    assert (first.transcription_model, first.llm_model) == ("whisper-large-v3-turbo", "deepseek-v4")
    assert first.finished_at is not None and str(first.meeting_id) == meeting_id
    assert (second.kind, second.status, second.transcription_model) == ("resummarize", "completed", None)

    # The log outlives the meeting.
    client.delete(f"/meetings/{meeting_id}", headers=auth)
    assert [r.meeting_id for r in runs(services)] == [None, None]


def test_failures_record_the_stage(client, auth, m4a_file, services, fakes):
    fakes.transcriber.error = TranscriptionError("boom")
    upload(client, auth, m4a_file)
    fakes.transcriber.error = None
    fakes.summarizer.error = SummarizerError("bad json")
    upload(client, auth, m4a_file)

    transcription_failure, summary_failure = runs(services)
    assert (transcription_failure.status, transcription_failure.failed_stage) == ("failed", "transcription")
    assert (summary_failure.status, summary_failure.failed_stage) == ("failed", "summary")
    assert summary_failure.llm_prompt_tokens == 1200  # tokens spent before the failure still count


@pytest.mark.parametrize(("billed", "stage"), [(None, "transcription"), (3.0, "summary")])
def test_interrupted_jobs_are_closed_in_the_log(client, auth, m4a_file, services, billed, stage):
    upload(client, auth, m4a_file)
    with services.session_factory() as db:
        db.execute(update(Meeting).values(status=MeetingStatus.TRANSCRIBING, processing_started_at=utcnow() - timedelta(hours=2)))
        db.execute(
            update(ProcessingRun).values(status="running", finished_at=None, failed_stage=None, transcribed_seconds=billed)
        )
        db.commit()
    client.get("/meetings", headers=auth)
    (run,) = runs(services)
    assert (run.status, run.failed_stage) == ("failed", stage)


# --------------------------------------------------------------------- quotas


def test_usage_endpoint_shows_unlimited_by_default(client, auth, m4a_file):
    upload(client, auth, m4a_file)
    usage = client.get("/users/me/usage", headers=auth).json()
    assert usage["uploads"] == 1 and usage["uploads_limit"] is None
    assert usage["audio_minutes"] == pytest.approx(0.05, abs=0.05)
    assert usage["audio_minutes_limit"] is None
    assert usage["period_start"] < usage["period_end"]


def test_monthly_upload_quota(client, auth, m4a_file, services, fakes):
    set_runtime_settings(services, quota_uploads_per_month=2)
    fakes.transcriber.error = TranscriptionError("provider down")
    assert upload(client, auth, m4a_file).status_code == 202  # failed transcription: not counted
    fakes.transcriber.error = None
    first = upload(client, auth, m4a_file).json()["id"]
    assert upload(client, auth, m4a_file).status_code == 202

    blocked = upload(client, auth, m4a_file)
    assert blocked.status_code == 429
    assert "used all 2 uploads for this month" in blocked.json()["detail"]
    assert list(services.settings.work_dir.iterdir()) == []

    # Deleting meetings doesn't give uploads back; re-uploads count; re-summaries don't.
    client.delete(f"/meetings/{first}", headers=auth)
    assert upload(client, auth, m4a_file).status_code == 429
    usage = client.get("/users/me/usage", headers=auth).json()
    assert (usage["uploads"], usage["uploads_limit"]) == (2, 2)
    meeting_id = client.get("/meetings", headers=auth).json()["items"][0]["id"]
    assert client.post(f"/meetings/{meeting_id}/summarize", headers=auth).status_code == 202
    with m4a_file.open("rb") as f:
        again = client.post(f"/meetings/{meeting_id}/audio", files={"audio": ("voice.m4a", f)}, headers=auth)
    assert again.status_code == 429


def test_per_user_quota_override(client, auth, admin_auth, m4a_file, services):
    set_runtime_settings(services, quota_uploads_per_month=1)
    upload(client, auth, m4a_file)
    assert upload(client, auth, m4a_file).status_code == 429

    sara = client.get("/admin/users", params={"q": "sara@"}, headers=admin_auth).json()["items"][0]
    assert (sara["uploads_this_month"], sara["quota_uploads_per_month"]) == (1, None)
    response = client.patch(f"/admin/users/{sara['id']}", json={"quota_uploads_per_month": 5}, headers=admin_auth)
    assert response.json()["quota_uploads_per_month"] == 5
    assert upload(client, auth, m4a_file).status_code == 202
    assert client.get("/users/me/usage", headers=auth).json()["uploads_limit"] == 5

    # null goes back to the global default
    client.patch(f"/admin/users/{sara['id']}", json={"quota_uploads_per_month": None}, headers=admin_auth)
    assert client.get("/users/me/usage", headers=auth).json()["uploads_limit"] == 1


def test_monthly_minutes_quota(client, auth, audio_dir, m4a_file, services):
    set_runtime_settings(services, quota_audio_minutes_per_month=1)
    long_file = make_audio(audio_dir / "seventy-seconds.m4a", 70, ["-c:a", "aac", "-b:a", "16k"])
    blocked = upload(client, auth, long_file)
    assert blocked.status_code == 429
    assert blocked.json()["detail"].startswith("This recording is 1.2 minutes long, but only 1.0 of your 1 monthly minutes")
    assert list(services.settings.work_dir.iterdir()) == []  # rejected after saving: still cleaned up
    assert upload(client, auth, m4a_file).status_code == 202  # 3 seconds fits


def test_quota_settings_are_validated(client, admin_auth):
    assert client.patch("/admin/settings", json={"quota_uploads_per_month": 0}, headers=admin_auth).status_code == 422
    ok = client.patch(
        "/admin/settings", json={"quota_uploads_per_month": 20, "quota_audio_minutes_per_month": 600}, headers=admin_auth
    ).json()
    assert (ok["quota_uploads_per_month"], ok["quota_audio_minutes_per_month"]) == (20, 600)
    reset = client.patch("/admin/settings", json={"quota_uploads_per_month": None}, headers=admin_auth).json()
    assert reset["quota_uploads_per_month"] is None


# ------------------------------------------------------------- revoking access


def test_revoke_access_with_a_reason(client, admin_auth, m4a_file):
    tokens = register(client, email="omar@example.com")
    upload(client, bearer(tokens), m4a_file)
    user_id = tokens["user"]["id"]

    revoked = client.patch(
        f"/admin/users/{user_id}",
        json={"is_active": False, "disabled_reason": "Recording people without their consent"},
        headers=admin_auth,
    ).json()
    assert revoked["is_active"] is False
    assert revoked["disabled_reason"] == "Recording people without their consent"
    assert revoked["disabled_at"] is not None
    assert client.get("/meetings", headers=bearer(tokens)).status_code == 401
    login = client.post("/auth/login", json={"email": "omar@example.com", "password": "correct-horse-1"})
    assert login.status_code == 403
    assert login.json()["detail"] == "This account has been disabled: Recording people without their consent"

    restored = client.patch(f"/admin/users/{user_id}", json={"is_active": True}, headers=admin_auth).json()
    assert restored["disabled_reason"] is None and restored["disabled_at"] is None
    assert client.post("/auth/login", json={"email": "omar@example.com", "password": "correct-horse-1"}).status_code == 200

    entries = client.get("/admin/audit-log", params={"target_id": user_id}, headers=admin_auth).json()["items"]
    assert [e["details"]["access"] for e in entries] == ["restored", "revoked"]
    assert entries[1]["details"]["reason"] == "Recording people without their consent"
    assert entries[1]["admin_email"] == "admin@example.com"


# ----------------------------------------------------------- admin: meetings


def test_admin_can_see_export_and_delete_any_meeting(client, auth, admin_auth, m4a_file):
    meeting_id = upload(client, auth, m4a_file, title="اجتماع خاص").json()["id"]
    omar = register(client, email="omar@example.com")
    upload(client, bearer(omar), m4a_file, title="Omar's meeting")

    page = client.get("/admin/meetings", headers=admin_auth).json()
    assert page["total"] == 2
    assert {m["owner"]["email"] for m in page["items"]} == {"sara@example.com", "omar@example.com"}
    sara_id = next(m["owner"]["id"] for m in page["items"] if m["owner"]["email"] == "sara@example.com")
    only_sara = client.get("/admin/meetings", params={"user_id": sara_id}, headers=admin_auth).json()
    assert [m["title"] for m in only_sara["items"]] == ["اجتماع خاص"]
    assert client.get("/admin/meetings", params={"q": "omar"}, headers=admin_auth).json()["total"] == 1

    detail = client.get(f"/admin/meetings/{meeting_id}", headers=admin_auth).json()
    assert detail["transcript"].startswith("السلام عليكم")
    assert detail["owner"]["email"] == "sara@example.com"
    assert len(detail["action_items"]) == 2

    pdf = client.get(f"/admin/meetings/{meeting_id}/export", params={"format": "pdf"}, headers=admin_auth)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    assert client.delete(f"/admin/meetings/{meeting_id}", headers=admin_auth).status_code == 204
    assert client.get(f"/meetings/{meeting_id}", headers=auth).status_code == 404
    assert client.get(f"/admin/meetings/{meeting_id}", headers=admin_auth).status_code == 404

    assert audit_actions(client, admin_auth, target_id=meeting_id) == ["meeting.delete", "meeting.export", "meeting.view"]
    deleted = client.get("/admin/audit-log", params={"action": "meeting.delete"}, headers=admin_auth).json()["items"][0]
    assert deleted["details"] == {"owner": "sara@example.com", "title": "اجتماع خاص"}


def test_regular_users_cannot_use_admin_meeting_routes(client, auth, m4a_file):
    meeting_id = upload(client, auth, m4a_file).json()["id"]
    for method, path in (
        ("get", "/admin/meetings"),
        ("get", f"/admin/meetings/{meeting_id}"),
        ("get", f"/admin/meetings/{meeting_id}/export"),
        ("delete", f"/admin/meetings/{meeting_id}"),
        ("get", "/admin/audit-log"),
        ("get", "/admin/dashboard"),
    ):
        assert client.request(method, path, headers=auth).status_code == 403, path


def test_settings_changes_are_audited_without_values(client, admin_auth):
    client.patch("/admin/settings", json={"llm_api_key": "sk-secret-value-123456", "llm_model": "x"}, headers=admin_auth)
    entry = client.get("/admin/audit-log", params={"action": "settings.update"}, headers=admin_auth).json()["items"][0]
    assert entry["details"] == {"fields": ["llm_api_key", "llm_model"], "secrets_changed": ["llm_api_key"]}
    assert "sk-secret" not in str(entry)


# ------------------------------------------------------------------ dashboard


def test_dashboard_kpis(client, auth, admin_auth, m4a_file, services, fakes):
    set_runtime_settings(
        services,
        price_transcription_per_hour_usd=0.36,
        price_llm_input_per_million_tokens_usd=0.5,
        price_llm_output_per_million_tokens_usd=2.0,
    )
    work = client.post("/categories", json={"name": "Work"}, headers=auth).json()
    upload(client, auth, m4a_file, category_id=work["id"])
    upload(client, auth, m4a_file)
    omar = bearer(register(client, email="omar@example.com"))
    omar_work = client.post("/categories", json={"name": " work "}, headers=omar).json()
    fakes.summarizer.result = sample_summary(topics=["Budget", "الإطلاق"], language="en")
    upload(client, omar, m4a_file, category_id=omar_work["id"])
    fakes.transcriber.error = TranscriptionError("down")
    upload(client, omar, m4a_file)

    dash = client.get("/admin/dashboard", params={"days": 7}, headers=admin_auth).json()
    assert dash["days"] == 7 and len(dash["daily"]) == 7
    assert dash["users"] == {"total": 3, "active": 3, "new": 3, "disabled": 0, "admins": 1}
    assert dash["meetings"] == {"total": 4, "created": 4, "completed": 3, "failed": 1, "processing": 0}

    usage = dash["usage"]
    assert (usage["uploads"], usage["jobs"], usage["failed_jobs"]) == (4, 4, 1)
    assert usage["failure_rate"] == 0.25
    assert usage["audio_minutes"] == pytest.approx(0.15, abs=0.06)  # 3 transcribed recordings of 3 s
    assert (usage["llm_prompt_tokens"], usage["llm_completion_tokens"]) == (3600, 900)
    assert usage["estimated_llm_cost_usd"] == pytest.approx(3600 / 1e6 * 0.5 + 900 / 1e6 * 2.0)
    assert usage["estimated_transcription_cost_usd"] == pytest.approx(9 / 3600 * 0.36, abs=0.0005)
    assert usage["estimated_cost_usd"] == pytest.approx(
        usage["estimated_llm_cost_usd"] + usage["estimated_transcription_cost_usd"], abs=1e-4
    )

    today = dash["daily"][-1]
    assert (today["uploads"], today["new_users"], today["failed_jobs"]) == (4, 3, 1)
    assert sum(day["uploads"] for day in dash["daily"]) == 4

    categories = {c["label"].lower(): c["count"] for c in dash["top_categories"]}
    assert categories == {"work": 2, "(uncategorized)": 2}
    topics = {t["label"]: t["count"] for t in dash["top_topics"]}
    assert topics["الإطلاق"] == 3 and topics["Budget"] == 1
    assert {lang["label"]: lang["count"] for lang in dash["languages"]} == {"ar": 2, "en": 1}
    top = dash["top_users"]
    assert [(u["email"], u["uploads"]) for u in top] == [("omar@example.com", 2), ("sara@example.com", 2)] or [
        (u["email"], u["uploads"]) for u in top
    ] == [("sara@example.com", 2), ("omar@example.com", 2)]


def test_dashboard_without_prices_or_data(client, admin_auth):
    dash = client.get("/admin/dashboard", headers=admin_auth).json()
    assert dash["days"] == 30 and len(dash["daily"]) == 30
    usage = dash["usage"]
    assert usage["estimated_cost_usd"] is None and usage["failure_rate"] is None
    assert dash["top_topics"] == [] and dash["top_users"] == []
    assert client.get("/admin/dashboard", params={"days": 0}, headers=admin_auth).status_code == 422


# ------------------------------------------------------------- seeded admin


def test_seeded_admin_is_created_at_startup(services):
    services.settings.admin_email = "Owner@Example.com"
    services.settings.admin_password = SecretStr("seeded-pass-123")
    with TestClient(create_app(services=services)) as client:
        login = client.post("/auth/login", json={"email": "owner@example.com", "password": "seeded-pass-123"})
        assert login.status_code == 200
        assert login.json()["user"]["role"] == "admin"
    # A restart finds the account and leaves it alone (password changes in the app are kept).
    services.settings.admin_password = SecretStr("a-different-password")
    with TestClient(create_app(services=services)) as client:
        assert client.post("/auth/login", json={"email": "owner@example.com", "password": "seeded-pass-123"}).status_code == 200


def test_seeding_never_promotes_an_existing_account(client, services):
    register(client, email="owner@example.com")
    with services.session_factory() as db:
        assert seed_admin(db, "owner@example.com", "whatever-123") == SeedResult.CONFLICT
        assert db.scalar(select(User.role).where(User.email == "owner@example.com")) == "user"
        with pytest.raises(ValueError):
            seed_admin(db, "new@example.com", "short")
        assert seed_admin(db, "new@example.com", "long-enough-1") == SeedResult.CREATED
        assert seed_admin(db, "new@example.com", "long-enough-1") == SeedResult.EXISTS


# ------------------------------------------------- fixes from the code review


def streamed_webm(path, seconds):
    """WebM written as a stream, like Android's recorder: no length in the header."""
    import subprocess

    data = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=duration={seconds}",
         "-c:a", "libopus", "-f", "webm", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    path.write_bytes(data)
    return path


def test_headerless_recordings_are_measured_for_limits_and_quotas(client, auth, audio_dir, services):
    from app.services.audio import probe

    webm = streamed_webm(audio_dir / "stream.webm", 90)
    assert probe(webm).duration_seconds is None  # the header really has no length

    set_runtime_settings(services, quota_audio_minutes_per_month=2)
    first = upload(client, auth, webm)
    assert first.status_code == 202
    assert first.json()["duration_seconds"] == 90
    blocked = upload(client, auth, webm)  # 1.5 of 2 minutes used; another 1.5 doesn't fit
    assert blocked.status_code == 429
    assert "only 0.5 of your 2 monthly minutes" in blocked.json()["detail"]

    set_runtime_settings(services, quota_audio_minutes_per_month=1)  # now over the limit already
    over = upload(client, auth, webm)
    assert over.status_code == 429
    assert over.json()["detail"].startswith("You have used all 1 audio minutes")

    set_runtime_settings(services, quota_audio_minutes_per_month=100, max_audio_minutes=1)
    too_long = upload(client, auth, webm)
    assert too_long.status_code == 422 and "longer than 1 minute are" in too_long.json()["detail"]


def test_reuploads_count_toward_the_quota(client, auth, m4a_file, services):
    set_runtime_settings(services, quota_uploads_per_month=2)
    meeting_id = upload(client, auth, m4a_file).json()["id"]
    with m4a_file.open("rb") as f:
        assert client.post(f"/meetings/{meeting_id}/audio", files={"audio": ("v.m4a", f)}, headers=auth).status_code == 202
    assert client.get("/users/me/usage", headers=auth).json()["uploads"] == 2
    assert upload(client, auth, m4a_file).status_code == 429


def test_uploads_still_processing_count_toward_the_quota(client, auth, m4a_file, services, monkeypatch):
    """Two uploads sent at once must not both get through: running jobs count."""
    monkeypatch.setattr("app.routers.meetings.run_pipeline", lambda *args: None)  # jobs stay "running"
    set_runtime_settings(services, quota_uploads_per_month=1)
    assert upload(client, auth, m4a_file).status_code == 202
    assert upload(client, auth, m4a_file).status_code == 429
    (run,) = runs(services)
    assert run.status == "running"


def test_a_dead_job_does_not_block_the_quota(client, auth, m4a_file, services, monkeypatch):
    monkeypatch.setattr("app.routers.meetings.run_pipeline", lambda *args: None)  # the instance "dies"
    set_runtime_settings(services, quota_uploads_per_month=1)
    meeting_id = upload(client, auth, m4a_file).json()["id"]

    # Deleting the stuck meeting closes its run, so the upload doesn't stay counted.
    assert client.delete(f"/meetings/{meeting_id}", headers=auth).status_code == 204
    (run,) = runs(services)
    assert (run.status, run.failed_stage, run.meeting_id) == ("failed", "transcription", None)
    assert client.get("/users/me/usage", headers=auth).json()["uploads"] == 0

    # A run that lost its meeting some other way is closed once it is older than the timeout.
    upload(client, auth, m4a_file)
    with services.session_factory() as db:
        db.execute(update(ProcessingRun).where(ProcessingRun.status == "running").values(
            meeting_id=None, created_at=utcnow() - timedelta(hours=2)))
        db.commit()
    assert client.get("/users/me/usage", headers=auth).json()["uploads"] == 0


def test_billed_audio_counts_even_when_no_speech_was_found(client, auth, m4a_file, services, fakes):
    set_runtime_settings(services, quota_uploads_per_month=1)
    fakes.transcriber.text = ""
    meeting = client.get(f"/meetings/{upload(client, auth, m4a_file).json()['id']}", headers=auth).json()
    assert meeting["status"] == "failed"
    (run,) = runs(services)
    assert run.transcribed_seconds == pytest.approx(3, abs=0.2)  # the API transcribed (and billed) it
    assert upload(client, auth, m4a_file).status_code == 429


def test_dashboard_minutes_and_cost_follow_billed_audio(client, auth, admin_auth, audio_dir, services, fakes):
    set_runtime_settings(services, price_transcription_per_hour_usd=3.6)
    recording = make_audio(audio_dir / "thirty.m4a", 30, ["-c:a", "aac", "-b:a", "16k"])
    upload(client, auth, recording)  # completed: 30 s
    fakes.summarizer.error = SummarizerError("bad json")
    upload(client, auth, recording)  # transcribed, summary failed: 30 s billed
    fakes.summarizer.error = None
    fakes.transcriber.error = TranscriptionError("down")
    upload(client, auth, recording)  # nothing transcribed

    usage = client.get("/admin/dashboard", headers=admin_auth).json()["usage"]
    assert usage["audio_minutes"] == pytest.approx(1.0, abs=0.02)
    assert usage["estimated_transcription_cost_usd"] == pytest.approx(60 / 3600 * 3.6, abs=0.001)
    assert usage["average_recording_minutes"] == pytest.approx(0.5, abs=0.02)
    assert client.get("/users/me/usage", headers=auth).json()["uploads"] == 2


def test_dashboard_leaves_out_deleted_accounts_and_cleared_summaries(client, auth, admin_auth, m4a_file):
    omar = bearer(register(client, email="omar@example.com"))
    upload(client, omar, m4a_file)
    assert client.delete("/users/me", headers=omar).status_code == 204
    meeting_id = upload(client, auth, m4a_file).json()["id"]
    upload(client, auth, m4a_file)
    client.patch(f"/meetings/{meeting_id}", json={"summary": None}, headers=auth)

    dash = client.get("/admin/dashboard", headers=admin_auth).json()
    assert [u["email"] for u in dash["top_users"]] == ["sara@example.com"]
    assert dash["usage"]["uploads"] == 3  # the deleted account's usage still counts in totals
    assert dash["languages"] == [{"label": "ar", "count": 1}]


def test_admin_meeting_list_is_audited(client, auth, admin_auth, m4a_file):
    meeting_id = upload(client, auth, m4a_file).json()["id"]
    client.get("/admin/meetings", params={"q": "الإطلاق"}, headers=admin_auth)
    entry = client.get("/admin/audit-log", params={"action": "meeting.list"}, headers=admin_auth).json()["items"][0]
    assert entry["details"]["filters"] == {"q": "الإطلاق"}
    assert entry["details"]["returned"] == [meeting_id]


def test_create_admin_command_clears_an_old_revocation(client, admin_auth, services):
    from app.cli import make_admin

    tokens = register(client, email="omar@example.com")
    client.patch(f"/admin/users/{tokens['user']['id']}", json={"is_active": False, "disabled_reason": "x"}, headers=admin_auth)
    with services.session_factory() as db:
        make_admin(db, "omar@example.com")
    omar = client.get(f"/admin/users/{tokens['user']['id']}", headers=admin_auth).json()
    assert (omar["is_active"], omar["disabled_reason"], omar["disabled_at"], omar["role"]) == (True, None, None, "admin")


def test_a_short_seed_password_is_logged_without_crashing(services, caplog):
    services.settings.admin_email = "owner@example.com"
    services.settings.admin_password = SecretStr("short")
    with TestClient(create_app(services=services)) as client:
        assert client.get("/healthz").status_code == 200
    assert "ADMIN_PASSWORD must be at least 8 characters" in caplog.text
    assert "short" not in caplog.text.replace("at least 8 characters", "")


def test_reused_refresh_token_is_detected_after_it_expired(client, services):
    from app.models import RefreshToken
    from app.security import hash_refresh_token

    tokens = register(client)
    rotated = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).json()
    with services.session_factory() as db:
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == hash_refresh_token(tokens["refresh_token"]))
            .values(revoked_at=utcnow() - timedelta(days=40), expires_at=utcnow() - timedelta(days=10))
        )
        db.commit()
    client.post("/auth/login", json={"email": "sara@example.com", "password": "correct-horse-1"})  # cleanup runs
    # The old (rotated, expired) token is still known, so its reuse ends every session.
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": rotated["refresh_token"]}).status_code == 401

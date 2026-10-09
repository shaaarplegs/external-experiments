import io
import json
import zipfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.main import create_app
from app.models import Meeting, MeetingStatus, utcnow
from app.services.summarizer import SummarizerError
from app.services.transcription import TranscriptionError
from tests.conftest import (
    ARABIC_TRANSCRIPT,
    make_audio,
    register,
    sample_summary,
    set_runtime_settings,
)


def upload(client, auth, audio_path, **form):
    with audio_path.open("rb") as f:
        return client.post(
            "/meetings",
            files={"audio": (audio_path.name, f, "application/octet-stream")},
            data=form,
            headers=auth,
        )


def test_upload_transcribes_and_summarizes(client, auth, m4a_file, services, fakes):
    response = upload(client, auth, m4a_file)
    assert response.status_code == 202, response.text
    created = response.json()
    assert created["status"] == "queued"
    assert created["duration_seconds"] == 3
    assert created["source_filename"] == "voice.m4a"

    meeting = client.get(f"/meetings/{created['id']}", headers=auth).json()
    assert meeting["status"] == "completed"
    assert meeting["error_message"] is None
    assert meeting["transcript"] == ARABIC_TRANSCRIPT
    assert meeting["title"] == "مراجعة خطة الإطلاق"  # taken from the summary when not given
    assert meeting["summary"]["overview"].startswith("ناقش الفريق")
    assert meeting["summary"]["decisions"] == ["الإطلاق التجريبي في بداية نوفمبر"]
    assert "action_items" not in meeting["summary"]
    items = meeting["action_items"]
    assert [i["description"] for i in items] == ["تجهيز نسخة الاختبار", "إبلاغ فريق التسويق"]
    assert items[0] | {"id": None, "created_at": None, "updated_at": None} == {
        "id": None,
        "description": "تجهيز نسخة الاختبار",
        "assignee": "أحمد",
        "due_date": "2026-10-11",
        "due_text": None,
        "priority": "high",
        "is_done": False,
        "source": "ai",
        "created_at": None,
        "updated_at": None,
    }
    assert items[1]["due_text"] == "قريباً"
    # The default speech language hint is Arabic, and the temp files are cleaned up.
    assert fakes.transcriber.calls == [("chunk_0000.flac", "ar")]
    assert list(services.settings.work_dir.iterdir()) == []


def test_upload_metadata_is_kept_and_passed_to_the_summarizer(client, auth, m4a_file, services, fakes):
    category = client.post("/categories", json={"name": "المشاريع", "color": "#1F6FEB"}, headers=auth).json()
    attendees = [{"name": "أحمد", "role": "مطور"}, {"name": "Sara", "email": "sara@example.com"}]
    response = upload(
        client, auth, m4a_file,
        title="اجتماع الإطلاق",
        category_id=category["id"],
        meeting_date="2026-10-08T09:30:00+04:00",
        attendees=json.dumps(attendees, ensure_ascii=False),
        language="auto",
    )
    assert response.status_code == 202, response.text
    meeting = client.get(f"/meetings/{response.json()['id']}", headers=auth).json()

    assert meeting["title"] == "اجتماع الإطلاق"  # the user's title wins over the model's
    assert meeting["category"] == {"id": category["id"], "name": "المشاريع", "color": "#1F6FEB"}
    assert meeting["meeting_date"].startswith("2026-10-08T05:30:00")
    assert [a["name"] for a in meeting["attendees"]] == ["أحمد", "Sara"]
    assert meeting["language"] is None
    assert fakes.transcriber.calls[-1][1] is None  # "auto" -> let Whisper detect

    transcript, context = fakes.summarizer.calls[-1]
    assert transcript == ARABIC_TRANSCRIPT
    assert context.title == "اجتماع الإطلاق"
    assert context.category == "المشاريع"
    assert context.attendees == ["أحمد (مطور)", "Sara"]
    assert str(context.meeting_date) == "2026-10-08"
    assert context.duration_minutes == 0


def test_upload_accepts_video_files_and_other_formats(client, auth, audio_dir, services):
    mp4 = make_audio(audio_dir / "screen.mp4", 2, ["-f", "lavfi", "-i", "color=c=black:s=64x64:d=2",
                                                     "-shortest", "-c:v", "libx264", "-c:a", "aac"])
    ogg = make_audio(audio_dir / "voice-note.ogg", 2, ["-c:a", "libopus"])
    for path in (mp4, ogg):
        response = upload(client, auth, path)
        assert response.status_code == 202, response.text
        assert client.get(f"/meetings/{response.json()['id']}", headers=auth).json()["status"] == "completed"


@pytest.mark.parametrize(
    ("content", "form", "message"),
    [
        (b"this is not audio", {}, "not a readable audio"),
        (b"", {}, "empty"),
    ],
)
def test_upload_rejects_bad_files(client, auth, tmp_path, content, form, message, services):
    path = tmp_path / "notes.txt"
    path.write_bytes(content)
    response = upload(client, auth, path, **form)
    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert list(services.settings.work_dir.iterdir()) == []


def test_upload_rejects_bad_metadata(client, auth, m4a_file):
    assert upload(client, auth, m4a_file, attendees="not json").status_code == 422
    assert upload(client, auth, m4a_file, attendees='[{"role": "no name"}]').status_code == 422
    assert upload(client, auth, m4a_file, language="arabic!").status_code == 422
    other_user = register(client, email="other@example.com")
    foreign_category = client.post(
        "/categories", json={"name": "Theirs"}, headers={"Authorization": f"Bearer {other_user['access_token']}"}
    ).json()
    assert upload(client, auth, m4a_file, category_id=foreign_category["id"]).status_code == 404


def test_upload_over_the_admin_limit_is_rejected(client, auth, services, tmp_path):
    set_runtime_settings(services, max_upload_mb=1)
    big = tmp_path / "big.m4a"
    big.write_bytes(b"0" * (2 * 1024 * 1024))
    response = upload(client, auth, big)
    assert response.status_code == 413
    assert response.json()["detail"] == "Upload is larger than 1 MB"
    assert list(services.settings.work_dir.iterdir()) == []


def test_oversized_requests_are_rejected_before_reading(services, auth, tmp_path):
    services.settings.max_request_mb = 1
    with TestClient(create_app(services=services)) as small_client:
        big = tmp_path / "big.m4a"
        big.write_bytes(b"0" * (2 * 1024 * 1024))
        assert upload(small_client, auth, big).status_code == 413


def test_upload_rejects_recordings_that_are_too_long(client, auth, audio_dir, services):
    set_runtime_settings(services, max_audio_minutes=1)
    long_file = make_audio(audio_dir / "long-call.m4a", 65, ["-c:a", "aac", "-b:a", "16k"])
    response = upload(client, auth, long_file)
    assert response.status_code == 422
    assert "longer than" in response.json()["detail"]


def test_transcription_failure_marks_the_meeting_failed(client, auth, m4a_file, services, fakes):
    fakes.transcriber.error = TranscriptionError("The speech-to-text API key has not been set.")
    meeting_id = upload(client, auth, m4a_file).json()["id"]
    meeting = client.get(f"/meetings/{meeting_id}", headers=auth).json()
    assert meeting["status"] == "failed"
    assert meeting["error_message"] == "The speech-to-text API key has not been set."
    assert meeting["transcript"] is None

    # The client re-uploads the audio to retry.
    fakes.transcriber.error = None
    with m4a_file.open("rb") as f:
        retry = client.post(f"/meetings/{meeting_id}/audio", files={"audio": ("voice.m4a", f)}, headers=auth)
    assert retry.status_code == 202
    assert client.get(f"/meetings/{meeting_id}", headers=auth).json()["status"] == "completed"


def test_silent_recording_fails_with_a_clear_message(client, auth, m4a_file, services, fakes):
    fakes.transcriber.text = ""
    meeting = client.get(f"/meetings/{upload(client, auth, m4a_file).json()['id']}", headers=auth).json()
    assert meeting["status"] == "failed"
    assert meeting["error_message"] == "No speech was detected in the recording."
    assert fakes.summarizer.calls == []


def test_summary_failure_keeps_the_transcript_and_can_be_retried(client, auth, m4a_file, services, fakes):
    fakes.summarizer.error = SummarizerError("The model did not return a valid summary")
    meeting_id = upload(client, auth, m4a_file).json()["id"]
    meeting = client.get(f"/meetings/{meeting_id}", headers=auth).json()
    assert meeting["status"] == "failed"
    assert meeting["transcript"] == ARABIC_TRANSCRIPT
    assert meeting["summary"] is None

    fakes.summarizer.error = None
    assert client.post(f"/meetings/{meeting_id}/summarize", headers=auth).status_code == 202
    meeting = client.get(f"/meetings/{meeting_id}", headers=auth).json()
    assert meeting["status"] == "completed"
    assert len(meeting["action_items"]) == 2
    assert len(fakes.transcriber.calls) == 1  # no second transcription


def test_resummarize_replaces_ai_items_but_keeps_user_items(client, auth, m4a_file, services, fakes):
    meeting_id = upload(client, auth, m4a_file).json()["id"]
    mine = client.post(
        f"/meetings/{meeting_id}/action-items", json={"description": "حجز قاعة الاجتماع"}, headers=auth
    )
    assert mine.status_code == 201

    fakes.summarizer.result = sample_summary(
        action_items=[{"task": "مهمة جديدة من الملخص", "owner": None}], overview="ملخص محدث"
    )
    client.post(f"/meetings/{meeting_id}/summarize", headers=auth)
    meeting = client.get(f"/meetings/{meeting_id}", headers=auth).json()
    assert meeting["summary"]["overview"] == "ملخص محدث"
    assert [(i["description"], i["source"]) for i in meeting["action_items"]] == [
        ("مهمة جديدة من الملخص", "ai"),
        ("حجز قاعة الاجتماع", "user"),
    ]


def test_retry_endpoints_refuse_while_processing_or_without_transcript(client, auth, m4a_file, services, fakes):
    fakes.transcriber.error = TranscriptionError("boom")
    meeting_id = upload(client, auth, m4a_file).json()["id"]
    assert client.post(f"/meetings/{meeting_id}/summarize", headers=auth).status_code == 409  # no transcript

    with services.session_factory() as db:
        db.execute(update(Meeting).values(status=MeetingStatus.TRANSCRIBING, processing_started_at=utcnow()))
        db.commit()
    assert client.post(f"/meetings/{meeting_id}/summarize", headers=auth).status_code == 409
    with m4a_file.open("rb") as f:
        busy = client.post(f"/meetings/{meeting_id}/audio", files={"audio": ("voice.m4a", f)}, headers=auth)
    assert busy.status_code == 409


def test_stuck_processing_is_reported_as_failed(client, auth, m4a_file, services):
    upload(client, auth, m4a_file)
    with services.session_factory() as db:
        db.execute(
            update(Meeting).values(
                status=MeetingStatus.SUMMARIZING, processing_started_at=utcnow() - timedelta(hours=1)
            )
        )
        db.commit()
    listed = client.get("/meetings", headers=auth).json()["items"][0]
    assert listed["status"] == "failed"
    assert listed["error_message"] == "Processing was interrupted. Please retry."


def test_list_filters_and_pagination(client, auth, m4a_file, services, fakes):
    work = client.post("/categories", json={"name": "Work"}, headers=auth).json()
    fakes.summarizer.result = sample_summary(title="")
    ids = []
    for index in range(3):
        form = {"title": f"Meeting {index}", "meeting_date": f"2026-10-0{index + 1}T10:00:00Z"}
        if index == 0:
            form["category_id"] = work["id"]
        ids.append(upload(client, auth, m4a_file, **form).json()["id"])

    page = client.get("/meetings", params={"limit": 2}, headers=auth).json()
    assert page["total"] == 3
    assert [m["title"] for m in page["items"]] == ["Meeting 2", "Meeting 1"]  # newest first
    first = page["items"][0]
    assert first["attendee_count"] == 0
    assert first["action_items_total"] == 2 and first["action_items_done"] == 0
    assert first["overview"].startswith("ناقش")
    assert "transcript" not in first

    second_page = client.get("/meetings", params={"limit": 2, "offset": 2}, headers=auth).json()
    assert [m["title"] for m in second_page["items"]] == ["Meeting 0"]

    def titles(**params):
        return [m["title"] for m in client.get("/meetings", params=params, headers=auth).json()["items"]]

    assert titles(category_id=work["id"]) == ["Meeting 0"]
    assert titles(uncategorized=True) == ["Meeting 2", "Meeting 1"]
    assert titles(q="meeting 1") == ["Meeting 1"]
    assert titles(q="الإطلاق") == ["Meeting 2", "Meeting 1", "Meeting 0"]  # transcript search
    assert titles(q="100%") == []
    assert titles(date_from="2026-10-02T00:00:00Z", date_to="2026-10-02T23:59:59Z") == ["Meeting 1"]
    assert titles(status="completed") == ["Meeting 2", "Meeting 1", "Meeting 0"]
    assert titles(status="failed") == []
    assert client.get("/meetings", params={"status": "bogus"}, headers=auth).status_code == 422


def test_meetings_are_private_to_their_owner(client, auth, m4a_file):
    meeting_id = upload(client, auth, m4a_file).json()["id"]
    other = {"Authorization": f"Bearer {register(client, email='other@example.com')['access_token']}"}

    assert client.get("/meetings", headers=other).json()["total"] == 0
    assert client.get(f"/meetings/{meeting_id}", headers=other).status_code == 404
    assert client.patch(f"/meetings/{meeting_id}", json={"title": "x"}, headers=other).status_code == 404
    assert client.get(f"/meetings/{meeting_id}/export", headers=other).status_code == 404
    assert client.post(f"/meetings/{meeting_id}/summarize", headers=other).status_code == 404
    assert client.post(f"/meetings/{meeting_id}/action-items", json={"description": "x"}, headers=other).status_code == 404
    assert client.delete(f"/meetings/{meeting_id}", headers=other).status_code == 404
    assert client.get(f"/meetings/{meeting_id}", headers=auth).status_code == 200


def test_update_meeting(client, auth, m4a_file):
    meeting_id = upload(client, auth, m4a_file).json()["id"]
    category = client.post("/categories", json={"name": "Clients"}, headers=auth).json()
    summary = client.get(f"/meetings/{meeting_id}", headers=auth).json()["summary"]
    summary["overview"] = "ملخص عدّله المستخدم"

    response = client.patch(
        f"/meetings/{meeting_id}",
        json={
            "title": "عنوان جديد",
            "category_id": category["id"],
            "meeting_date": "2026-09-30T08:00:00Z",
            "duration_seconds": 3600,
            "attendees": [{"name": "Omar"}],
            "summary": summary,
        },
        headers=auth,
    )
    assert response.status_code == 200, response.text
    meeting = response.json()
    assert meeting["title"] == "عنوان جديد"
    assert meeting["category"]["name"] == "Clients"
    assert meeting["meeting_date"].startswith("2026-09-30T08:00:00")
    assert meeting["duration_seconds"] == 3600
    assert [a["name"] for a in meeting["attendees"]] == ["Omar"]
    assert meeting["summary"]["overview"] == "ملخص عدّله المستخدم"

    # Fields that are not sent stay as they are; null clears optional fields.
    cleared = client.patch(f"/meetings/{meeting_id}", json={"category_id": None, "attendees": []}, headers=auth).json()
    assert cleared["category"] is None and cleared["attendees"] == []
    assert cleared["title"] == "عنوان جديد"
    assert client.patch(f"/meetings/{meeting_id}", json={"meeting_date": None}, headers=auth).status_code == 422


def test_action_item_crud(client, auth, m4a_file):
    meeting_id = upload(client, auth, m4a_file).json()["id"]
    created = client.post(
        f"/meetings/{meeting_id}/action-items",
        json={"description": "Send the contract", "assignee": "Omar", "due_date": "2026-10-20", "priority": "low"},
        headers=auth,
    )
    assert created.status_code == 201
    item = created.json()
    assert item["source"] == "user" and item["is_done"] is False

    ai_item = client.get(f"/meetings/{meeting_id}", headers=auth).json()["action_items"][1]
    done = client.patch(
        f"/meetings/{meeting_id}/action-items/{ai_item['id']}",
        json={"is_done": True, "due_date": "2026-10-25"},
        headers=auth,
    ).json()
    assert done["is_done"] is True
    assert done["due_date"] == "2026-10-25" and done["due_text"] is None

    assert client.delete(f"/meetings/{meeting_id}/action-items/{item['id']}", headers=auth).status_code == 204
    assert client.delete(f"/meetings/{meeting_id}/action-items/{item['id']}", headers=auth).status_code == 404
    listed = client.get("/meetings", headers=auth).json()["items"][0]
    assert (listed["action_items_total"], listed["action_items_done"]) == (2, 1)


def test_delete_meeting(client, auth, m4a_file):
    meeting_id = upload(client, auth, m4a_file).json()["id"]
    assert client.delete(f"/meetings/{meeting_id}", headers=auth).status_code == 204
    assert client.get(f"/meetings/{meeting_id}", headers=auth).status_code == 404


def test_export_pdf_and_docx(client, auth, m4a_file):
    meeting_id = upload(client, auth, m4a_file, attendees='[{"name": "أحمد"}]').json()["id"]

    pdf = client.get(f"/meetings/{meeting_id}/export", params={"format": "pdf"}, headers=auth)
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    disposition = pdf.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="')
    assert "filename*=UTF-8''" in disposition and ".pdf" in disposition

    docx = client.get(
        f"/meetings/{meeting_id}/export", params={"format": "docx", "include_transcript": True}, headers=auth
    )
    assert docx.status_code == 200
    assert docx.headers["content-type"].startswith("application/vnd.openxmlformats")
    document_xml = zipfile.ZipFile(io.BytesIO(docx.content)).read("word/document.xml").decode()
    assert "مراجعة خطة الإطلاق" in document_xml
    assert "تجهيز نسخة الاختبار" in document_xml
    assert "أحمد سيجهز نسخة التطبيق" in document_xml  # transcript appendix
    assert "<w:bidi/>" in document_xml and "<w:rtl/>" in document_xml

    assert client.get(f"/meetings/{meeting_id}/export", params={"format": "xls"}, headers=auth).status_code == 422


def test_categories_crud(client, auth, m4a_file):
    work = client.post("/categories", json={"name": " Work ", "color": "#ff0000"}, headers=auth)
    assert work.status_code == 201
    work = work.json()
    assert work["name"] == "Work" and work["meeting_count"] == 0
    assert client.post("/categories", json={"name": "Work"}, headers=auth).status_code == 409
    assert client.post("/categories", json={"name": "Bad", "color": "red"}, headers=auth).status_code == 422
    client.post("/categories", json={"name": "Family"}, headers=auth)

    upload(client, auth, m4a_file, category_id=work["id"])
    listed = client.get("/categories", headers=auth).json()
    assert [(c["name"], c["meeting_count"]) for c in listed] == [("Family", 0), ("Work", 1)]

    renamed = client.patch(f"/categories/{work['id']}", json={"name": "العمل"}, headers=auth)
    assert renamed.status_code == 200 and renamed.json()["meeting_count"] == 1
    assert client.patch(f"/categories/{work['id']}", json={"name": "Family"}, headers=auth).status_code == 409

    assert client.delete(f"/categories/{work['id']}", headers=auth).status_code == 204
    meeting = client.get("/meetings", headers=auth).json()["items"][0]
    assert meeting["category"] is None  # the meeting survives, uncategorized

    other = {"Authorization": f"Bearer {register(client, email='other@example.com')['access_token']}"}
    family_id = next(c["id"] for c in listed if c["name"] == "Family")
    assert client.delete(f"/categories/{family_id}", headers=other).status_code == 404
    assert client.get("/categories", headers=other).json() == []


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_admin_panel_is_served(client):
    page = client.get("/panel/")
    assert page.status_code == 200 and "Meetings Admin" in page.text
    assert client.get("/panel/app.js").status_code == 200
    assert client.get("/", follow_redirects=False).headers["location"] == "/panel/"

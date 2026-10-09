import pytest
from sqlalchemy import select

from app.cli import make_admin
from app.models import AppSetting, User
from app.runtime_settings import DEFAULT_SUMMARY_PROMPT, load_runtime_settings
from app.security import SecretBox
from tests.conftest import bearer, promote, register
from tests.test_meetings import upload

KEY = "sk-live-0123456789abcdef"


def test_admin_routes_need_an_admin(client, auth):
    for method, path in (("get", "/admin/settings"), ("patch", "/admin/settings"), ("get", "/admin/users")):
        assert client.request(method, path, json={}, headers=auth).status_code == 403
        assert client.request(method, path, json={}).status_code == 401


def test_settings_start_with_defaults(client, admin_auth):
    settings = client.get("/admin/settings", headers=admin_auth).json()
    assert settings["llm_base_url"] == "https://api.deepseek.com"
    assert settings["llm_model"] == "deepseek-chat"
    assert settings["transcription_model"] == "whisper-large-v3"
    assert settings["transcription_language"] == "ar"
    assert settings["llm_api_key"] == {"configured": False, "hint": None}
    assert settings["transcription_api_key"] == {"configured": False, "hint": None}
    assert settings["summary_prompt"] == DEFAULT_SUMMARY_PROMPT
    assert settings["max_upload_mb"] == 30


def test_api_keys_are_encrypted_and_never_returned(client, admin_auth, services):
    response = client.patch(
        "/admin/settings", json={"llm_api_key": KEY, "llm_model": "deepseek-reasoner"}, headers=admin_auth
    )
    assert response.status_code == 200, response.text
    assert KEY not in response.text
    body = response.json()
    assert body["llm_api_key"] == {"configured": True, "hint": "…cdef"}
    assert body["llm_model"] == "deepseek-reasoner"
    assert KEY not in client.get("/admin/settings", headers=admin_auth).text

    with services.session_factory() as db:
        stored = db.get(AppSetting, "llm_api_key")
        assert KEY not in str(stored.value)
        assert SecretBox(services.settings.secret_key).decrypt(stored.value) == KEY
        admin_id = db.scalar(select(User.id).where(User.email == "admin@example.com"))
        assert stored.updated_by_id == admin_id
        # With a different SECRET_KEY the stored key cannot be read, so it counts as unset.
        assert load_runtime_settings(db, SecretBox("another-secret-another-secret-12345")).llm_api_key == ""


def test_patch_changes_only_what_is_sent_and_null_resets(client, admin_auth, services):
    client.patch("/admin/settings", json={"llm_api_key": KEY, "llm_temperature": 0.7}, headers=admin_auth)
    client.patch("/admin/settings", json={"max_audio_minutes": 90}, headers=admin_auth)
    settings = client.get("/admin/settings", headers=admin_auth).json()
    assert (settings["llm_temperature"], settings["max_audio_minutes"]) == (0.7, 90)
    assert settings["llm_api_key"]["configured"] is True

    settings = client.patch(
        "/admin/settings", json={"llm_temperature": None, "llm_api_key": ""}, headers=admin_auth
    ).json()
    assert settings["llm_temperature"] == 0.3
    assert settings["llm_api_key"]["configured"] is False
    with services.session_factory() as db:
        assert sorted(db.scalars(select(AppSetting.key))) == ["max_audio_minutes"]


@pytest.mark.parametrize(
    "change",
    [
        {"llm_temperature": 5},
        {"llm_max_tokens": 10},
        {"max_upload_mb": 64},
        {"transcription_language": "arabic"},
        {"llm_base_url": "ftp://example.com"},
        {"summary_prompt": "Summarize the meeting in a few bullet points, please. Keep it short and clear."},
    ],
)
def test_invalid_settings_are_rejected(client, admin_auth, change):
    response = client.patch("/admin/settings", json=change, headers=admin_auth)
    assert response.status_code == 422, change


def test_settings_apply_to_the_next_upload(client, admin_auth, auth, m4a_file, fakes):
    new_prompt = DEFAULT_SUMMARY_PROMPT + "\nAlways add a closing remark."
    response = client.patch(
        "/admin/settings",
        json={
            "llm_api_key": KEY,
            "llm_model": "deepseek-v4",
            "transcription_language": "AUTO",
            "transcription_chunk_seconds": 300,
            "summary_prompt": new_prompt,
        },
        headers=admin_auth,
    )
    assert response.status_code == 200, response.text
    assert response.json()["transcription_language"] == "auto"
    meeting = upload(client, auth, m4a_file).json()
    assert client.get(f"/meetings/{meeting['id']}", headers=auth).json()["status"] == "completed"

    runtime = fakes.runtimes[-1]
    assert runtime.llm_api_key == KEY
    assert runtime.llm_model == "deepseek-v4"
    assert runtime.transcription_chunk_seconds == 300
    assert runtime.summary_prompt == new_prompt
    assert fakes.transcriber.calls[-1][1] is None  # "auto" = let Whisper detect the language


def test_list_and_view_users(client, admin_auth, auth, m4a_file):
    upload(client, auth, m4a_file)
    register(client, email="omar@example.com")

    page = client.get("/admin/users", headers=admin_auth).json()
    assert page["total"] == 3
    by_email = {u["email"]: u for u in page["items"]}
    assert by_email["sara@example.com"]["meeting_count"] == 1
    assert by_email["admin@example.com"]["role"] == "admin"
    registered = by_email["omar@example.com"]
    assert registered["last_login_at"] is not None  # registering signs the user in
    assert registered["last_active_at"] >= registered["last_login_at"]

    client.post("/auth/login", json={"email": "omar@example.com", "password": "correct-horse-1"})
    omar = client.get(f"/admin/users/{registered['id']}", headers=admin_auth).json()
    assert omar["last_login_at"] > registered["last_login_at"]
    assert omar["last_active_at"] >= omar["last_login_at"]
    assert omar["is_active"] is True and omar["meeting_count"] == 0

    def emails(**params):
        return sorted(u["email"] for u in client.get("/admin/users", params=params, headers=admin_auth).json()["items"])

    assert emails(q="OMAR") == ["omar@example.com"]
    assert emails(role="admin") == ["admin@example.com"]
    assert emails(limit=1)  # pagination works
    assert client.get("/admin/users/00000000-0000-0000-0000-000000000000", headers=admin_auth).status_code == 404


def test_disable_and_enable_an_account(client, admin_auth):
    tokens = register(client, email="omar@example.com")
    user_id = tokens["user"]["id"]

    disabled = client.patch(f"/admin/users/{user_id}", json={"is_active": False}, headers=admin_auth)
    assert disabled.status_code == 200 and disabled.json()["is_active"] is False
    assert client.get("/users/me", headers=bearer(tokens)).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    login = client.post("/auth/login", json={"email": "omar@example.com", "password": "correct-horse-1"})
    assert login.status_code == 403
    assert login.json()["detail"] == "This account has been disabled"

    client.patch(f"/admin/users/{user_id}", json={"is_active": True}, headers=admin_auth)
    assert client.post("/auth/login", json={"email": "omar@example.com", "password": "correct-horse-1"}).status_code == 200


def test_promote_a_user_to_admin(client, admin_auth):
    tokens = register(client, email="omar@example.com")
    response = client.patch(f"/admin/users/{tokens['user']['id']}", json={"role": "admin"}, headers=admin_auth)
    assert response.json()["role"] == "admin"
    assert client.get("/users/me", headers=bearer(tokens)).json()["role"] == "admin"
    assert client.get("/admin/settings", headers=bearer(tokens)).status_code == 200


def test_the_last_admin_cannot_be_removed(client, admin_auth, services):
    me = client.get("/users/me", headers=admin_auth).json()
    for change in ({"role": "user"}, {"is_active": False}):
        response = client.patch(f"/admin/users/{me['id']}", json=change, headers=admin_auth)
        assert response.status_code == 409

    assert client.delete("/users/me", headers=admin_auth).status_code == 409

    register(client, email="second@example.com")
    promote(services, "second@example.com")
    assert client.patch(f"/admin/users/{me['id']}", json={"role": "user"}, headers=admin_auth).status_code == 200
    assert client.get("/admin/settings", headers=admin_auth).status_code == 403


def test_make_admin_command(client, services):
    register(client, email="owner@example.com")
    with services.session_factory() as db:
        assert make_admin(db, "Owner@Example.com") is False  # existing account promoted
        with pytest.raises(ValueError):
            make_admin(db, "new@example.com")  # a new account needs a password
        assert make_admin(db, "new@example.com", password="new-admin-pass-1") is True

    tokens = client.post("/auth/login", json={"email": "new@example.com", "password": "new-admin-pass-1"}).json()
    assert tokens["user"]["role"] == "admin"
    owner = client.post("/auth/login", json={"email": "owner@example.com", "password": "correct-horse-1"}).json()
    assert owner["user"]["role"] == "admin"

from datetime import timedelta

from sqlalchemy import func, select, update

from app.models import (
    ActionItem,
    Category,
    Meeting,
    MeetingAttendee,
    RefreshToken,
    utcnow,
)
from app.security import hash_refresh_token
from tests.conftest import register


def bearer(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_register_returns_tokens_and_user(client):
    tokens = register(client, email="Sara@Example.com")
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == 30 * 60
    assert tokens["user"]["email"] == "sara@example.com"

    me = client.get("/users/me", headers=bearer(tokens))
    assert me.status_code == 200
    assert me.json()["full_name"] == "Sara"


def test_register_rejects_duplicate_email_case_insensitively(client):
    register(client, email="sara@example.com")
    response = client.post("/auth/register", json={"email": "SARA@example.com", "password": "another-pass-1"})
    assert response.status_code == 409


def test_register_validates_input(client):
    assert client.post("/auth/register", json={"email": "not-an-email", "password": "long-enough"}).status_code == 422
    assert client.post("/auth/register", json={"email": "a@example.com", "password": "short"}).status_code == 422


def test_login(client):
    register(client)
    ok = client.post("/auth/login", json={"email": "SARA@example.com", "password": "correct-horse-1"})
    assert ok.status_code == 200
    assert client.get("/users/me", headers=bearer(ok.json())).status_code == 200

    wrong = client.post("/auth/login", json={"email": "sara@example.com", "password": "wrong-password"})
    unknown = client.post("/auth/login", json={"email": "nobody@example.com", "password": "correct-horse-1"})
    assert wrong.status_code == unknown.status_code == 401


def test_protected_routes_need_a_valid_token(client):
    assert client.get("/users/me").status_code == 401
    assert client.get("/meetings", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_refresh_rotates_tokens(client):
    tokens = register(client)
    first = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert first.status_code == 200
    new_tokens = first.json()
    assert new_tokens["refresh_token"] != tokens["refresh_token"]
    assert client.get("/users/me", headers=bearer(new_tokens)).status_code == 200

    # The old refresh token is single-use.
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    # A quick retry is not treated as theft, so the new token still works.
    assert client.post("/auth/refresh", json={"refresh_token": new_tokens["refresh_token"]}).status_code == 200


def test_reusing_an_old_refresh_token_revokes_all_sessions(client, services):
    tokens = register(client)
    rotated = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).json()
    with services.session_factory() as db:
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == hash_refresh_token(tokens["refresh_token"]))
            .values(revoked_at=utcnow() - timedelta(minutes=5))
        )
        db.commit()

    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": rotated["refresh_token"]}).status_code == 401


def test_expired_refresh_token_is_rejected(client, services):
    tokens = register(client)
    with services.session_factory() as db:
        db.execute(update(RefreshToken).values(expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401


def test_logout_revokes_the_refresh_token(client):
    tokens = register(client)
    assert client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]}).status_code == 204
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401


def test_update_profile(client):
    tokens = register(client)
    response = client.patch("/users/me", json={"full_name": "سارة"}, headers=bearer(tokens))
    assert response.status_code == 200
    assert response.json()["full_name"] == "سارة"


def test_change_password(client):
    tokens = register(client)
    wrong = client.post(
        "/users/me/password",
        json={"current_password": "nope-nope-nope", "new_password": "brand-new-pass-2"},
        headers=bearer(tokens),
    )
    assert wrong.status_code == 400

    ok = client.post(
        "/users/me/password",
        json={"current_password": "correct-horse-1", "new_password": "brand-new-pass-2"},
        headers=bearer(tokens),
    )
    assert ok.status_code == 204
    # Other sessions are signed out and only the new password works.
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    assert client.post("/auth/login", json={"email": "sara@example.com", "password": "correct-horse-1"}).status_code == 401
    assert client.post("/auth/login", json={"email": "sara@example.com", "password": "brand-new-pass-2"}).status_code == 200


def test_delete_account_removes_everything(client, m4a_file, services):
    tokens = register(client)
    with m4a_file.open("rb") as f:
        client.post("/meetings", files={"audio": ("voice.m4a", f, "audio/mp4")}, headers=bearer(tokens))
    client.post("/categories", json={"name": "Work"}, headers=bearer(tokens))

    assert client.delete("/users/me", headers=bearer(tokens)).status_code == 204
    with services.session_factory() as db:
        for model in (Meeting, MeetingAttendee, ActionItem, Category, RefreshToken):
            assert db.scalar(select(func.count()).select_from(model)) == 0, model.__name__
    assert client.get("/users/me", headers=bearer(tokens)).status_code == 401
    assert client.post("/auth/login", json={"email": "sara@example.com", "password": "correct-horse-1"}).status_code == 401
    # The email can be registered again from scratch.
    fresh = register(client)
    assert client.get("/meetings", headers=bearer(fresh)).json()["total"] == 0

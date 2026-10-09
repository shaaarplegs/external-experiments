import base64
import hashlib
import secrets
import uuid
from datetime import timedelta
from functools import lru_cache

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import Settings
from app.models import utcnow

_hasher = PasswordHasher()
JWT_ALGORITHM = "HS256"


@lru_cache
def derive_key(secret_key: str, purpose: str) -> bytes:
    """Independent 32-byte keys from the one SECRET_KEY, one per purpose."""
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=f"meetings-api/{purpose}".encode()
    ).derive(secret_key.encode())


class SecretBox:
    """Encrypts secrets (provider API keys) before they are stored in the database."""

    def __init__(self, secret_key: str):
        self._fernet = Fernet(base64.urlsafe_b64encode(derive_key(secret_key, "settings-encryption")))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str | None:
        """None if the value was encrypted with a different SECRET_KEY."""
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def create_access_token(user_id: uuid.UUID, settings: Settings) -> str:
    now = utcnow()
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
    }
    return jwt.encode(payload, derive_key(settings.secret_key, "jwt"), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, settings: Settings) -> uuid.UUID | None:
    try:
        payload = jwt.decode(token, derive_key(settings.secret_key, "jwt"), algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("type") != "access":
        return None
    try:
        return uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        return None


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    # Refresh tokens are long random strings, so a fast hash is enough.
    return hashlib.sha256(token.encode()).hexdigest()

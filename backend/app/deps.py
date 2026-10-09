from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import User
from app.runtime_settings import RuntimeSettings, load_runtime_settings
from app.security import SecretBox, decode_access_token
from app.services.summarizer import Summarizer
from app.services.transcription import Transcriber


@dataclass
class Services:
    """Process-wide dependencies, stored on app.state so tests can swap them."""

    settings: Settings
    session_factory: sessionmaker[Session]
    secret_box: SecretBox
    # Built per job from the current admin settings, so key/model changes apply immediately.
    make_transcriber: Callable[[RuntimeSettings], Transcriber]
    make_summarizer: Callable[[RuntimeSettings], Summarizer]

    def load_runtime_settings(self, db: Session) -> RuntimeSettings:
        return load_runtime_settings(db, self.secret_box)


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_settings_dep(services: Annotated[Services, Depends(get_services)]) -> Settings:
    return services.settings


def get_db(services: Annotated[Services, Depends(get_services)]) -> Iterator[Session]:
    session = services.session_factory()
    try:
        yield session
    finally:
        session.close()


_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized
    user_id = decode_access_token(credentials.credentials, settings)
    if user_id is None:
        raise unauthorized
    # Loaded on every request, so disabling an account or changing a role applies at once.
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorized
    return user


def get_current_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user


DbSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentAdmin = Annotated[User, Depends(get_current_admin)]
AppSettings = Annotated[Settings, Depends(get_settings_dep)]
AppServices = Annotated[Services, Depends(get_services)]

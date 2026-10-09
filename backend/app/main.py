import json
import logging

from fastapi import FastAPI

from app.config import DEV_JWT_SECRET, Settings, get_settings
from app.db import make_engine, make_session_factory
from app.deps import Services
from app.routers import auth, categories, meetings, users
from app.services.summarizer import OpenAICompatibleSummarizer
from app.services.transcription import OpenAICompatibleTranscriber

DESCRIPTION = """
Backend for the meetings app: upload a recording, get back a transcript, a structured
summary and action items. Meetings can be categorized, edited and exported as PDF or Word.

Authenticate with `POST /auth/login` (or `/auth/register`), then send
`Authorization: Bearer <access_token>`. Refresh with `POST /auth/refresh` before it expires.
"""


class MaxBodySizeMiddleware:
    """Reject oversized uploads before reading them (Cloud Run's own cap is 32 MiB)."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            length = dict(scope["headers"]).get(b"content-length")
            if length is not None and length.isdigit() and int(length) > self.max_bytes:
                body = json.dumps(
                    {"detail": f"Upload is larger than {self.max_bytes // (1024 * 1024)} MB"}
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [(b"content-type", b"application/json"), (b"connection", b"close")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_services(settings: Settings) -> Services:
    return Services(
        settings=settings,
        session_factory=make_session_factory(make_engine(settings.database_url)),
        transcriber=OpenAICompatibleTranscriber(settings),
        summarizer=OpenAICompatibleSummarizer(settings),
    )


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or (services.settings if services else get_settings())
    if settings.environment == "production" and settings.jwt_secret == DEV_JWT_SECRET:
        raise RuntimeError("Set JWT_SECRET before running in production")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings.work_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title=settings.app_name, version="0.1.0", description=DESCRIPTION)
    app.state.services = services or build_services(settings)
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.max_upload_mb * 1024 * 1024)

    for module in (auth, users, categories, meetings):
        app.include_router(module.router)

    @app.get("/healthz", tags=["health"])
    def healthz():
        return {"status": "ok"}

    return app

"""Command-line tools. They use DATABASE_URL and SECRET_KEY from the environment or .env.

    python -m app.cli create-admin owner@example.com      # create or promote the first admin
    python -m app.cli try recording.m4a --title "Weekly sync" --attendee "Ahmed (CFO)"
    python -m app.cli try --transcript transcript.txt --prompt-file new-prompt.md

`try` runs transcription and/or the summary with the provider keys, models and prompt saved
in the admin settings, without creating a meeting.
"""

import argparse
import getpass
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import make_engine, make_session_factory
from app.models import User, UserRole
from app.runtime_settings import RuntimeSettings, load_runtime_settings
from app.security import SecretBox, hash_password
from app.services.pipeline import transcribe_audio
from app.services.summarizer import MeetingContext, OpenAICompatibleSummarizer
from app.services.transcription import OpenAICompatibleTranscriber


def make_admin(db: Session, email: str, password: str | None = None, full_name: str | None = None) -> bool:
    """Promote an existing account to admin, or create one. Returns True if it was created."""
    email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    created = user is None
    if created:
        if not password or len(password) < 8:
            raise ValueError("A password of at least 8 characters is required for a new account")
        user = User(email=email, password_hash=hash_password(password), full_name=full_name)
        db.add(user)
    user.role = UserRole.ADMIN
    user.is_active = True
    db.commit()
    return created


def create_admin_command(args: argparse.Namespace) -> None:
    session = make_session_factory(make_engine(get_settings().database_url))()
    with session as db:
        exists = db.scalar(select(User.id).where(User.email == args.email.strip().lower()))
        password = None
        if not exists:
            password = getpass.getpass(f"New account {args.email}. Password: ")
            if getpass.getpass("Repeat password: ") != password:
                sys.exit("Passwords do not match")
        try:
            created = make_admin(db, args.email, password, args.name)
        except ValueError as exc:
            sys.exit(str(exc))
    print(f"{'Created' if created else 'Promoted'} admin account {args.email}")


def try_command(args: argparse.Namespace) -> None:
    settings = get_settings()
    session = make_session_factory(make_engine(settings.database_url))()
    with session as db:
        runtime = load_runtime_settings(db, SecretBox(settings.secret_key))
    if args.prompt_file:
        prompt = args.prompt_file.read_text(encoding="utf-8")
        try:
            runtime = RuntimeSettings.model_validate({**runtime.model_dump(), "summary_prompt": prompt})
        except ValidationError as exc:
            sys.exit(f"Invalid prompt: {exc.errors()[0]['msg']}")

    if args.transcript:
        transcript = args.transcript.read_text(encoding="utf-8")
    else:
        language = runtime.language_hint if args.language is None else args.language
        work_dir = Path(tempfile.mkdtemp(prefix="meeting-cli-"))
        try:
            transcript, _ = transcribe_audio(
                OpenAICompatibleTranscriber(runtime),
                args.audio,
                work_dir,
                None if language == "auto" else language,
                runtime.transcription_chunk_seconds,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        print("=== transcript ===\n" + transcript + "\n", file=sys.stderr)

    context = MeetingContext(title=args.title, meeting_date=args.date, attendees=args.attendee)
    summary = OpenAICompatibleSummarizer(runtime).summarize(transcript, context)
    print(summary.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    admin = commands.add_parser("create-admin", help="create an admin account or promote an existing one")
    admin.add_argument("email")
    admin.add_argument("--name", help="full name, for a new account")
    admin.set_defaults(handler=create_admin_command)

    trial = commands.add_parser("try", help="transcribe and summarize a local file")
    trial.add_argument("audio", nargs="?", type=Path, help="recording to transcribe and summarize")
    trial.add_argument("--transcript", type=Path, help="summarize this text file instead of audio")
    trial.add_argument("--prompt-file", type=Path, help="try a different summary prompt")
    trial.add_argument("--language", help='speech language hint, e.g. "ar"; "auto" to detect')
    trial.add_argument("--title")
    trial.add_argument("--date", type=date.fromisoformat, default=date.today())
    trial.add_argument("--attendee", action="append", default=[])
    trial.set_defaults(handler=try_command)

    args = parser.parse_args()
    if args.command == "try" and bool(args.audio) == bool(args.transcript):
        trial.error("pass either an audio file or --transcript")
    args.handler(args)


if __name__ == "__main__":
    main()

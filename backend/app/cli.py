"""Try the pipeline on a local file with the real providers (reads .env), no database needed.

    python -m app.cli recording.m4a --title "Weekly sync" --attendee "Ahmed (CFO)"
    python -m app.cli --transcript transcript.txt      # summary only, e.g. to tune the prompt
"""

import argparse
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

from app.config import get_settings
from app.services.pipeline import transcribe_audio
from app.services.summarizer import MeetingContext, OpenAICompatibleSummarizer
from app.services.transcription import OpenAICompatibleTranscriber


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audio", nargs="?", type=Path, help="recording to transcribe and summarize")
    parser.add_argument("--transcript", type=Path, help="summarize this text file instead of audio")
    parser.add_argument("--language", default=None, help='speech language hint, e.g. "ar"; "auto" to detect')
    parser.add_argument("--title")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--attendee", action="append", default=[])
    args = parser.parse_args()
    if bool(args.audio) == bool(args.transcript):
        parser.error("pass either an audio file or --transcript")

    settings = get_settings()
    if args.transcript:
        transcript = args.transcript.read_text(encoding="utf-8")
    else:
        language = settings.transcription_language if args.language is None else args.language
        work_dir = Path(tempfile.mkdtemp(prefix="meeting-cli-"))
        try:
            transcript = transcribe_audio(
                OpenAICompatibleTranscriber(settings),
                args.audio,
                work_dir,
                None if language == "auto" else language,
                settings.transcription_chunk_seconds,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        print("=== transcript ===\n" + transcript + "\n", file=sys.stderr)

    context = MeetingContext(title=args.title, meeting_date=args.date, attendees=args.attendee)
    summary = OpenAICompatibleSummarizer(settings).summarize(transcript, context)
    print(summary.model_dump_json(indent=2))


if __name__ == "__main__":
    main()

"""ffmpeg/ffprobe helpers. The phone can send mp4, m4a, 3gp/amr, ogg/opus, webm, wav, mp3..."""

import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

MIN_CHUNK_SECONDS = 1.0  # Whisper tends to hallucinate text on near-empty clips


class AudioError(Exception):
    pass


@dataclass
class AudioInfo:
    duration_seconds: float | None
    has_audio: bool


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"{cmd[0]} timed out") from exc


def probe(path: Path) -> AudioInfo:
    result = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json", str(path),
        ],
        timeout=60,
    )
    if result.returncode != 0:
        raise AudioError("The file is not a readable audio or video file")
    data = json.loads(result.stdout or "{}")
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    try:
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return AudioInfo(duration_seconds=duration, has_audio=has_audio)


def _ffmpeg(args: list[str], timeout: float) -> None:
    result = _run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args], timeout)
    if result.returncode != 0:
        raise AudioError(f"Could not decode the audio: {result.stderr.strip()[-300:]}")


@dataclass
class NormalizedAudio:
    path: Path
    duration_seconds: float


def normalize(src: Path, out_dir: Path) -> NormalizedAudio:
    """Extract the first audio track as 16 kHz mono FLAC.

    Whisper works on 16 kHz mono internally, so this loses nothing. Decoding everything once
    also gives a reliable length for files whose headers lack one (e.g. Android webm).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "normalized.flac"
    _ffmpeg(["-i", str(src), "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "flac",
             str(path)], timeout=900)
    duration = probe(path).duration_seconds
    if not duration:
        raise AudioError("Could not determine the length of the recording")
    return NormalizedAudio(path, duration)


def iter_chunks(audio: NormalizedAudio, chunk_seconds: int) -> Iterator[Path]:
    """Yield fixed-length chunks, well under the 25 MB per-request limit of hosted Whisper.

    Chunks are cut one at a time so the caller can delete each after use (Cloud Run's /tmp
    lives in memory); the normalized file is deleted at the end.
    """
    out_dir = audio.path.parent
    if audio.duration_seconds <= chunk_seconds:
        first_chunk = out_dir / "chunk_0000.flac"
        audio.path.rename(first_chunk)
        yield first_chunk
        return

    try:
        index, start = 0, 0
        while audio.duration_seconds - start >= MIN_CHUNK_SECONDS:
            chunk = out_dir / f"chunk_{index:04d}.flac"
            # Re-encoding (cheap for FLAC) writes a correct length into each chunk's header.
            _ffmpeg(["-ss", str(start), "-i", str(audio.path), "-t", str(chunk_seconds),
                     "-c:a", "flac", str(chunk)], timeout=300)
            yield chunk
            index, start = index + 1, start + chunk_seconds
    finally:
        audio.path.unlink(missing_ok=True)

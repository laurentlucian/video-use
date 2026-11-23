from __future__ import annotations

import json
from pathlib import Path


def resolve_transcripts_dir(edit_dir: Path, explicit: Path | None = None) -> Path:
    return (explicit or edit_dir / "transcripts").resolve()


def load_transcript(transcripts_dir: Path, source: str) -> dict | None:
    path = transcripts_dir / f"{source}.json"
    return json.loads(path.read_text()) if path.exists() else None


def spoken_words(transcript: dict) -> list[dict]:
    return [
        w for w in transcript.get("words", [])
        if w.get("type", "word") == "word" and (w.get("text") or "").strip()
        and w.get("start") is not None and w.get("end") is not None
    ]

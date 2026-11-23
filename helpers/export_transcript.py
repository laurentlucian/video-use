from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from render import _srt_timestamp, chunk_words


def spoken_words(transcript: dict) -> list[dict]:
    return [
        w for w in transcript.get("words", [])
        if w.get("type", "word") == "word" and (w.get("text") or "").strip()
        and w.get("start") is not None and w.get("end") is not None
    ]


def srt_text(words: list[dict]) -> str:
    lines: list[str] = []
    for i, chunk in enumerate(chunk_words(words), start=1):
        start, end = chunk[0]["start"], max(chunk[-1]["end"], chunk[0]["start"] + 0.4)
        text = re.sub(r"\s+", " ", " ".join(w["text"].strip() for w in chunk)).strip()
        lines += [str(i), f"{_srt_timestamp(start)} --> {_srt_timestamp(end)}", text, ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Export a cached transcript as word timings JSON and SRT for other tools "
                    "(ffmpeg-skill: silence.py --words, caption.py --srt)")
    ap.add_argument("transcript", type=Path, help="<edit_dir>/transcripts/<name>.json")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="Output directory (default: <edit_dir>/exports)")
    args = ap.parse_args()

    src = args.transcript.resolve()
    if not src.exists():
        sys.exit(f"transcript not found: {src}")
    out_dir = (args.out_dir or src.parent.parent / "exports").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    words = spoken_words(json.loads(src.read_text()))
    words_path = out_dir / f"{src.stem}.words.json"
    srt_path = out_dir / f"{src.stem}.srt"
    words_path.write_text(json.dumps(
        {"words": [{"word": w["text"], "start": w["start"], "end": w["end"]} for w in words]},
        indent=1))
    srt_path.write_text(srt_text(words))
    print(f"{len(words)} words → {words_path}")
    print(f"srt → {srt_path}")


if __name__ == "__main__":
    main()

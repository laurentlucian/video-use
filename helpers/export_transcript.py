from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from captions import chunk_words
from transcripts import spoken_words


def srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt_text(words: list[dict]) -> str:
    lines: list[str] = []
    for i, chunk in enumerate(chunk_words(words), start=1):
        start, end = chunk[0]["start"], max(chunk[-1]["end"], chunk[0]["start"] + 0.4)
        text = re.sub(r"\s+", " ", " ".join(w["text"].strip() for w in chunk)).strip()
        lines += [str(i), f"{srt_timestamp(start)} --> {srt_timestamp(end)}", text, ""]
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

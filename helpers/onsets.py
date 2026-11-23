from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import librosa
import numpy as np

from transcripts import spoken_words

SAMPLE_RATE = 16000
WINDOW_S = 0.02
HOP_S = 0.005
FLOOR_SPAN_S = 1.0
FLOOR_PERCENTILE = 10
RISE_DB = 12.0
PEAK_HEADROOM_DB = 6.0
END_GUARD_S = 0.03
MIN_SPEECH_S = 0.06
SILENT_DB = -120.0


def load_audio(video: Path, start: float, end: float, sr: int = SAMPLE_RATE) -> tuple[np.ndarray, float]:
    t0 = max(0.0, start)
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{end - t0:.3f}", "-i", str(video),
         "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
        capture_output=True, check=True,
    )
    return np.frombuffer(out.stdout, dtype=np.float32), t0


def rms_db(audio: np.ndarray, sr: int) -> np.ndarray:
    if audio.size == 0:
        return np.full(1, SILENT_DB)
    rms = librosa.feature.rms(y=audio, frame_length=int(WINDOW_S * sr), hop_length=int(HOP_S * sr))[0]
    return 20.0 * np.log10(np.maximum(rms, 1e-6))


def sustained_onset(loud: np.ndarray, last: int) -> int | None:
    run = round(MIN_SPEECH_S / HOP_S)
    for i in np.flatnonzero(loud[:last + 1]):
        if loud[i:i + run].all():
            return int(i)
    return None


def refine_onsets(words: list[dict], audio: np.ndarray, audio_start: float, sr: int = SAMPLE_RATE) -> list[dict]:
    db = rms_db(audio, sr)

    def frame(t: float) -> int:
        return int(np.clip(round((t - audio_start) / HOP_S), 0, db.size - 1))

    refined: list[dict] = []
    for w in words:
        start, end = float(w["start"]), float(w["end"])
        latest = end - END_GUARD_S
        lo = frame(start)
        if frame(latest) <= lo:
            refined.append(w)
            continue
        floor = np.percentile(db[frame(start - FLOOR_SPAN_S):frame(end + FLOOR_SPAN_S) + 1], FLOOR_PERCENTILE)
        peak = db[lo:frame(end) + 1].max()
        threshold = min(floor + RISE_DB, peak - PEAK_HEADROOM_DB)
        onset_frame = sustained_onset(db[lo:] >= threshold, frame(end) - 1 - lo)
        onset = audio_start + (lo + onset_frame) * HOP_S if onset_frame is not None else start
        refined.append({**w, "start": round(min(max(onset, start), latest), 3)})
    return refined


def refine_range(video: Path, words: list[dict], start: float, end: float) -> list[dict]:
    if not words:
        return []
    lo = min(start, min(float(w["start"]) for w in words)) - FLOOR_SPAN_S
    hi = max(end, max(float(w["end"]) for w in words)) + FLOOR_SPAN_S
    audio, audio_start = load_audio(video, lo, hi)
    return refine_onsets(words, audio, audio_start)


def main() -> None:
    ap = argparse.ArgumentParser(description="Print transcript word starts refined to the audio onset")
    ap.add_argument("video", type=Path)
    ap.add_argument("transcript", type=Path)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=float("inf"))
    args = ap.parse_args()

    words = [w for w in spoken_words(json.loads(args.transcript.read_text()))
             if w["end"] > args.start and w["start"] < args.end]
    end = min(args.end, max((w["end"] for w in words), default=args.start))
    for before, after in zip(words, refine_range(args.video.resolve(), words, args.start, end)):
        shift = after["start"] - before["start"]
        mark = f"  +{shift:.2f}" if shift >= 0.005 else ""
        print(f"{before['start']:8.2f} → {after['start']:8.3f}  {after['end']:8.2f}  {after['text'].strip()}{mark}")


if __name__ == "__main__":
    main()

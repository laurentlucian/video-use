from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

DEFAULT_FPS = Fraction(30)
MAX_RATE_COMPONENT = 2_147_483_647
CENTRE = (0.5, 0.5)


def parse_fps(value: str | int | float) -> Fraction:
    text = str(value).strip()
    if len(text) > 32 or not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+)?|[0-9]+/[0-9]+)", text):
        raise argparse.ArgumentTypeError("fps must be a positive number or rational, e.g. 30 or 30000/1001")
    try:
        rate = Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError("fps must be a positive number or rational") from exc
    if rate <= 0:
        raise argparse.ArgumentTypeError("fps must be greater than zero")
    if rate.numerator > MAX_RATE_COMPONENT or rate.denominator > MAX_RATE_COMPONENT:
        raise argparse.ArgumentTypeError("fps precision or magnitude is too large")
    return rate


def ffmpeg_rate(rate: Fraction) -> str:
    return f"{rate.numerator}/{rate.denominator}"


def resolve_path(maybe_path: str, base: Path) -> Path:
    p = Path(maybe_path)
    return p if p.is_absolute() else (base / p).resolve()


@dataclass(frozen=True)
class Reframe:
    at: float
    zoom: float
    ms: int


@dataclass(frozen=True)
class Cut:
    index: int
    source: str
    path: Path
    start: float
    frames: int
    offset_frames: int
    fps: Fraction
    zoom: float
    anchor: tuple[float, float]
    reframes: tuple[Reframe, ...]
    freeze_head: float
    note: str

    @property
    def duration(self) -> float:
        return float(self.frames / self.fps)

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def offset(self) -> float:
        return float(self.offset_frames / self.fps)

    def to_output(self, source_time: float) -> float:
        return self.offset + source_time - self.start


def parse_anchor(value: object, where: str) -> tuple[float, float]:
    if value is None:
        return CENTRE
    if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, (int, float)) for v in value)):
        sys.exit(f"{where}: anchor must be [x, y] fractions of the source frame")
    x, y = float(value[0]), float(value[1])
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        sys.exit(f"{where}: anchor values must be within 0..1")
    return x, y


def parse_zoom(value: object, where: str) -> float:
    zoom = float(value) if value is not None else 1.0
    if zoom < 1.0:
        sys.exit(f"{where}: zoom must be >= 1.0")
    return zoom


def parse_reframes(value: object, duration: float, where: str) -> tuple[Reframe, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        sys.exit(f"{where}: reframes must be a list of {{at_s, zoom, ms}}")
    reframes: list[Reframe] = []
    for i, r in enumerate(value):
        here = f"{where} reframe {i}"
        if not isinstance(r, dict) or "at_s" not in r or "zoom" not in r:
            sys.exit(f"{here}: needs at_s and zoom")
        reframe = Reframe(at=float(r["at_s"]), zoom=parse_zoom(r["zoom"], here), ms=int(r.get("ms", 450)))
        if reframe.ms < 1 or not 0.0 <= reframe.at < duration:
            sys.exit(f"{here}: at_s must fall inside the range and ms must be positive")
        if reframes and reframe.at < reframes[-1].at + reframes[-1].ms / 1000:
            sys.exit(f"{here}: starts before the previous reframe ends")
        reframes.append(reframe)
    return tuple(reframes)


def parse_freeze(value: object, duration: float, where: str) -> float:
    freeze = float(value) if value is not None else 0.0
    if not 0.0 <= freeze < duration:
        sys.exit(f"{where}: freeze_head_s must be >= 0 and shorter than the range")
    return freeze


def edl_fps(edl: dict) -> Fraction:
    value = edl.get("fps")
    if value is None:
        return DEFAULT_FPS
    try:
        return parse_fps(value)
    except argparse.ArgumentTypeError as exc:
        sys.exit(f"edl fps: {exc}")


def plan_cuts(edl: dict, edit_dir: Path) -> list[Cut]:
    fps = edl_fps(edl)
    cuts: list[Cut] = []
    offset = 0
    for i, r in enumerate(edl["ranges"]):
        where = f"range {i}"
        start = float(r["start"])
        frames = round((float(r["end"]) - start) * fps)
        if frames < 1:
            sys.exit(f"{where}: shorter than one frame at {ffmpeg_rate(fps)} fps")
        cuts.append(Cut(
            index=i,
            source=r["source"],
            path=resolve_path(edl["sources"][r["source"]], edit_dir),
            start=start,
            frames=frames,
            offset_frames=offset,
            fps=fps,
            zoom=parse_zoom(r.get("zoom"), where),
            anchor=parse_anchor(r.get("anchor"), where),
            reframes=parse_reframes(r.get("reframes"), float(frames / fps), where),
            freeze_head=parse_freeze(r.get("freeze_head_s"), float(frames / fps), where),
            note=r.get("beat") or r.get("note") or "",
        ))
        offset += frames
    return cuts


def total_frames(cuts: list[Cut]) -> int:
    return sum(c.frames for c in cuts)


def load_edl(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"edl not found: {path}")
    return json.loads(path.read_text())

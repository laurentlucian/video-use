from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fontTools.ttLib import TTFont
from PIL import ImageFont

from edl import Cut
from onsets import refine_range
from transcripts import load_transcript, spoken_words

DEFAULT_STYLE = Path(__file__).resolve().parent.parent / "styles" / "default.json"

PUNCT_BREAK = set(".,!?;:")
DISPLAY_STRIP = re.compile(r"[.,!?;:…\"“”{}\\]")
CHUNK_MIN_S = 0.35
CHUNK_PAUSE_S = 0.3
EDGE_OVERLAP_S = 0.06
MIN_WORD_S = 0.08
HOLD_S = 1.0


def chunk_words(
    words: list[dict],
    words_max: int = 3,
    fits: Callable[[list[dict]], bool] | None = None,
) -> list[list[dict]]:
    target = max(1, words_max - 1)
    words = [w for w in words if (w.get("text") or "").strip()]
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for i, w in enumerate(words):
        current.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["start"] - w["end"]) if nxt else 0.0
        dur = w["end"] - current[0]["start"]
        if (
            nxt is None
            or w["text"].strip()[-1] in PUNCT_BREAK
            or gap >= CHUNK_PAUSE_S
            or len(current) >= words_max
            or (len(current) >= target and dur >= CHUNK_MIN_S)
            or (fits is not None and not fits(current + [nxt]))
        ):
            chunks.append(current)
            current = []
    return chunks


@dataclass(frozen=True)
class CaptionStyle:
    canvas_w: int
    canvas_h: int
    face: str
    weight: int
    font_file: Path
    size: int
    case: str
    words_max: int
    max_width: float
    y: float
    outline: float
    shadow: float
    shadow_alpha: float
    text_color: str
    outline_color: str
    active_color: str
    active_scale: float
    pop_ms: int
    pop_from: float

    @property
    def fonts_dir(self) -> Path:
        return self.font_file.parent


def font_face(font_file: Path) -> tuple[str, int]:
    font = TTFont(font_file, fontNumber=0)
    return font["name"].getDebugName(4) or font["name"].getDebugName(1), font["OS/2"].usWeightClass


def load_style(path: Path) -> CaptionStyle:
    path = path.resolve()
    if not path.exists():
        sys.exit(f"style not found: {path}")
    data = json.loads(path.read_text())
    font = data["fonts"]["caption"]
    font_file = (path.parent / font["file"]).resolve()
    if not font_file.exists():
        sys.exit(f"caption font not found: {font_file}")
    face, weight = font_face(font_file)
    c = data["captions"]
    return CaptionStyle(
        canvas_w=int(data["canvas"]["w"]),
        canvas_h=int(data["canvas"]["h"]),
        face=face,
        weight=weight,
        font_file=font_file,
        size=int(c["size"]),
        case=c.get("case", "natural"),
        words_max=int(c.get("words_max", 3)),
        max_width=float(c.get("max_width", 0.9)),
        y=float(c["y"]),
        outline=float(c.get("outline", 0)),
        shadow=float(c.get("shadow", 0)),
        shadow_alpha=float(c.get("shadow_alpha", 1.0)),
        text_color=data["colors"]["text"],
        outline_color=data["colors"]["outline"],
        active_color=c["active_color"],
        active_scale=float(c.get("active_scale", 1.0)),
        pop_ms=int(c.get("pop_ms", 0)),
        pop_from=float(c.get("pop_from", 1.0)),
    )


def ass_color(hex_rgb: str, alpha: float = 1.0) -> str:
    rgb = hex_rgb.lstrip("#")
    return f"&H{round((1 - alpha) * 255):02X}{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}".upper()


def tag_color(hex_rgb: str) -> str:
    rgb = hex_rgb.lstrip("#")
    return f"&H{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&".upper()


def ass_time(cs: int) -> str:
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def replacer(replacements: dict[str, str]) -> Callable[[str], str]:
    if not replacements:
        return lambda text: text
    keys = sorted(replacements, key=len, reverse=True)
    lookup = {k.lower(): v for k, v in replacements.items()}
    pattern = re.compile(r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b", re.IGNORECASE)
    return lambda text: pattern.sub(lambda m: lookup[m.group(0).lower()], text)


def display_text(raw: str, case: str, replace: Callable[[str], str]) -> str:
    text = DISPLAY_STRIP.sub("", replace(raw.strip()))
    return text.upper() if case == "upper" else text.lower() if case == "lower" else text


def output_words(
    cuts: list[Cut],
    transcripts_dir: Path,
    refine: bool,
    case: str,
    replace: Callable[[str], str],
) -> list[dict]:
    words: list[dict] = []
    for cut in cuts:
        transcript = load_transcript(transcripts_dir, cut.source)
        if transcript is None:
            print(f"  no transcript for {cut.source} in {transcripts_dir}, no captions for range {cut.index}")
            continue
        inside = [w for w in spoken_words(transcript)
                  if w["end"] >= cut.start + EDGE_OVERLAP_S and w["start"] <= cut.end - EDGE_OVERLAP_S]
        if refine:
            inside = refine_range(cut.path, inside, cut.start, cut.end)
        for w in inside:
            text = display_text(w["text"], case, replace)
            if not text:
                continue
            start = cut.to_output(max(cut.start, w["start"]))
            end = cut.to_output(min(cut.end, w["end"]))
            words.append({"text": w["text"], "display": text, "start": start, "end": max(end, start + MIN_WORD_S)})
    return words


def word_tag(style: CaptionStyle, active: bool, pop: bool) -> str:
    color = tag_color(style.active_color if active else style.text_color)
    scale = round(100 * (style.active_scale if active else 1.0))
    if not pop:
        return rf"{{\1c{color}\fscx{scale}\fscy{scale}}}"
    start = round(scale * style.pop_from)
    return rf"{{\1c{color}\fscx{start}\fscy{start}\t(0,{style.pop_ms},\fscx{scale}\fscy{scale})}}"


def build_ass(
    cuts: list[Cut],
    transcripts_dir: Path,
    style: CaptionStyle,
    replacements: dict[str, str],
    refine: bool,
    out_path: Path,
) -> int:
    font = ImageFont.truetype(str(style.font_file), style.size)
    limit = style.max_width * style.canvas_w
    total = cuts[-1].offset + cuts[-1].duration if cuts else 0.0

    def width(cue: list[dict]) -> float:
        return font.getlength(" ".join(w["display"] for w in cue))

    words = output_words(cuts, transcripts_dir, refine, style.case, replacer(replacements))
    cues = chunk_words(words, style.words_max, fits=lambda cue: width(cue) <= limit)

    shadow = ass_color(style.outline_color, style.shadow_alpha)
    lines = [
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {style.canvas_w}", f"PlayResY: {style.canvas_h}",
        "ScaledBorderAndShadow: yes", "WrapStyle: 2", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{style.face},{style.size},{ass_color(style.text_color)},{ass_color(style.text_color)},"
        f"{ass_color(style.outline_color)},{shadow},{style.weight},0,0,0,100,100,0,0,1,"
        f"{style.outline:g},{style.shadow:g},5,0,0,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    position = rf"\an5\pos({style.canvas_w // 2},{round(style.y * style.canvas_h)})"
    events = 0
    for ci, cue in enumerate(cues):
        next_start = cues[ci + 1][0]["start"] if ci + 1 < len(cues) else total
        cue_end = min(next_start, cue[-1]["end"] + HOLD_S, total)
        fs = style.size if width(cue) <= limit else int(style.size * limit / width(cue))
        for wi, w in enumerate(cue):
            a = round(w["start"] * 100)
            b = round((cue[wi + 1]["start"] if wi + 1 < len(cue) else cue_end) * 100)
            if b <= a:
                continue
            text = " ".join(word_tag(style, j == wi, wi == 0) + x["display"] for j, x in enumerate(cue))
            lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Default,,0,0,0,,{{{position}\\fs{fs}}}{text}")
            events += 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    print(f"captions → {out_path.name} ({len(cues)} cues, {len(words)} words, {events} events)")
    return events

from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Callable

from fontTools.ttLib import TTFont
from PIL import ImageFont

from edl import Cut
from onsets import refine_range
from transcripts import load_transcript, spoken_words

DEFAULT_STYLE = Path(__file__).resolve().parent.parent / "styles" / "default.json"

PUNCT_BREAK = set(".,!?;:…")
SENTENCE_END = set(".!?…")
END_PUNCTUATION = ".,!?;:…"
DISPLAY_STRIP = re.compile(r"[\"“”{}\\]")
PHRASE_PAUSE_S = 0.3
MERGE_PAUSE_S = 0.6
EDGE_OVERLAP_S = 0.06
MIN_WORD_S = 0.08
MIN_VISIBLE_S = 0.2
HOLD_S = 1.0

Window = tuple[float, float]
Cue = list[dict]


def split_phrases(words: list[dict]) -> list[Cue]:
    phrases: list[Cue] = []
    current: Cue = []
    for i, w in enumerate(words):
        current.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is None or w["text"].strip()[-1] in PUNCT_BREAK or nxt["start"] - w["end"] >= PHRASE_PAUSE_S:
            phrases.append(current)
            current = []
    return phrases


def ends_sentence(cue: Cue) -> bool:
    return cue[-1]["text"].strip()[-1] in SENTENCE_END


def merge_short(phrases: list[Cue], words_min: int) -> list[Cue]:
    groups: list[Cue] = []
    for p in phrases:
        short = groups and not ends_sentence(groups[-1]) and (len(groups[-1]) < words_min or len(p) < words_min)
        if short and p[0]["start"] - groups[-1][-1]["end"] < MERGE_PAUSE_S:
            groups[-1] = groups[-1] + p
        else:
            groups.append(list(p))
    return groups


def split_even(group: Cue, words_max: int, fits: Callable[[Cue], bool]) -> list[Cue]:
    n = math.ceil(len(group) / words_max)
    while True:
        size, extra = divmod(len(group), n)
        bounds = [0]
        for i in range(n):
            bounds.append(bounds[-1] + size + (i < extra))
        parts = [group[a:b] for a, b in zip(bounds, bounds[1:])]
        if n == len(group) or all(fits(p) for p in parts):
            return parts
        n += 1


def chunk_words(
    words: list[dict],
    words_min: int = 1,
    words_max: int = 3,
    fits: Callable[[Cue], bool] = lambda cue: True,
) -> list[Cue]:
    words = [w for w in words if (w.get("text") or "").strip()]
    groups = merge_short(split_phrases(words), words_min)
    return [cue for g in groups for cue in split_even(g, words_max, fits)]


@dataclass(frozen=True)
class CaptionStyle:
    canvas_w: int
    canvas_h: int
    face: str
    weight: int
    font_file: Path
    em_scale: float
    size: int
    tracking: float
    case: str
    words_min: int
    words_max: int
    lines_max: int
    max_width: float
    x: float
    y: float
    outline: float
    shadow: float
    shadow_blur: float
    shadow_alpha: float
    text_color: str
    outline_color: str
    active_color: str | None
    active_scale: float
    pop_ms: int
    pop_from: float
    end_strip: str

    @property
    def fonts_dir(self) -> Path:
        return self.font_file.parent


def font_face(font_file: Path) -> tuple[str, int, float]:
    font = TTFont(font_file, fontNumber=0)
    os2, hhea = font["OS/2"], font["hhea"]
    height = (os2.usWinAscent + os2.usWinDescent) or (hhea.ascent - hhea.descent)
    name = font["name"].getDebugName(4) or font["name"].getDebugName(1)
    return name, os2.usWeightClass, font["head"].unitsPerEm / height


def end_strip_chars(c: dict, case: str) -> str:
    if not c.get("strip_end_punctuation", case == "lower"):
        return ""
    keep = c.get("end_punctuation_keep", "")
    return "".join(ch for ch in END_PUNCTUATION if ch not in keep)


def load_style(path: Path) -> CaptionStyle:
    path = path.resolve()
    if not path.exists():
        sys.exit(f"style not found: {path}")
    data = json.loads(path.read_text())
    font = data["fonts"]["caption"]
    font_file = (path.parent / font["file"]).resolve()
    if not font_file.exists():
        sys.exit(f"caption font not found: {font_file}")
    face, weight, em_scale = font_face(font_file)
    c = data["captions"]
    case = c.get("case", "natural")
    return CaptionStyle(
        canvas_w=int(data["canvas"]["w"]),
        canvas_h=int(data["canvas"]["h"]),
        face=face,
        weight=weight,
        em_scale=em_scale,
        font_file=font_file,
        size=int(c["size"]),
        tracking=float(font.get("tracking", 0.0)),
        case=case,
        words_min=int(c.get("words_min", 1)),
        words_max=int(c.get("words_max", 3)),
        lines_max=int(c.get("lines_max", 1)),
        max_width=float(c.get("max_width", 0.9)),
        x=float(c.get("x", 0.5)),
        y=float(c["y"]),
        outline=float(c.get("outline", 0)),
        shadow=float(c.get("shadow", 0)),
        shadow_blur=float(c.get("shadow_blur", 0)),
        shadow_alpha=float(c.get("shadow_alpha", 1.0)),
        text_color=data["colors"]["text"],
        outline_color=data["colors"]["outline"],
        active_color=c.get("active_color"),
        active_scale=float(c.get("active_scale", 1.0)),
        pop_ms=int(c.get("pop_ms", 0)),
        pop_from=float(c.get("pop_from", 1.0)),
        end_strip=end_strip_chars(c, case),
    )


def ass_color(hex_rgb: str, alpha: float = 1.0) -> str:
    rgb = hex_rgb.lstrip("#")
    return f"&H{round((1 - alpha) * 255):02X}{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}".upper()


def tag_color(hex_rgb: str) -> str:
    rgb = hex_rgb.lstrip("#")
    return f"&H{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&".upper()


def tag_alpha(alpha: float) -> str:
    return f"&H{round((1 - alpha) * 255):02X}&"


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
    text = DISPLAY_STRIP.sub("", raw).strip()
    return replace(text.upper() if case == "upper" else text.lower() if case == "lower" else text)


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
            if not text.strip(END_PUNCTUATION):
                continue
            start = cut.to_output(max(cut.start, w["start"]))
            end = cut.to_output(min(cut.end, w["end"]))
            words.append({"text": w["text"], "display": text, "start": start, "end": max(end, start + MIN_WORD_S)})
    return words


class Layout:
    def __init__(self, style: CaptionStyle):
        self.style = style
        self.font = ImageFont.truetype(str(style.font_file), style.size * style.em_scale)
        self.spacing = style.tracking * style.size * style.em_scale
        self.limit = style.max_width * style.canvas_w

    def width(self, text: str) -> float:
        return self.font.getlength(text) + self.spacing * max(0, len(text) - 1)

    def texts(self, cue: Cue) -> list[str]:
        texts = [w["display"] for w in cue]
        texts[-1] = texts[-1].rstrip(self.style.end_strip) or texts[-1]
        return texts

    def lines(self, texts: list[str]) -> list[tuple[int, int]] | None:
        for k in range(1, min(self.style.lines_max, len(texts)) + 1):
            best: tuple[float, list[tuple[int, int]]] | None = None
            for breaks in combinations(range(1, len(texts)), k - 1):
                bounds = (0, *breaks, len(texts))
                spans = list(zip(bounds, bounds[1:]))
                widest = max(self.width(" ".join(texts[a:b])) for a, b in spans)
                if widest <= self.limit and (best is None or widest < best[0]):
                    best = (widest, spans)
            if best is not None:
                return best[1]
        return None

    def fits(self, cue: Cue) -> bool:
        return self.lines(self.texts(cue)) is not None

    def place(self, cue: Cue) -> tuple[list[str], list[tuple[int, int]], int]:
        texts = self.texts(cue)
        spans = self.lines(texts)
        if spans is not None:
            return texts, spans, self.style.size
        line = " ".join(texts)
        return texts, [(0, len(texts))], int(self.style.size * self.limit / self.width(line))


def visible(span: Window, hidden: list[Window]) -> list[Window]:
    pieces = [span]
    for hs, he in hidden:
        pieces = [p for s, e in pieces for p in ((s, min(e, hs)), (max(s, he), e)) if p[1] > p[0]]
    return [p for p in pieces if p == span or p[1] - p[0] >= MIN_VISIBLE_S]


def scale_tag(scale: float, pop: bool, style: CaptionStyle) -> str:
    target = round(100 * scale)
    if not pop or style.pop_ms <= 0:
        return rf"\fscx{target}\fscy{target}"
    start = round(target * style.pop_from)
    return rf"\fscx{start}\fscy{start}\t(0,{style.pop_ms},\fscx{target}\fscy{target})"


def cue_body(texts: list[str], spans: list[tuple[int, int]], style: CaptionStyle,
             active: int | None, pop: bool, color: str | None) -> str:
    def word(j: int) -> str:
        if active is None:
            return texts[j]
        hit = j == active
        fill = color or (style.active_color if hit else style.text_color)
        return f"{{\\1c{tag_color(fill)}{scale_tag(style.active_scale if hit else 1.0, pop, style)}}}{texts[j]}"

    lead = "" if active is not None else f"{{{scale_tag(1.0, pop, style)}}}"
    return lead + r"\N".join(" ".join(word(j) for j in range(a, b)) for a, b in spans)


def replace_phrases(words: list[dict], replacements: dict[str, str]) -> list[dict]:
    phrases = sorted(
        ((k.lower().split(), v.split()) for k, v in replacements.items() if len(k.split()) > 1),
        key=lambda kv: len(kv[0]), reverse=True,
    )
    for key, value in phrases:
        if len(key) != len(value):
            sys.exit(f"replacement '{' '.join(key)}' → '{' '.join(value)}': phrase replacements need the same word count")
    out = [dict(w) for w in words]
    bare = [w["display"].strip(END_PUNCTUATION).lower() for w in out]
    for key, value in phrases:
        for i in range(len(out) - len(key) + 1):
            if bare[i:i + len(key)] == key:
                for j, token in enumerate(value):
                    w = out[i + j]
                    core = w["display"].strip(END_PUNCTUATION)
                    w["display"] = w["display"].replace(core, token, 1)
                    bare[i + j] = token.lower()
    return out


def build_ass(
    cuts: list[Cut],
    transcripts_dir: Path,
    style: CaptionStyle,
    replacements: dict[str, str],
    refine: bool,
    hidden: list[Window],
    out_path: Path,
) -> int:
    layout = Layout(style)
    total = cuts[-1].offset + cuts[-1].duration if cuts else 0.0
    words = replace_phrases(output_words(cuts, transcripts_dir, refine, style.case, replacer(replacements)), replacements)
    cues = chunk_words(words, style.words_min, style.words_max, layout.fits)

    lines = [
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {style.canvas_w}", f"PlayResY: {style.canvas_h}",
        "ScaledBorderAndShadow: yes", "WrapStyle: 2", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{style.face},{style.size},{ass_color(style.text_color)},{ass_color(style.text_color)},"
        f"{ass_color(style.outline_color)},{ass_color(style.outline_color)},{style.weight},0,0,0,100,100,0,0,1,"
        f"{style.outline:g},0,5,0,0,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    x, y = round(style.x * style.canvas_w), round(style.y * style.canvas_h)
    shadowed = style.shadow_alpha > 0 and (style.shadow > 0 or style.shadow_blur > 0)
    shade = (rf"\1c{tag_color(style.outline_color)}\3c{tag_color(style.outline_color)}"
             rf"\1a{tag_alpha(style.shadow_alpha)}\3a{tag_alpha(style.shadow_alpha)}\blur{style.shadow_blur / 2:g}")
    events = 0
    dropped = 0

    def emit(a: float, b: float, texts: list[str], spans: list[tuple[int, int]], fs: int, active: int | None) -> None:
        nonlocal events, dropped
        pieces = visible((a, b), hidden)
        dropped += not pieces
        head = rf"\an5\fs{fs}\fsp{style.tracking * fs * style.em_scale:.2f}"
        for pa, pb in pieces:
            ca, cb = round(pa * 100), round(pb * 100)
            if cb <= ca:
                continue
            pop = pa == a and active in (None, 0)
            if shadowed:
                body = cue_body(texts, spans, style, active, pop, style.outline_color)
                lines.append(f"Dialogue: 0,{ass_time(ca)},{ass_time(cb)},Default,,0,0,0,,"
                             f"{{{head}\\pos({x},{y + round(style.shadow)}){shade}}}{body}")
            body = cue_body(texts, spans, style, active, pop, None)
            lines.append(f"Dialogue: 1,{ass_time(ca)},{ass_time(cb)},Default,,0,0,0,,{{{head}\\pos({x},{y})}}{body}")
            events += 1

    for ci, cue in enumerate(cues):
        next_start = cues[ci + 1][0]["start"] if ci + 1 < len(cues) else total
        cue_end = min(next_start, cue[-1]["end"] + HOLD_S, total)
        texts, spans, fs = layout.place(cue)
        if style.active_color is None:
            emit(cue[0]["start"], cue_end, texts, spans, fs, None)
            continue
        for wi, w in enumerate(cue):
            emit(w["start"], cue[wi + 1]["start"] if wi + 1 < len(cue) else cue_end, texts, spans, fs, wi)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    hid = f", {dropped} hidden by overlays" if hidden else ""
    print(f"captions → {out_path.name} ({len(cues)} cues, {len(words)} words, {events} events{hid})")
    return events

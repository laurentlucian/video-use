from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

from captions import DEFAULT_STYLE, build_ass, load_style
from edl import Cut, ffmpeg_rate, load_edl, plan_cuts, resolve_path, total_frames
from grade import auto_grade_for_clip, get_preset
from mix import audio_mix
from transcripts import resolve_transcripts_dir

AUDIO_RATE = 48000
FADE_S = 0.03
READ_PAD_S = 0.5

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}

TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)

QUALITY = {
    "final": {"long_side": 1920, "preset": "fast", "crf": "20"},
    "preview": {"long_side": 1920, "preset": "medium", "crf": "22"},
    "draft": {"long_side": 1280, "preset": "ultrafast", "crf": "28"},
}

STILL_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def ffprobe_stream(video: Path, entries: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", entries, "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    )
    streams = json.loads(out.stdout).get("streams") or []
    if not streams:
        sys.exit(f"no video stream in {video}")
    return streams[0]


def is_hdr_source(video: Path) -> bool:
    return ffprobe_stream(video, "stream=color_transfer").get("color_transfer") in HDR_TRANSFERS


def display_size(video: Path) -> tuple[int, int]:
    stream = ffprobe_stream(video, "stream=width,height:stream_side_data=rotation")
    w, h = int(stream["width"]), int(stream["height"])
    rotation = next((sd["rotation"] for sd in stream.get("side_data_list") or [] if sd.get("rotation") is not None), 0)
    return (h, w) if round(float(rotation)) % 180 else (w, h)


def output_size(video: Path, long_side: int) -> tuple[int, int]:
    w, h = display_size(video)
    short = round(min(w, h) * long_side / max(w, h) / 2) * 2
    return (short, long_side) if h > w else (long_side, short)


def resolve_grade_filter(grade_field: str | None) -> str:
    if not grade_field or grade_field == "auto":
        return grade_field or ""
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError:
            print(f"warning: unknown preset '{grade_field}', using as raw filter")
    return grade_field


def aspect_crop(cut: Cut, size: tuple[int, int], zoom: float) -> str:
    w, h = size
    crop_w = f"min(iw,ih*{w}/{h})/{zoom}"
    crop_h = f"min(iw,ih*{w}/{h})/{zoom}*{h}/{w}"
    ax, ay = cut.anchor
    return (
        f"crop=w='{crop_w}':h='{crop_h}'"
        f":x='max(0,min(iw-ow,iw*{ax}-ow/2))':y='max(0,min(ih-oh,ih*{ay}-oh/2))'"
    )


def eased(t: str, start: float, ms: int) -> str:
    p = f"clip(({t}-{start:.6f})/{ms / 1000:.6f},0,1)"
    return f"if(lt({p},0.5),4*pow({p},3),1-pow(2-2*{p},3)/2)"


def zoom_expr(cut: Cut) -> str:
    t = f"(on/{float(cut.fps):.6f})"
    expr, current = f"{cut.zoom}", cut.zoom
    for r in cut.reframes:
        expr += f"+({r.zoom - current:.6f})*{eased(t, r.at, r.ms)}"
        current = r.zoom
    return expr


def frame_filter(cut: Cut, size: tuple[int, int]) -> str:
    w, h = size
    rate = ffmpeg_rate(cut.fps)
    if not cut.reframes:
        return f"{aspect_crop(cut, size, cut.zoom)},scale={w}:{h}:flags=lanczos,setsar=1,fps={rate}"
    ax, ay = cut.anchor
    return (
        f"fps={rate},{aspect_crop(cut, size, 1.0)},"
        f"zoompan=z='{zoom_expr(cut)}'"
        f":x='max(0,min(iw-iw/zoom,iw*{ax}-iw/zoom/2))':y='max(0,min(ih-ih/zoom,ih*{ay}-ih/zoom/2))'"
        f":d=1:s={w}x{h}:fps={rate},setsar=1"
    )


def extract_segment(
    cut: Cut,
    grade_filter: str,
    audio_filter: str,
    size: tuple[int, int],
    quality: dict,
    out_path: Path,
) -> None:
    vf = [TONEMAP_CHAIN] if is_hdr_source(cut.path) else []
    vf.append(frame_filter(cut, size))
    if grade_filter:
        vf.append(grade_filter)

    samples = round(cut.frames / cut.fps * AUDIO_RATE)
    af = [f"aresample={AUDIO_RATE}"]
    if audio_filter:
        af.append(audio_filter)
    af += [
        "apad",
        f"atrim=end_sample={samples}",
        f"afade=t=in:st=0:d={FADE_S}",
        f"afade=t=out:st={max(0.0, cut.duration - FADE_S):.6f}:d={FADE_S}",
    ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{cut.start:.6f}", "-t", f"{cut.duration + READ_PAD_S:.6f}", "-i", str(cut.path),
        "-vf", ",".join(vf), "-af", ",".join(af),
        "-frames:v", str(cut.frames),
        "-c:v", "libx264", "-preset", quality["preset"], "-crf", quality["crf"],
        "-pix_fmt", "yuv420p", "-r", ffmpeg_rate(cut.fps),
        "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE),
        "-movflags", "+faststart",
        str(out_path),
    ], check=True)


def extract_all_segments(
    edl: dict,
    cuts: list[Cut],
    clips_dir: Path,
    size: tuple[int, int],
    quality: dict,
) -> list[Path]:
    grade = resolve_grade_filter(edl.get("grade"))
    audio_filter = edl.get("audio_filter") or ""
    clips_dir.mkdir(parents=True, exist_ok=True)
    print(f"extracting {len(cuts)} segment(s) → {clips_dir.name}/  {size[0]}x{size[1]} @ {ffmpeg_rate(cuts[0].fps)} fps")
    paths: list[Path] = []
    for cut in cuts:
        seg_filter = auto_grade_for_clip(cut.path, start=cut.start, duration=cut.duration)[0] if grade == "auto" else grade
        zoom = f"  zoom {cut.zoom:g}" if cut.zoom != 1.0 else ""
        print(f"  [{cut.index:02d}] {cut.source}  {cut.start:7.3f}-{cut.end:7.3f}  "
              f"({cut.frames}f @ {cut.offset:6.3f}){zoom}  {cut.note}")
        out_path = clips_dir / f"seg_{cut.index:02d}_{cut.source}.mp4"
        extract_segment(cut, seg_filter, audio_filter, size, quality, out_path)
        paths.append(out_path)
    return paths


def concat_segments(segment_paths: list[Path], out_path: Path, work_dir: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = work_dir / "_concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segment_paths))
    print(f"concat → {out_path.name}")
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", "-movflags", "+faststart", str(out_path),
    ], check=True)
    concat_list.unlink(missing_ok=True)


def filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def media_duration(path: Path) -> float | None:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def with_clip_durations(overlays: list[dict], edit_dir: Path) -> list[dict]:
    resolved = []
    for ov in overlays:
        path = resolve_path(ov["file"], edit_dir)
        clip = None if "duration" in ov or path.suffix.lower() in STILL_SUFFIXES else media_duration(path)
        resolved.append({**ov, "duration": clip} if clip else ov)
    return resolved


def overlay_window(
ov: dict, index: int, total_s: float) -> tuple[float, float]:
    start = float(ov["start_in_output"])
    duration = min(float(ov.get("duration", total_s - start)), total_s - start)
    if duration <= 0:
        sys.exit(f"overlay {index} starts at or after the end of the output ({total_s:.3f}s)")
    return start, start + duration


def caption_holes(overlays: list[dict], total_s: float) -> list[tuple[float, float]]:
    return [overlay_window(ov, i, total_s) for i, ov in enumerate(overlays) if ov.get("hide_captions", True)]


def overlay_input(path: Path, fps: Fraction, duration: float) -> list[str]:
    if path.suffix.lower() in STILL_SUFFIXES:
        return ["-loop", "1", "-framerate", ffmpeg_rate(fps), "-t", f"{duration:.6f}", "-i", str(path)]
    if path.suffix.lower() == ".webm":
        return ["-c:v", "libvpx-vp9", "-i", str(path)]
    return ["-i", str(path)]


def blur_windows(overlays: list[dict], total_s: float, current: str) -> tuple[list[str], str]:
    graph: list[str] = []
    for idx, ov in enumerate(overlays):
        blur = ov.get("blur_base")
        if not blur:
            continue
        start, end = overlay_window(ov, idx, total_s)
        ramp = max(float(blur.get("ramp_ms", 250)) / 1000, 1 / 1000)
        fades = f"fade=t=in:st={start:.6f}:d={ramp:.6f}:alpha=1"
        if end < total_s - 1e-6:
            fades += f",fade=t=out:st={max(start, end - ramp):.6f}:d={ramp:.6f}:alpha=1"
        graph.append(f"{current}split=2[bs{idx}][bb{idx}]")
        graph.append(
            f"[bb{idx}]trim=start={start:.6f}:end={end:.6f},gblur=sigma={float(blur.get('px', 30)):g},"
            f"format=yuva420p,{fades}[bl{idx}]"
        )
        graph.append(f"[bs{idx}][bl{idx}]overlay=eof_action=pass:enable='between(t,{start:.6f},{end:.6f})'[bv{idx}]")
        current = f"[bv{idx}]"
    return graph, current


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    captions: tuple[Path, Path] | None,
    edl: dict,
    out_path: Path,
    edit_dir: Path,
    fps: Fraction,
    total_s: float,
    size: tuple[int, int],
) -> None:
    mix = audio_mix(edl, edit_dir, len(overlays) + 1, total_s)
    audio_codec = ["-c:a", "pcm_s24le"] if out_path.suffix.lower() == ".mov" else ["-c:a", "aac", "-b:a", "192k"]
    if not overlays and captions is None and mix is None:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(base_path), "-c", "copy", str(out_path)], check=True)
        return

    inputs = ["-i", str(base_path)]
    graph, current = blur_windows(overlays, total_s, "[0:v]")
    w, h = size
    for idx, ov in enumerate(overlays, start=1):
        start, end = overlay_window(ov, idx - 1, total_s)
        duration = end - start
        inputs += overlay_input(resolve_path(ov["file"], edit_dir), fps, duration)
        scale = float(ov.get("scale", 1.0))
        px, py = ov.get("position", [0.5, 0.5])
        graph.append(
            f"[{idx}:v]trim=duration={duration:.6f},setpts=PTS-STARTPTS+{start:.6f}/TB,"
            f"format=rgba,scale=iw*{scale}:ih*{scale}[a{idx}]"
        )
        graph.append(
            f"{current}[a{idx}]overlay=x={w}*{px}-w/2:y={h}*{py}-h/2:eof_action=pass"
            f":enable='between(t,{start:.6f},{start + duration:.6f})'[v{idx}]"
        )
        current = f"[v{idx}]"

    if mix is not None:
        inputs += mix[0]
        graph += mix[1]

    if captions is not None:
        ass_path, fonts_dir = captions
        graph.append(f"{current}subtitles=filename='{filter_path(ass_path)}':fontsdir='{filter_path(fonts_dir)}'[outv]")
    else:
        graph.append(f"{current}null[outv]")

    print(f"compositing → {out_path.name}  (overlays: {len(overlays)}, captions: {'yes' if captions else 'no'}, mix: {'yes' if mix else 'no'})")
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", *inputs,
        "-filter_complex", ";".join(graph),
        "-map_chapters", "-1", "-map", "[outv]", "-map", mix[2] if mix else "0:a", "-frames:v", str(round(total_s * fps)),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", ffmpeg_rate(fps),
        *(audio_codec if mix else ["-c:a", "copy"]), "-movflags", "+faststart",
        str(out_path),
    ], check=True)


LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    proc = subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-i", str(video_path),
        "-af", f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json",
        "-vn", "-f", "null", "-",
    ], capture_output=True, text=True)
    start, end = proc.stderr.rfind("{"), proc.stderr.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(proc.stderr[start:end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    return data if needed.issubset(data) else None


def apply_loudnorm(input_path: Path, output_path: Path, two_pass: bool) -> None:
    target = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
    measurement = measure_loudness(input_path) if two_pass else None
    if measurement is not None:
        print(f"  loudnorm measured: I={measurement['input_i']} LUFS  "
              f"TP={measurement['input_tp']}  LRA={measurement['input_lra']}")
        target += (
            f":measured_I={measurement['input_i']}:measured_TP={measurement['input_tp']}"
            f":measured_LRA={measurement['input_lra']}:measured_thresh={measurement['input_thresh']}"
            f":offset={measurement['target_offset']}:linear=true"
        )
    print(f"  loudnorm ({'2-pass' if measurement else '1-pass'}) → {output_path.name}")
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(input_path), "-c:v", "copy", "-map_chapters", "-1",
        "-af", f"{target},aresample={AUDIO_RATE}",
        "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE), "-movflags", "+faststart",
        str(output_path),
    ], check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="1080p, medium, CRF 22")
    mode.add_argument("--draft", action="store_true", help="720p, ultrafast, CRF 28, 1-pass loudnorm")
    ap.add_argument("--style", type=Path, nargs="?", const=DEFAULT_STYLE, default=None,
                    help=f"Burn word-by-word captions with this style JSON (bare flag: {DEFAULT_STYLE})")
    ap.add_argument("--transcripts-dir", type=Path, default=None,
                    help="Transcript JSON directory (default: <edl_dir>/transcripts)")
    ap.add_argument("--no-refine-onsets", action="store_true",
                    help="Use raw transcript word starts for captions")
    ap.add_argument("--no-loudnorm", action="store_true", help="Skip -14 LUFS / -1 dBTP normalization")
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    edl = load_edl(edl_path)
    edit_dir = edl_path.parent
    out_path = args.output.resolve()
    work_dir = out_path.parent
    level = "draft" if args.draft else "preview" if args.preview else "final"
    quality = QUALITY[level]

    cuts = plan_cuts(edl, edit_dir)
    if not cuts:
        sys.exit("edl has no ranges")
    size = output_size(cuts[0].path, quality["long_side"])
    fps = cuts[0].fps
    frames = total_frames(cuts)
    total_s = float(frames / fps)

    clips_dir = work_dir / ("clips_graded" if level == "final" else f"clips_{level}")
    segments = extract_all_segments(edl, cuts, clips_dir, size, quality)
    base_path = work_dir / ("base.mp4" if level == "final" else f"base_{level}.mp4")
    concat_segments(segments, base_path, work_dir)

    overlays = with_clip_durations(edl.get("overlays") or [], edit_dir)
    captions = None
    if args.style is not None:
        style = load_style(args.style)
        ass_path = work_dir / "captions.ass"
        build_ass(cuts, resolve_transcripts_dir(edit_dir, args.transcripts_dir), style,
                  edl.get("replacements") or {}, not args.no_refine_onsets,
                  caption_holes(overlays, total_s), ass_path)
        captions = (ass_path, style.fonts_dir)

    if args.no_loudnorm:
        build_final_composite(base_path, overlays, captions, edl, out_path, edit_dir, fps, total_s, size)
    else:
        prenorm = out_path.with_suffix(".prenorm.mov")
        build_final_composite(base_path, overlays, captions, edl, prenorm, edit_dir, fps, total_s, size)
        apply_loudnorm(prenorm, out_path, two_pass=not args.draft)
        prenorm.unlink(missing_ok=True)

    print(f"\ndone: {out_path} ({out_path.stat().st_size / 1048576:.1f} MB, {frames} frames, {total_s:.3f}s)")


if __name__ == "__main__":
    main()

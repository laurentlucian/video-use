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


def frame_filter(cut: Cut, size: tuple[int, int]) -> str:
    w, h = size
    crop_w = f"min(iw,ih*{w}/{h})/{cut.zoom}"
    crop_h = f"min(iw,ih*{w}/{h})/{cut.zoom}*{h}/{w}"
    ax, ay = cut.anchor
    return (
        f"crop=w='{crop_w}':h='{crop_h}'"
        f":x='max(0,min(iw-ow,iw*{ax}-ow/2))':y='max(0,min(ih-oh,ih*{ay}-oh/2))',"
        f"scale={w}:{h}:flags=lanczos,setsar=1,fps={ffmpeg_rate(cut.fps)}"
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


def overlay_input(path: Path, fps: Fraction, duration: float) -> list[str]:
    if path.suffix.lower() in STILL_SUFFIXES:
        return ["-loop", "1", "-framerate", ffmpeg_rate(fps), "-t", f"{duration:.6f}", "-i", str(path)]
    if path.suffix.lower() == ".webm":
        return ["-c:v", "libvpx-vp9", "-i", str(path)]
    return ["-i", str(path)]


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    captions: tuple[Path, Path] | None,
    out_path: Path,
    edit_dir: Path,
    fps: Fraction,
    total_s: float,
    size: tuple[int, int],
) -> None:
    if not overlays and captions is None:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(base_path), "-c", "copy", str(out_path)], check=True)
        return

    inputs = ["-i", str(base_path)]
    graph: list[str] = []
    current = "[0:v]"
    w, h = size
    for idx, ov in enumerate(overlays, start=1):
        start = float(ov["start_in_output"])
        duration = min(float(ov.get("duration", total_s - start)), total_s - start)
        if duration <= 0:
            sys.exit(f"overlay {idx - 1} starts at or after the end of the output ({total_s:.3f}s)")
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

    if captions is not None:
        ass_path, fonts_dir = captions
        graph.append(f"{current}subtitles=filename='{filter_path(ass_path)}':fontsdir='{filter_path(fonts_dir)}'[outv]")
    else:
        graph.append(f"{current}null[outv]")

    print(f"compositing → {out_path.name}  (overlays: {len(overlays)}, captions: {'yes' if captions else 'no'})")
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", *inputs,
        "-filter_complex", ";".join(graph),
        "-map", "[outv]", "-map", "0:a", "-frames:v", str(round(total_s * fps)),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", ffmpeg_rate(fps),
        "-c:a", "copy", "-movflags", "+faststart",
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
        "ffmpeg", "-y", "-v", "error", "-i", str(input_path), "-c:v", "copy",
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

    captions = None
    if args.style is not None:
        style = load_style(args.style)
        ass_path = work_dir / "captions.ass"
        build_ass(cuts, resolve_transcripts_dir(edit_dir, args.transcripts_dir), style,
                  edl.get("replacements") or {}, not args.no_refine_onsets, ass_path)
        captions = (ass_path, style.fonts_dir)

    overlays = edl.get("overlays") or []
    if args.no_loudnorm:
        build_final_composite(base_path, overlays, captions, out_path, edit_dir, fps, total_s, size)
    else:
        prenorm = out_path.with_suffix(".prenorm.mp4")
        build_final_composite(base_path, overlays, captions, prenorm, edit_dir, fps, total_s, size)
        apply_loudnorm(prenorm, out_path, two_pass=not args.draft)
        prenorm.unlink(missing_ok=True)

    print(f"\ndone: {out_path} ({out_path.stat().st_size / 1048576:.1f} MB, {frames} frames, {total_s:.3f}s)")


if __name__ == "__main__":
    main()

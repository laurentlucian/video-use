from __future__ import annotations

import math
import sys
from itertools import pairwise
from pathlib import Path

from edl import resolve_path

AUDIO_RATE = 48000
DUCK_THRESHOLD = 0.03
DUCK_RATIO = 20
STEREO = f"aresample={AUDIO_RATE},aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts=stereo"


def gain_curve(base_db: float, points: list[dict]) -> str:
    keys = [(0.0, base_db)] + sorted((float(p["at_s"]), float(p["gain_db"])) for p in points)
    expr = f"{keys[-1][1]:.3f}"
    for (t0, g0), (t1, g1) in reversed(list(pairwise(keys))):
        ramp = f"{g0:.3f}" if t1 <= t0 else f"{g0:.3f}+({g1 - g0:.3f})*(t-{t0:.6f})/{t1 - t0:.6f}"
        expr = f"if(lt(t,{t1:.6f}),{ramp},{expr})"
    return expr


def duck_mix(depth_db: float) -> float:
    return min(1.0, 1.0 - 10 ** (-abs(depth_db) / 20))


def music_chain(music: dict, total_s: float) -> str:
    start = float(music.get("start_s", 0.0))
    fade_in = float(music.get("fade_in_ms", 0)) / 1000
    fade_out = float(music.get("fade_out_ms", 0)) / 1000
    chain = [
        f"atrim=start={start:.6f}", "asetpts=PTS-STARTPTS", f"atrim=duration={total_s:.6f}", STEREO,
        f"volume='pow(10,({gain_curve(float(music.get('gain_db', 0.0)), music.get('automation') or [])})/20)':eval=frame",
    ]
    if fade_in > 0:
        chain.append(f"afade=t=in:st=0:d={fade_in:.6f}")
    if fade_out > 0:
        chain.append(f"afade=t=out:st={max(0.0, total_s - fade_out):.6f}:d={fade_out:.6f}")
    return ",".join(chain)


def pan_gains(pan: float) -> tuple[float, float]:
    angle = (max(-1.0, min(1.0, pan)) + 1) * math.pi / 4
    return math.cos(angle) * math.sqrt(2), math.sin(angle) * math.sqrt(2)


def sfx_chain(fx: dict) -> str:
    chain = [STEREO, f"volume={float(fx.get('gain_db', 0.0)):g}dB"]
    if fx.get("duration_s") is not None:
        chain.append(f"atrim=duration={float(fx['duration_s']):.6f}")
    fade = float(fx.get("fade_out_ms", 0)) / 1000
    if fade > 0:
        chain.append(f"areverse,afade=t=in:d={fade:.6f},areverse")
    if fx.get("pan"):
        left, right = pan_gains(float(fx["pan"]))
        chain.append(f"pan=stereo|c0={left:.4f}*c0|c1={right:.4f}*c1")
    return ",".join(chain)


def audio_mix(edl: dict, edit_dir: Path, first_input: int, total_s: float) -> tuple[list[str], list[str], str] | None:
    music = edl.get("music")
    sfx = edl.get("sfx") or []
    if not music and not sfx:
        return None

    inputs: list[str] = []
    graph = [f"[0:a]{STEREO}[voice]"]
    stems = ["[voice]"]
    index = first_input

    if music:
        inputs += ["-i", str(resolve_path(music["file"], edit_dir))]
        graph.append(f"[{index}:a]{music_chain(music, total_s)}[bed]")
        duck = music.get("duck")
        if duck:
            graph[0] = f"[0:a]{STEREO},asplit=2[voice][key]"
            graph.append(
                f"[bed][key]sidechaincompress=threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}"
                f":attack={float(duck.get('attack_ms', 20)):g}:release={float(duck.get('release_ms', 250)):g}"
                f":mix={duck_mix(float(duck.get('under_voice_db', 6))):.4f}[music]"
            )
            stems.append("[music]")
        else:
            stems.append("[bed]")
        index += 1

    for i, fx in enumerate(sfx):
        at = float(fx["at_s"])
        if not 0.0 <= at < total_s:
            sys.exit(f"sfx {i}: at_s {at:.3f} is outside the output (0..{total_s:.3f}s)")
        inputs += ["-i", str(resolve_path(fx["file"], edit_dir))]
        graph.append(f"[{index}:a]{sfx_chain(fx)},adelay=delays={round(at * AUDIO_RATE)}S:all=1[fx{i}]")
        stems.append(f"[fx{i}]")
        index += 1

    graph.append(
        f"{''.join(stems)}amix=inputs={len(stems)}:duration=first:normalize=0,"
        f"atrim=duration={total_s:.6f}[outa]"
    )
    return inputs, graph, "[outa]"

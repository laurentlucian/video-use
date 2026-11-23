from __future__ import annotations

import ctypes
import math
import os
import platform
import sys
from functools import lru_cache
from pathlib import Path

MODEL = os.environ.get("VIDEO_USE_WHISPER_MODEL", "large-v3-turbo")
DISFLUENCY_PROMPT = "Umm, uh, so, like, you know, I mean, hmm. Uh-huh. Yeah, um, okay, so uh."
HALLUCINATION_SILENCE_S = 2.0


def on_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def engine_name() -> str:
    lib = "mlx-whisper" if on_apple_silicon() else "faster-whisper"
    return f"local:{lib}:{MODEL}"


def preload_pip_cuda_libs() -> None:
    try:
        import nvidia.cublas.lib
        import nvidia.cudnn.lib
    except ImportError:
        return
    for pkg in (nvidia.cublas.lib, nvidia.cudnn.lib):
        for lib_dir in pkg.__path__:
            for so in sorted(Path(lib_dir).glob("lib*.so*")):
                try:
                    ctypes.CDLL(str(so), mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass


@lru_cache(maxsize=1)
def faster_whisper_model():
    preload_pip_cuda_libs()
    import ctranslate2
    from faster_whisper import WhisperModel

    if ctranslate2.get_cuda_device_count() > 0:
        return WhisperModel(MODEL, device="cuda", compute_type="float16")
    return WhisperModel(MODEL, device="cpu", compute_type="int8")


def words_mlx(audio: Path, language: str | None) -> tuple[list[dict], str | None]:
    import mlx_whisper

    result = mlx_whisper.transcribe(
        str(audio),
        path_or_hf_repo=f"mlx-community/whisper-{MODEL}",
        word_timestamps=True,
        initial_prompt=DISFLUENCY_PROMPT,
        hallucination_silence_threshold=HALLUCINATION_SILENCE_S,
        language=language,
    )
    words = [
        {"text": w["word"], "start": w["start"], "end": w["end"], "p": w.get("probability", 1.0)}
        for seg in result.get("segments", [])
        for w in seg.get("words", [])
    ]
    return words, result.get("language")


def words_faster_whisper(audio: Path, language: str | None) -> tuple[list[dict], str | None]:
    segments, info = faster_whisper_model().transcribe(
        str(audio),
        word_timestamps=True,
        initial_prompt=DISFLUENCY_PROMPT,
        hallucination_silence_threshold=HALLUCINATION_SILENCE_S,
        language=language,
    )
    words = [
        {"text": w.word, "start": w.start, "end": w.end, "p": w.probability}
        for seg in segments
        for w in (seg.words or [])
    ]
    return words, info.language


def scribe_shape(words: list[dict], language: str | None) -> dict:
    out: list[dict] = []
    texts: list[str] = []
    prev_end: float | None = None
    for w in words:
        text = w["text"].strip()
        if not text:
            continue
        start, end = round(w["start"], 3), round(w["end"], 3)
        if prev_end is not None and start > prev_end:
            out.append({"text": " ", "start": prev_end, "end": start,
                        "type": "spacing", "speaker_id": "speaker_0"})
        out.append({"text": text, "start": start, "end": end, "type": "word",
                    "speaker_id": "speaker_0", "logprob": round(math.log(max(w["p"], 1e-6)), 4)})
        texts.append(text)
        prev_end = end
    return {
        "language_code": language,
        "text": " ".join(texts),
        "words": out,
        "engine": engine_name(),
    }


def transcribe(audio: Path, language: str | None = None) -> dict:
    words, detected = (words_mlx if on_apple_silicon() else words_faster_whisper)(audio, language)
    return scribe_shape(words, language or detected)


if __name__ == "__main__":
    import json

    print(json.dumps(transcribe(Path(sys.argv[1])), indent=2))

"""Fast asynchronous playback helper for cached Japanese voice reactions.

Generated WAVs remain local. Clips and the audio backend are warmed in a background
thread so the turn-start hook does not pay import or file decoding latency.
"""
from __future__ import annotations

import random
import threading
import wave
from pathlib import Path

DEFAULT_REACTION_DIR = Path(__file__).resolve().parent / "reactions"
_warm_lock = threading.Lock()
_warm_started = False
_cached_sd = None
_cached_np = None
_cached_audio: dict[Path, tuple[object, int]] = {}


def list_reaction_clips(directory: Path = DEFAULT_REACTION_DIR) -> list[Path]:
    """Return sorted playable candidate WAV paths from the reaction cache."""
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.wav") if path.is_file() and path.stat().st_size > 44)


def choose_reaction_clip(directory: Path = DEFAULT_REACTION_DIR, rng: random.Random | None = None) -> Path | None:
    clips = list_reaction_clips(directory)
    if not clips:
        return None
    return (rng or random).choice(clips)


def _load_wav_float32(path: Path, numpy_module=None):
    np = numpy_module
    if np is None:
        import numpy as np
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if channels < 1 or width not in (1, 2, 4) or not frames:
        raise ValueError("Unsupported/empty reaction WAV.")
    if width == 1:
        audio = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    else:
        audio = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    if channels > 1:
        audio = audio.reshape((-1, channels))
    return audio, rate


def _warm_worker(directory: Path) -> None:
    global _cached_sd, _cached_np
    try:
        import numpy as np
        import sounddevice as sd
        _cached_np, _cached_sd = np, sd
        for path in list_reaction_clips(directory):
            try:
                _cached_audio[path] = _load_wav_float32(path, np)
            except Exception as exc:
                print(f"[Aurora reação] Cache inválido {path.name}: {exc}")
    except Exception as exc:
        print(f"[Aurora reação] Pré-aquecimento de áudio indisponível: {type(exc).__name__}: {exc}")


def warm_reaction_cache(directory: Path = DEFAULT_REACTION_DIR) -> bool:
    """Preload audio backend and clips asynchronously; safe to call multiple times."""
    global _warm_started
    with _warm_lock:
        if _warm_started:
            return True
        if not list_reaction_clips(directory):
            return False
        _warm_started = True
        threading.Thread(target=_warm_worker, args=(directory,), daemon=True, name="AuroraReactionWarmup").start()
    return True


def _play_clip(path: Path, directory: Path, on_finished=None) -> bool:
    sd = _cached_sd
    audio_info = _cached_audio.get(path)
    if sd is not None and audio_info is not None:
        audio, rate = audio_info
        sd.play(audio, samplerate=rate, blocking=False)
        sd.wait()
        return True
    # Cold cache: decode/import off the turn-start thread, then play.
    try:
        import numpy as np
        import sounddevice as sd_local
        audio, rate = _load_wav_float32(path, np)
        sd_local.play(audio, samplerate=rate, blocking=False)
        sd_local.wait()
        return True
    except Exception as exc:
        print(f"[Aurora reação] Não foi possível iniciar {path.name}: {type(exc).__name__}: {exc}")
        return False
    finally:
        if on_finished is not None:
            try:
                on_finished()
            except Exception:
                pass


def play_reaction_clip(directory: Path = DEFAULT_REACTION_DIR, rng: random.Random | None = None, on_finished=None) -> bool:
    """Schedule a cached clip immediately; sounddevice playback is nonblocking."""
    clip = choose_reaction_clip(directory, rng)
    if clip is None:
        return False
    # Keep the turn-start hook nonblocking even when sounddevice needs to
    # initialize an output stream; warm cache makes the worker path fast.
    threading.Thread(target=_play_clip, args=(clip, directory, on_finished), daemon=True, name="AuroraReactionPlayback").start()
    return True

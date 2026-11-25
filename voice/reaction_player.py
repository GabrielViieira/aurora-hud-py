"""Fast playback helper for cached Japanese voice reactions.

WAV assets are generated locally with the user's configured SAPI voice. Playback
uses sounddevice so reactions can start independently from the HUD's winsound TTS.
"""
from __future__ import annotations

import random
import wave
from pathlib import Path

DEFAULT_REACTION_DIR = Path(__file__).resolve().parent / "reactions"


def list_reaction_clips(directory: Path = DEFAULT_REACTION_DIR) -> list[Path]:
    """Return sorted, playable candidate WAV paths from the reaction cache."""
    if not directory.is_dir():
        return []
    return sorted(
        path for path in directory.glob("*.wav")
        if path.is_file() and path.stat().st_size > 44
    )


def choose_reaction_clip(
    directory: Path = DEFAULT_REACTION_DIR,
    rng: random.Random | None = None,
) -> Path | None:
    clips = list_reaction_clips(directory)
    if not clips:
        return None
    return (rng or random).choice(clips)


def _load_wav_float32(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if channels < 1 or sample_width not in (1, 2, 4) or not frames:
        raise ValueError("Unsupported or empty reaction WAV.")
    if sample_width == 1:
        audio = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    else:
        audio = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    if channels > 1:
        audio = audio.reshape((-1, channels))
    return audio, sample_rate


def play_reaction_clip(
    directory: Path = DEFAULT_REACTION_DIR,
    rng: random.Random | None = None,
) -> bool:
    """Start a cached WAV asynchronously on a separate audio stream."""
    clip = choose_reaction_clip(directory, rng)
    if clip is None:
        return False
    try:
        import sounddevice as sd
        audio, sample_rate = _load_wav_float32(clip)
        sd.play(audio, samplerate=sample_rate, blocking=False)
        return True
    except Exception as exc:
        print(f"[Aurora reação] Não foi possível iniciar {clip.name}: {type(exc).__name__}: {exc}")
        return False

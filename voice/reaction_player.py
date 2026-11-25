"""Fast playback helper for cached Japanese voice reactions.

WAV assets are intentionally generated locally with the user's configured SAPI
voice; they are not committed to the repository.
"""
from __future__ import annotations

import random
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


def play_reaction_clip(
    directory: Path = DEFAULT_REACTION_DIR,
    rng: random.Random | None = None,
) -> bool:
    """Start a cached WAV asynchronously on Windows; returns False if unavailable."""
    clip = choose_reaction_clip(directory, rng)
    if clip is None:
        return False
    try:
        import winsound
        winsound.PlaySound(str(clip), winsound.SND_FILENAME | winsound.SND_ASYNC)
        return True
    except (ImportError, RuntimeError, OSError) as exc:
        print(f"[Aurora reação] Não foi possível iniciar {clip.name}: {exc}")
        return False

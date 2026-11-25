"""Offline tests for the cached reaction player."""
from __future__ import annotations

import random
import sys
import tempfile
import types
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if "aurora" in sys.modules:
    _package = sys.modules["aurora"]
    _paths = getattr(_package, "__path__", None)
    if _paths is not None and str(_REPO_ROOT) not in _paths:
        _paths.insert(0, str(_REPO_ROOT))
else:
    _package = types.ModuleType("aurora")
    _package.__path__ = [str(_REPO_ROOT)]
    sys.modules["aurora"] = _package
sys.path.insert(0, str(_REPO_ROOT.parent))

from aurora.voice.reaction_player import (
    choose_reaction_clip,
    list_reaction_clips,
    play_reaction_clip,
)


class ReactionPlayerTests(unittest.TestCase):
    def test_empty_cache_is_safe_and_non_playing(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertEqual(list_reaction_clips(directory), [])
            self.assertIsNone(choose_reaction_clip(directory))
            self.assertFalse(play_reaction_clip(directory))

    def test_lists_only_nonempty_wav_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "b.wav").write_bytes(b"RIFF" + b"x" * 50)
            (directory / "a.wav").write_bytes(b"RIFF")
            (directory / "notes.txt").write_text("not audio", encoding="utf-8")
            self.assertEqual([p.name for p in list_reaction_clips(directory)], ["b.wav"])

    def test_random_choice_is_deterministic_with_rng(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "one.wav").write_bytes(b"x" * 60)
            (directory / "two.wav").write_bytes(b"x" * 60)
            selected = choose_reaction_clip(directory, random.Random(1))
            self.assertIn(selected.name, {"one.wav", "two.wav"})

    def test_play_uses_async_winsound(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            clip = directory / "only.wav"
            clip.write_bytes(b"x" * 60)
            fake = types.SimpleNamespace(SND_FILENAME=1, SND_ASYNC=2, calls=[])
            fake.PlaySound = lambda path, flags: fake.calls.append((path, flags))
            original = sys.modules.get("winsound")
            sys.modules["winsound"] = fake
            try:
                self.assertTrue(play_reaction_clip(directory))
                self.assertEqual(fake.calls, [(str(clip), 3)])
            finally:
                if original is None:
                    del sys.modules["winsound"]
                else:
                    sys.modules["winsound"] = original


if __name__ == "__main__":
    unittest.main()

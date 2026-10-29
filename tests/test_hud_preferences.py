"""Testes das preferências locais da HUD."""
from __future__ import annotations

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

from aurora.ui.hud_preferences import (
    load_hud_preferences,
    normalize_hud_preferences,
    save_hud_preferences,
)


class HudPreferencesTests(unittest.TestCase):
    def test_defaults_can_follow_environment_streaming_setting(self):
        self.assertFalse(load_hud_preferences(Path("/path/that/does/not/exist"), streaming_default=False)["streaming_enabled"])

    def test_values_are_clamped_to_safe_ranges(self):
        preferences = normalize_hud_preferences({
            "speech_volume_percent": 999,
            "speech_rate": 1,
            "response_panel_threshold": -5,
        })
        self.assertEqual(preferences["speech_volume_percent"], 125)
        self.assertEqual(preferences["speech_rate"], 100)
        self.assertEqual(preferences["response_panel_threshold"], 160)

    def test_preferences_round_trip_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "hud_preferences.json"
            saved = save_hud_preferences(path, {
                "speech_enabled": False,
                "streaming_enabled": True,
                "long_responses_in_panel": False,
                "speech_volume_percent": 80,
                "speech_rate": 175,
                "response_panel_threshold": 300,
                "conversation_timeout_seconds": 90,
            })
            loaded = load_hud_preferences(path)
        self.assertEqual(loaded, saved)
        self.assertFalse(loaded["speech_enabled"])
        self.assertFalse(loaded["long_responses_in_panel"])
        self.assertEqual(loaded["conversation_timeout_seconds"], 90)
        self.assertNotIn("api_key", loaded)

    def test_corrupt_file_uses_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prefs.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertTrue(load_hud_preferences(path)["speech_enabled"])


if __name__ == "__main__":
    unittest.main()

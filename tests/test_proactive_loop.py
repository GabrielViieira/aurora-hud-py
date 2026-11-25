"""Tests for cheap proactive observers and event gates."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aurora.core.observers import CheapObservers
from aurora.core.proactive_loop import ProactiveLoop


class ObserverTests(unittest.TestCase):
    def test_game_time_only_accumulates_when_title_matches_game_heuristic(self):
        title = ["Unturned"]
        observer = CheapObservers(downloads_dir=Path(tempfile.mkdtemp()), window_reader=lambda: title[0])
        snap = observer.snapshot(now=observer._last_tick + 10)
        self.assertTrue(snap.is_game)
        self.assertGreaterEqual(snap.game_seconds, 9.9)
        title[0] = "Notepad"
        snap = observer.snapshot(now=observer._last_tick + 20)
        self.assertFalse(snap.is_game)
        self.assertAlmostEqual(snap.game_seconds, 10, delta=0.1)

    def test_game_milestone_emitted_once(self):
        loop = ProactiveLoop(provider=None, model_for_chat=lambda: "unused", speak_callback=lambda *_: None, busy_callback=lambda: False, game_milestone_seconds=60)
        snap = type("Snapshot", (), {"is_game": True, "game_seconds": 70, "download_completed": ""})()
        self.assertEqual(loop._choose_event(snap), "game_milestone")
        self.assertEqual(loop._choose_event(snap), "")

    def test_quiet_hours_wrap_midnight(self):
        loop = ProactiveLoop(provider=None, model_for_chat=lambda: "unused", speak_callback=lambda *_: None, busy_callback=lambda: False, quiet_start_hour=22, quiet_end_hour=7)
        from datetime import datetime
        self.assertTrue(loop._quiet_hours(datetime(2026, 1, 1, 23)))
        self.assertFalse(loop._quiet_hours(datetime(2026, 1, 1, 12)))

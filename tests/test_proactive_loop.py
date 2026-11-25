"""Tests for cheap proactive observers and event gates."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys
import types

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

    def test_stable_download_file_emits_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp)
            target = downloads / "sample.bin"
            target.write_bytes(b"x" * 2048)
            observer = CheapObservers(downloads_dir=downloads, window_reader=lambda: "")
            start = observer._last_tick
            observer.snapshot(now=start + 10)
            observer.snapshot(now=start + 20)
            snapshot = observer.snapshot(now=start + 30)
            self.assertEqual(snapshot.download_completed, "sample.bin")

    def test_game_milestone_emitted_once(self):
        loop = ProactiveLoop(provider=None, model_for_chat=lambda: "unused", speak_callback=lambda *_: None, busy_callback=lambda: False, game_milestone_seconds=60)
        snap = type("Snapshot", (), {"is_game": True, "game_seconds": 70, "download_completed": ""})()
        self.assertEqual(loop._choose_event(snap), "game_milestone")
        self.assertEqual(loop._choose_event(snap), "")

    def test_pending_events_survive_busy_cooldown_gates(self):
        loop = ProactiveLoop(provider=None, model_for_chat=lambda: "unused", speak_callback=lambda *_: None, busy_callback=lambda: True)
        snap = type("Snapshot", (), {"is_game": True, "game_seconds": 10800, "download_completed": "download.zip"})()
        loop._pending_download_notice = snap.download_completed
        loop._pending_game_milestone = True
        self.assertEqual(loop._choose_event(snap), "download_completed")
        self.assertTrue(loop._pending_game_milestone)
        self.assertEqual(loop._choose_event(snap), "game_milestone")

    def test_proactive_request_is_chat_only_and_tool_free(self):
        class FakeProvider:
            def __init__(self):
                self.request = None
            def chat(self, request):
                self.request = request
                return type("Reply", (), {"text": "[PT]Tudo certo por aí? [JA]大丈夫ですか？"})()
        provider = FakeProvider()
        delivered = []
        loop = ProactiveLoop(
            provider=provider,
            model_for_chat=lambda: "chat-role-model",
            speak_callback=lambda pt, ja: delivered.append((pt, ja)),
            busy_callback=lambda: False,
        )
        snapshot = type("Snapshot", (), {
            "active_window": "Unturned",
            "is_game": True,
            "game_seconds": 10800,
            "local_time": "18:30",
            "download_completed": "",
        })()
        loop._generate_and_speak("game_milestone", snapshot)
        self.assertEqual(provider.request.model, "chat-role-model")
        self.assertEqual(provider.request.tools, [])
        self.assertEqual(delivered, [("Tudo certo por aí?", "大丈夫ですか？")])

    def test_quiet_hours_wrap_midnight(self):
        loop = ProactiveLoop(provider=None, model_for_chat=lambda: "unused", speak_callback=lambda *_: None, busy_callback=lambda: False, quiet_start_hour=22, quiet_end_hour=7)
        from datetime import datetime
        self.assertTrue(loop._quiet_hours(datetime(2026, 1, 1, 23)))
        self.assertFalse(loop._quiet_hours(datetime(2026, 1, 1, 12)))

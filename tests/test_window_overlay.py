"""Testes da ordem Z do overlay sem depender da API do Windows."""
from __future__ import annotations

import ctypes
import sys
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

from aurora.ui.window_overlay import (
    HWND_TOPMOST,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_SHOWWINDOW,
    TOPMOST_FLAGS,
    set_window_topmost,
)


class _FakeSetWindowPos:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class _FakeUser32:
    def __init__(self, result=True):
        self.SetWindowPos = _FakeSetWindowPos(result)


class WindowOverlayTests(unittest.TestCase):
    def test_sets_topmost_without_moving_resizing_or_activating(self):
        api = _FakeUser32()
        self.assertTrue(set_window_topmost(1234, user32=api))
        args = api.SetWindowPos.calls[0]
        self.assertEqual(args[0].value, 1234)
        self.assertEqual(args[1].value, ctypes.c_void_p(HWND_TOPMOST).value)
        self.assertEqual(args[2:6], (0, 0, 0, 0))
        self.assertEqual(args[6], TOPMOST_FLAGS)
        self.assertTrue(TOPMOST_FLAGS & SWP_NOACTIVATE)
        self.assertTrue(TOPMOST_FLAGS & SWP_NOMOVE)
        self.assertTrue(TOPMOST_FLAGS & SWP_NOSIZE)
        self.assertTrue(TOPMOST_FLAGS & SWP_SHOWWINDOW)

    def test_zero_handle_is_rejected(self):
        api = _FakeUser32()
        self.assertFalse(set_window_topmost(0, user32=api))
        self.assertEqual(api.SetWindowPos.calls, [])

    def test_api_failure_is_reported_as_false(self):
        self.assertFalse(set_window_topmost(1234, user32=_FakeUser32(result=0)))


if __name__ == "__main__":
    unittest.main()

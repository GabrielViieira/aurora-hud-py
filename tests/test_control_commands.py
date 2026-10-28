"""Testes dos comandos locais de controle da HUD."""
from __future__ import annotations

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

from aurora.core.control_commands import classify_hud_control_command


class ControlCommandTests(unittest.TestCase):
    def test_voice_stop_command_accepts_short_portuguese_imperative(self):
        self.assertEqual(classify_hud_control_command("Aurora, para"), "cancel")
        self.assertEqual(classify_hud_control_command("pare de falar agora"), "cancel")
        self.assertEqual(classify_hud_control_command("stop talking"), "cancel")

    def test_normal_conversation_is_not_mistaken_for_cancel(self):
        self.assertEqual(classify_hud_control_command("para jogar, abra o Steam"), "")

    def test_exit_command_is_local(self):
        self.assertEqual(classify_hud_control_command("feche a Aurora"), "exit")


if __name__ == "__main__":
    unittest.main()

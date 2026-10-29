"""Testes offline da sessão contínua de voz da Aurora."""
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

from aurora.ui.voice_conversation import (
    VoiceConversationSession,
    is_conversation_end_command,
    normalize_voice_command,
)


class VoiceCommandTests(unittest.TestCase):
    def test_end_command_accepts_wakeword_and_accent_variants(self):
        self.assertTrue(is_conversation_end_command("Aurora, pode descansar"))
        self.assertTrue(is_conversation_end_command("pode descansar agora"))
        self.assertFalse(is_conversation_end_command("encerra a conversação", "aurora"))
        self.assertTrue(is_conversation_end_command("volta para o modo de espera"))
        self.assertTrue(is_conversation_end_command("Aurora, encerra a sessão"))

    def test_end_command_does_not_match_ordinary_conversation(self):
        self.assertFalse(is_conversation_end_command("pode descansar que eu continuo jogando"))
        self.assertFalse(is_conversation_end_command("quero conversar sobre dormir"))
        self.assertFalse(is_conversation_end_command("para de falar sobre a conversa"))

    def test_normalization_removes_accents_and_punctuation(self):
        self.assertEqual(normalize_voice_command("Aurora, encerra a sessão!"), "aurora encerra a sessao")


class VoiceConversationSessionTests(unittest.TestCase):
    def test_activation_refresh_and_idle_expiry(self):
        session = VoiceConversationSession(30)
        self.assertFalse(session.is_active)
        session.activate(100.0)
        self.assertTrue(session.is_active)
        self.assertEqual(session.seconds_remaining(110.0), 20)
        session.refresh(120.0)
        self.assertEqual(session.seconds_remaining(130.0), 20)
        self.assertFalse(session.expire_if_needed(149.0))
        self.assertTrue(session.expire_if_needed(150.0))
        self.assertFalse(session.is_active)

    def test_timeout_is_clamped_and_can_change_during_session(self):
        session = VoiceConversationSession(9999)
        self.assertEqual(session.timeout_seconds, 600)
        session.activate(5.0)
        session.set_timeout(15)
        self.assertEqual(session.seconds_remaining(5.0), 15)
        session.set_timeout(1)
        self.assertEqual(session.timeout_seconds, 15)
        self.assertEqual(session.seconds_remaining(5.0), 15)

    def test_end_is_idempotent(self):
        session = VoiceConversationSession(60)
        self.assertFalse(session.end())
        session.activate(1.0)
        self.assertTrue(session.end())
        self.assertFalse(session.end())


if __name__ == "__main__":
    unittest.main()

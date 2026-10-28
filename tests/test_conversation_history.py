"""Testes da persistência e limpeza do histórico local."""
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

from aurora.memory.conversation_store import ConversationStore
from aurora.storage.database import Database


class ConversationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "aurora-test.sqlite")
        self.db.migrate()
        self.store = ConversationStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def _session_with_messages(self, title: str = "Teste") -> str:
        session_id = self.store.create_session(title)
        self.store.add_message(session_id, "user", "Oi, Aurora")
        self.store.add_message(session_id, "assistant", "Olá!")
        return session_id

    def test_messages_are_returned_in_chronological_order(self):
        session_id = self._session_with_messages()
        messages = self.store.messages(session_id)
        self.assertEqual([message.role for message in messages], ["user", "assistant"])
        self.assertEqual([message.content for message in messages], ["Oi, Aurora", "Olá!"])

    def test_delete_session_cascades_to_messages(self):
        session_id = self._session_with_messages()
        self.store.delete_session(session_id)
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.messages(session_id), [])

    def test_clear_history_keeps_sessions_table_empty(self):
        self._session_with_messages()
        self._session_with_messages("Outro")
        self.store.clear_sessions()
        self.assertEqual(self.store.sessions(), [])


if __name__ == "__main__":
    unittest.main()

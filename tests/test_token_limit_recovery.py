"""Testes para recuperação de respostas recusadas por limite de tokens."""
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

from aurora.core.agent import (
    _compact_messages_for_token_retry,
    _is_token_limit_error,
)
from aurora.llm.freellm_client import FreeLLMClient
from aurora.llm.provider import ChatMessage, ErrorKind, ProviderError, ToolCall


class TokenLimitRecoveryTests(unittest.TestCase):
    def test_detects_token_limit_error(self):
        error = ProviderError(
            "HTTP 500: limit token atingido",
            kind=ErrorKind.UNAVAILABLE,
            status=500,
            body='{"error":{"message":"limit token atingido"}}',
        )
        self.assertTrue(_is_token_limit_error(error))

    def test_http_500_token_limit_does_not_get_generic_unavailable_kind(self):
        client = FreeLLMClient("http://127.0.0.1:3001/v1", "test-key")
        error = client._http_error(
            500, b'{"error":{"message":"limit token atingido"}}'
        )
        self.assertEqual(error.kind, ErrorKind.BAD_REQUEST)
        self.assertFalse(error.retryable)

    def test_compaction_keeps_current_goal_and_latest_tool_cycle(self):
        messages = [
            ChatMessage(role="system", content="Instruções essenciais da Aurora."),
            ChatMessage(role="user", content="conversa antiga que pode ser removida"),
            ChatMessage(role="assistant", content="resposta antiga"),
            ChatMessage(role="user", content="Clique no campo de pesquisa e pesquise o jogo."),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="old-call", name="capturar_tela", arguments={"monitor": 1})],
            ),
            ChatMessage(role="tool", name="capturar_tela", tool_call_id="old-call",
                        content="OLD_DETAIL " * 1500),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="latest-call", name="controlar_mouse",
                                     arguments={"acao": "clicar", "x": 200, "y": 300})],
            ),
            ChatMessage(role="tool", name="controlar_mouse", tool_call_id="latest-call",
                        content="LATEST_RESULT " * 1500),
        ]

        compacted = _compact_messages_for_token_retry(
            messages,
            "Clique no campo de pesquisa e pesquise o jogo.",
            [],
            budget=700,
        )
        serialized = "\\n".join(
            str(message.content) for message in compacted
        )

        self.assertTrue(any(
            message.role == "system" and "Instruções essenciais" in str(message.content)
            for message in compacted
        ))
        self.assertTrue(any(
            message.role == "user"
            and message.content == "Clique no campo de pesquisa e pesquise o jogo."
            for message in compacted
        ))
        self.assertIn("LATEST_RESULT", serialized)
        self.assertNotIn("OLD_DETAIL", serialized)


if __name__ == "__main__":
    unittest.main()

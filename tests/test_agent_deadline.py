"""Testes para garantir que o agente não espere indefinidamente por um provedor."""
from __future__ import annotations

import sys
import threading
import time
import types
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
# O aplicativo é normalmente executado como pacote "aurora" a partir da pasta
# pai. Este alias permite executar os testes também quando o checkout tem outro nome.
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
    AuroraAgent,
    TaskDeadlineExceeded,
    _local_app_intent,
    _should_reuse_recent_capture,
    _should_use_accessibility_only_verification,
    _unknown_tool_names,
)
from aurora.llm.provider import LLMRequest, LLMResponse, ToolCall


class SlowProvider:
    def chat(self, request: LLMRequest) -> LLMResponse:
        del request
        time.sleep(0.25)
        return LLMResponse(text="resposta atrasada")


class AgentDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = object.__new__(AuroraAgent)
        self.agent.provider = SlowProvider()
        self.agent.cancel_event = threading.Event()

    def test_model_wait_raises_when_deadline_expires(self):
        request = LLMRequest(messages=[])
        deadline = time.monotonic() + 0.03
        with self.assertRaises(TaskDeadlineExceeded):
            self.agent._chat_with_cancel(request, deadline=deadline)

    def test_cancel_still_interrupts_model_wait(self):
        request = LLMRequest(messages=[])
        cancel_event = self.agent.cancel_event

        def cancel_soon():
            time.sleep(0.03)
            cancel_event.set()

        threading.Thread(target=cancel_soon, daemon=True).start()
        self.assertIsNone(
            self.agent._chat_with_cancel(request, deadline=time.monotonic() + 1.0)
        )

    def test_typing_uses_accessibility_only_verification(self):
        self.assertTrue(
            _should_use_accessibility_only_verification(
                "controlar_teclado", {"acao": "digitar", "texto": "Hollow Knight"}
            )
        )

    def test_named_accessible_click_uses_accessibility_only_verification(self):
        self.assertTrue(
            _should_use_accessibility_only_verification(
                "controlar_mouse", {"acao": "clicar", "controle": "Search"}
            )
        )

    def test_coordinate_click_uses_accessibility_only_verification(self):
        # Mudança de política (2026-10-26): clique por coordenada também tenta UIA
        # primeiro. Se a árvore não tiver controles úteis, a própria ferramenta
        # captura a imagem como fallback, então a verificação continua existindo —
        # só deixa de custar uma inferência de visão (~13s) quando é dispensável.
        self.assertTrue(
            _should_use_accessibility_only_verification(
                "controlar_mouse", {"acao": "clicar", "x": 420, "y": 250}
            )
        )

    def test_enter_uses_accessibility_only_verification(self):
        self.assertTrue(
            _should_use_accessibility_only_verification(
                "controlar_teclado", {"acao": "pressionar", "tecla": "enter"}
            )
        )

    def test_explicit_steam_search_uses_direct_store_url(self):
        intent = _local_app_intent("Aurora, pesquise o jogo Hollow Knight na Steam")
        self.assertIsNotNone(intent)
        self.assertEqual(intent[0], "abrir_url")
        self.assertEqual(
            intent[1]["url"],
            "https://store.steampowered.com/search/?term=hollow+knight",
        )

    def test_recent_capture_is_reused_when_requested_evidence_exists(self):
        self.assertTrue(
            _should_reuse_recent_capture("capturar_tela", {}, True, True)
        )
        self.assertFalse(
            _should_reuse_recent_capture(
                "capturar_tela", {"somente_imagem": True}, True, False
            )
        )
        self.assertTrue(
            _should_reuse_recent_capture(
                "capturar_tela", {"somente_acessibilidade": True}, False, True
            )
        )
        # Qualquer observação recente torna uma captura genérica redundante;
        # para buscar a modalidade ausente, o modelo precisa pedi-la explicitamente.
        self.assertTrue(
            _should_reuse_recent_capture("capturar_tela", {}, False, True)
        )
        self.assertTrue(
            _should_reuse_recent_capture("capturar_tela", {}, True, False)
        )
        self.assertFalse(
            _should_reuse_recent_capture(
                "capturar_tela", {"somente_imagem": True}, False, True
            )
        )
        self.assertFalse(
            _should_reuse_recent_capture("controlar_mouse", {}, True, True)
        )

    def test_unknown_tool_typo_is_detected_against_registered_names(self):
        calls = [
            ToolCall(
                id="call-1",
                name="pesificar",
                arguments={"texto": "Hollow Knight"},
            )
        ]

        self.assertEqual(
            _unknown_tool_names(calls, {"pesquisar", "controlar_teclado"}),
            ["pesificar"],
        )

    def test_exact_registered_tool_name_is_not_flagged_as_unknown(self):
        calls = [
            ToolCall(
                id="call-2",
                name="pesquisar",
                arguments={"texto": "Hollow Knight"},
            )
        ]

        self.assertEqual(
            _unknown_tool_names(calls, {"pesquisar", "controlar_teclado"}),
            [],
        )

    def test_non_steam_search_is_not_routed_without_llm(self):
        self.assertIsNone(_local_app_intent("pesquise Hollow Knight"))


if __name__ == "__main__":
    unittest.main()

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

from aurora.core.agent import AuroraAgent, TaskDeadlineExceeded
from aurora.llm.provider import LLMRequest, LLMResponse


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


if __name__ == "__main__":
    unittest.main()

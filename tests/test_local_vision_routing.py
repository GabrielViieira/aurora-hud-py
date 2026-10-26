"""Testes da rota de visão local independente do provedor de conversa."""
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

from aurora.config.settings import Settings
from aurora.core.agent import AuroraAgent
from aurora.llm.provider import ChatMessage, LLMRequest


class LocalVisionRoutingTests(unittest.TestCase):
    def test_settings_read_local_vision_endpoint(self):
        settings = Settings.load(
            env={
                "AURORA_LLM_VISION_BASE_URL": "http://127.0.0.1:11434/v1/",
                "AURORA_LLM_VISION_MODEL": "qwen2.5vl:3b",
                "AURORA_LLM_VISION_TIMEOUT_S": "120",
                "AURORA_LLM_VISION_MAX_RETRIES": "0",
            },
            dotenv_path=Path("/this-file-does-not-exist.env"),
            app_dir=_REPO_ROOT,
        )

        self.assertEqual(settings.llm.vision_base_url, "http://127.0.0.1:11434/v1")
        self.assertEqual(settings.llm.vision_model, "qwen2.5vl:3b")
        self.assertEqual(settings.llm.vision_timeout_s, 120.0)
        self.assertEqual(settings.llm.vision_max_retries, 0)

    def _agent(self, *, vision_provider=None):
        agent = object.__new__(AuroraAgent)
        agent.provider = object()
        if vision_provider is not None:
            agent.vision_provider = vision_provider
        return agent

    def test_image_request_uses_local_vision_provider(self):
        local = object()
        agent = self._agent(vision_provider=local)
        request = LLMRequest(
            model="qwen2.5vl:3b",
            messages=[
                ChatMessage(
                    role="user",
                    content=[
                        {"type": "text", "text": "Descreva esta tela."},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        },
                    ],
                )
            ],
        )

        self.assertIs(agent._provider_for_request(request), local)

    def test_text_only_request_stays_on_main_provider(self):
        agent = self._agent(vision_provider=object())
        request = LLMRequest(
            model="auto",
            messages=[ChatMessage(role="user", content="Olá, Aurora.")],
        )

        self.assertIs(agent._provider_for_request(request), agent.provider)

    def test_image_request_falls_back_when_local_vision_is_not_configured(self):
        agent = self._agent()
        request = LLMRequest(
            model="auto",
            messages=[
                ChatMessage(
                    role="user",
                    content=[{"type": "image", "data": "AAAA"}],
                )
            ],
        )

        self.assertIs(agent._provider_for_request(request), agent.provider)


if __name__ == "__main__":
    unittest.main()

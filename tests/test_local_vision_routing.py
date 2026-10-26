"""Testes da rota de visão local independente do provedor de conversa."""
from __future__ import annotations

import base64
import json
import sys
import threading
import time
import types
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

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
from aurora.core.agent import AuroraAgent, _is_vision_token_limit_error
from aurora.llm.provider import ChatMessage, LLMRequest, LLMResponse, ProviderError


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

    def test_local_vision_request_omits_tools_and_keeps_image(self):
        local = object()
        agent = self._agent(vision_provider=local)
        tools = [{"type": "function", "function": {"name": "capturar_tela"}}]
        image_message = ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "Captura visual da tela. Analise a imagem."},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        )
        request = LLMRequest(
            model="qwen2.5vl:3b",
            tools=tools,
            messages=[
                ChatMessage(role="system", content="Instruções longas " * 3000),
                ChatMessage(role="user", content="Uma solicitação antiga."),
                ChatMessage(role="assistant", content="Resposta antiga."),
                ChatMessage(role="user", content="Descreva a janela que está aberta."),
                ChatMessage(role="tool", content="Resultado de ferramenta muito longo." * 50),
                image_message,
            ],
        )

        prepared = agent._prepare_provider_request(request, local)

        self.assertIsNot(prepared, request)
        self.assertEqual(prepared.tools, [])
        self.assertEqual(prepared.model, request.model)
        self.assertEqual(len(prepared.messages), 1)
        self.assertEqual(prepared.max_tokens, 300)
        parts = prepared.messages[0].content
        self.assertIsInstance(parts, list)
        self.assertIn("Descreva a janela que está aberta.", parts[0]["text"])
        self.assertIn("até 600 caracteres", parts[0]["text"])
        self.assertEqual(parts[1]["type"], "image_url")
        # A solicitação original não é modificada para o provedor principal.
        self.assertEqual(request.tools, tools)
        self.assertEqual(len(request.messages), 6)

    def test_local_vision_request_resizes_large_data_url_image(self):
        local = object()
        agent = self._agent(vision_provider=local)
        buffer = BytesIO()
        Image.new("RGB", (960, 540), (12, 34, 56)).save(buffer, format="PNG")
        data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        request = LLMRequest(
            model="qwen2.5vl:3b",
            messages=[
                ChatMessage(
                    role="user",
                    content=[
                        {"type": "text", "text": "Identifique a janela."},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                )
            ],
        )

        prepared = agent._prepare_provider_request(request, local)
        resized_url = prepared.messages[0].content[1]["image_url"]["url"]
        resized_bytes = base64.b64decode(resized_url.split(",", 1)[1])
        with Image.open(BytesIO(resized_bytes)) as resized:
            self.assertEqual(resized.size, (640, 360))
            self.assertEqual(resized.format, "JPEG")

        # O payload da conversa principal continua intacto.
        self.assertEqual(request.messages[0].content[1]["image_url"]["url"], data_url)

    def test_local_vision_retries_with_smaller_image_after_token_limit(self):
        buffer = BytesIO()
        Image.new("RGB", (960, 540), (12, 34, 56)).save(buffer, format="PNG")
        data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

        class TokenLimitedFirstCallsProvider:
            def __init__(self):
                self.requests = []

            def chat(self, request):
                self.requests.append(request)
                if len(self.requests) < 3:
                    raise ProviderError(
                        "HTTP 500: limit token atingido",
                        kind="bad_request",
                        status=500,
                        body='{"error":"limit token atingido"}',
                    )
                return LLMResponse(text="Janela do aplicativo identificada.")

        local = TokenLimitedFirstCallsProvider()
        agent = self._agent(vision_provider=local)
        agent.cancel_event = threading.Event()
        request = LLMRequest(
            model="qwen2.5vl:3b",
            messages=[
                ChatMessage(
                    role="user",
                    content=[
                        {"type": "text", "text": "Identifique a janela."},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                )
            ],
        )

        response = agent._chat_with_cancel(request, deadline=time.monotonic() + 10)

        self.assertEqual(response.text, "Janela do aplicativo identificada.")
        self.assertEqual(len(local.requests), 3)
        sizes = []
        for submitted in local.requests:
            url = submitted.messages[0].content[1]["image_url"]["url"]
            raw = base64.b64decode(url.split(",", 1)[1])
            with Image.open(BytesIO(raw)) as img:
                sizes.append(img.size)
        self.assertEqual(sizes, [(640, 360), (448, 252), (320, 180)])

    def test_local_vision_failure_falls_back_to_accessibility_data(self):
        agent = self._agent(vision_provider=object())
        accessible_result = {
            "ok": True,
            "data": {
                "interface_acessivel": {
                    "disponivel": True,
                    "elementos": [{"tipo": "Edit", "nome": "Search", "centro_x": 420, "centro_y": 90}],
                }
            },
        }
        messages = [
            ChatMessage(role="tool", content=json.dumps(accessible_result), name="capturar_tela"),
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "Captura visual após a ação."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            ),
        ]
        error = ProviderError(
            "HTTP 500: limite do modelo visual",
            kind="bad_request",
            status=500,
            body='{"error":"limite do modelo visual"}',
        )

        recovered = agent._handoff_local_vision_failure_to_accessibility(messages, error)

        self.assertTrue(recovered)
        self.assertTrue(all(not (isinstance(message.content, list)) for message in messages))
        self.assertIn("controles estruturados", messages[-1].content)
        self.assertIn("não finja ter visto", messages[-1].content)
        self.assertIn("Search", messages[0].content)

    def test_local_vision_token_limit_error_is_detected(self):
        error = ProviderError(
            "HTTP 500: limit token atingido",
            kind="bad_request",
            status=500,
            body='{"error":"limit token atingido"}',
        )
        self.assertTrue(_is_vision_token_limit_error(error))

    def test_local_vision_non_token_error_is_not_retried_as_context_limit(self):
        error = ProviderError(
            "HTTP 500: server internal error",
            kind="unavailable",
            status=500,
            body='{"error":"server internal error"}',
        )
        self.assertFalse(_is_vision_token_limit_error(error))

    def test_local_vision_observation_removes_images_for_main_provider(self):
        agent = self._agent(vision_provider=object())
        messages = [
            ChatMessage(role="user", content="Abra o Steam e veja a loja."),
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "Captura visual da área de trabalho."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            ),
        ]

        handed_off = agent._handoff_local_vision_observation(
            messages, "Steam está aberto; a janela principal mostra a Loja e a barra lateral."
        )

        self.assertTrue(handed_off)
        self.assertTrue(all(not (isinstance(m.content, list)) for m in messages))
        self.assertIn("Steam está aberto", messages[1].content)
        self.assertIn("Captura visual", messages[1].content)

    def test_main_provider_keeps_tools_for_image_requests(self):
        local = object()
        agent = self._agent(vision_provider=local)
        tools = [{"type": "function", "function": {"name": "capturar_tela"}}]
        request = LLMRequest(
            tools=tools,
            messages=[ChatMessage(role="user", content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}])],
        )

        prepared = agent._prepare_provider_request(request, agent.provider)

        self.assertIs(prepared, request)
        self.assertEqual(prepared.tools, tools)

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

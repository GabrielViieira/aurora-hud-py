"""Testes da recarga dinâmica da chave do FreeLLM e da visão local offline."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

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

from aurora.config.settings import ConfigError, Settings
from aurora.core.agent import (
    AuroraAgent,
    _is_vision_backend_unreachable,
    _messages_contain_image,
)
from aurora.llm.freellm_client import FreeLLMClient, _HTTPResult, _parse_dots_function_calls
from aurora.llm.provider import ChatMessage, ErrorKind, LLMRequest, ProviderError


class _FakeGateway:
    """Gateway OpenAI-compatible mínimo: 200 só para chaves válidas, senão 401."""

    def __init__(self, valid_keys: set[str]) -> None:
        self.valid_keys = set(valid_keys)
        self.auth_headers: list[str] = []
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # silencia o log do servidor
                return

            def do_GET(self) -> None:
                header = self.headers.get("Authorization", "")
                gateway.auth_headers.append(header)
                token = header[len("Bearer ") :] if header.startswith("Bearer ") else ""
                if token in gateway.valid_keys:
                    status = 200
                    body = json.dumps(
                        {"object": "list", "data": [{"id": "auto"}]}
                    ).encode("utf-8")
                else:
                    status = 401
                    body = json.dumps(
                        {"error": {"message": "invalid api key"}}
                    ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class DotsFunctionCallParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FreeLLMClient("http://127.0.0.1:9999/v1", "test-key", max_retries=0)
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": "controlar_teclado",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "acao": {"type": "string", "enum": ["digitar", "pressionar", "atalho"]},
                            "tecla": {"type": "string"},
                        },
                        "required": ["acao"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "controlar_mouse",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "acao": {"type": "string"},
                            "x": {"type": "integer"},
                        },
                        "required": ["acao", "x"],
                    },
                },
            },
        ]

    def _chat_from_text(self, content: str):
        payload = {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "model": "test-model",
        }
        with mock.patch.object(
            self.client,
            "_request",
            return_value=_HTTPResult(200, json.dumps(payload).encode("utf-8"), 5),
        ):
            return self.client.chat(LLMRequest(
                messages=[ChatMessage(role="user", content="Pressione Enter.")],
                tools=self.tools,
            ))

    def test_dots_xml_becomes_native_keyboard_tool_call(self):
        markup = (
            "<dots_function_call>\n"
            '<invoke name="controlar_teclado">\n'
            '<parameter name="acao">\npressionar\n</parameter>\n'
            '<parameter name="tecla">\nenter\n</parameter>\n'
            "</invoke>\n</dots_function_call>"
        )
        response = self._chat_from_text(markup)

        self.assertEqual(response.text, "")
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "controlar_teclado")
        self.assertEqual(
            response.tool_calls[0].arguments,
            {"acao": "pressionar", "tecla": "enter"},
        )

    def test_dots_xml_arguments_follow_tool_schema_types(self):
        markup = (
            '<dots_function_call><invoke name="controlar_mouse">'
            '<parameter name="acao">clicar</parameter>'
            '<parameter name="x">420</parameter>'
            "</invoke></dots_function_call>"
        )
        calls = _parse_dots_function_calls(markup, self.tools)

        self.assertEqual(calls[0].name, "controlar_mouse")
        self.assertEqual(calls[0].arguments, {"acao": "clicar", "x": 420})

    def test_malformed_dots_markup_is_not_exposed_as_assistant_text(self):
        response = self._chat_from_text(
            '<dots_function_call><invoke name="controlar_teclado">'
        )

        self.assertEqual(response.tool_calls, [])
        self.assertNotIn("<dots_function_call>", response.text)
        self.assertIn("formato inválido", response.text)


class DynamicApiKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = _FakeGateway({"sk-antiga", "sk-nova"})
        self.addCleanup(self.gateway.close)

    def test_resolver_is_called_on_every_request(self):
        current = {"value": "sk-antiga"}
        client = FreeLLMClient(
            self.gateway.url, lambda: current["value"], max_retries=0
        )
        self.assertEqual(client.list_models(), ["auto"])
        current["value"] = "sk-nova"
        self.assertEqual(client.list_models(), ["auto"])
        self.assertEqual(
            self.gateway.auth_headers, ["Bearer sk-antiga", "Bearer sk-nova"]
        )

    def test_401_reloads_changed_key_and_retries_once(self):
        gateway = _FakeGateway({"sk-nova"})
        self.addCleanup(gateway.close)
        calls = {"count": 0}

        def resolver() -> str:
            calls["count"] += 1
            return "sk-antiga" if calls["count"] == 1 else "sk-nova"

        client = FreeLLMClient(gateway.url, resolver, max_retries=1)
        self.assertEqual(client.list_models(), ["auto"])
        self.assertEqual(
            gateway.auth_headers, ["Bearer sk-antiga", "Bearer sk-nova"]
        )

    def test_401_with_unchanged_key_stops_immediately(self):
        client = FreeLLMClient(
            self.gateway.url, lambda: "sk-desconhecida", max_retries=3
        )
        with self.assertRaises(ProviderError) as ctx:
            client.list_models()
        self.assertEqual(ctx.exception.kind, ErrorKind.AUTH)
        self.assertEqual(len(self.gateway.auth_headers), 1)

    def test_missing_key_fails_before_any_http_call(self):
        def resolver() -> str:
            raise ConfigError("sem chave")

        client = FreeLLMClient(
            self.gateway.url, resolver, key_env_hint="FREELLMAPI_API_KEY"
        )
        with self.assertRaises(ProviderError) as ctx:
            client.list_models()
        self.assertEqual(ctx.exception.kind, ErrorKind.AUTH)
        self.assertIn("FREELLMAPI_API_KEY", str(ctx.exception))
        self.assertEqual(self.gateway.auth_headers, [])


class DotenvReloadTests(unittest.TestCase):
    def test_api_key_follows_dotenv_edits_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("FREELLMAPI_API_KEY=chave-antiga\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"FREELLMAPI_API_KEY": ""}, clear=False):
                settings = Settings.load(dotenv_path=env_file)
                self.assertEqual(settings.api_key(), "chave-antiga")
                env_file.write_text(
                    "FREELLMAPI_API_KEY=chave-nova\n", encoding="utf-8"
                )
                self.assertEqual(settings.api_key(), "chave-nova")

    def test_environment_wins_over_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("FREELLMAPI_API_KEY=do-arquivo\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ, {"FREELLMAPI_API_KEY": "do-ambiente"}, clear=False
            ):
                settings = Settings.load(dotenv_path=env_file)
                self.assertEqual(settings.api_key(), "do-ambiente")

    def test_missing_key_raises_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("# sem chave\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"FREELLMAPI_API_KEY": "", "AURORA_LLM_API_KEY": ""},
                clear=False,
            ):
                settings = Settings.load(dotenv_path=env_file)
                with self.assertRaises(ConfigError):
                    settings.api_key()
                self.assertFalse(settings.has_api_key())


class VisionOutageFallbackTests(unittest.TestCase):
    def _agent(self) -> AuroraAgent:
        return object.__new__(AuroraAgent)

    def test_unreachable_backend_is_recognized(self):
        error = ProviderError(
            "falha de conexão com http://127.0.0.1:11434/v1/chat/completions: "
            "[WinError 10061] Nenhuma conexão pôde ser feita",
            kind=ErrorKind.UNAVAILABLE,
        )
        self.assertTrue(_is_vision_backend_unreachable(error))

    def test_model_error_is_not_treated_as_outage(self):
        error = ProviderError(
            "HTTP 500: prediction aborted, token repeat limit reached",
            kind=ErrorKind.UNAVAILABLE,
            status=500,
        )
        self.assertFalse(_is_vision_backend_unreachable(error))

    def test_outage_removes_pixels_and_points_to_accessibility(self):
        agent = self._agent()
        messages = [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "Captura da tela para a tarefa."},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            )
        ]
        error = ProviderError(
            "falha de conexão com http://127.0.0.1:11434/v1/chat/completions: recusou",
            kind=ErrorKind.UNAVAILABLE,
        )
        self.assertTrue(agent._handoff_local_vision_outage(messages, error))
        self.assertFalse(_messages_contain_image(messages))
        self.assertIn("somente_acessibilidade", str(messages[0].content))

    def test_outage_without_images_changes_nothing(self):
        agent = self._agent()
        messages = [ChatMessage(role="user", content="sem imagem")]
        error = ProviderError("offline", kind=ErrorKind.UNAVAILABLE)
        self.assertFalse(agent._handoff_local_vision_outage(messages, error))


if __name__ == "__main__":
    unittest.main()

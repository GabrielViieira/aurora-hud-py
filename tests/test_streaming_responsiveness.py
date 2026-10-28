"""Testes offline do streaming incremental usado pela HUD VTuber."""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import types
import unittest
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

from aurora.config.settings import Settings
from aurora.core.agent import _should_stream_response_preview
from aurora.llm.freellm_client import FreeLLMClient
from aurora.llm.provider import ChatMessage, LLMRequest, ProviderError


class _ChunkedResponse:
    """Resposta HTTP falsa que entrega blocos pequenos, como um stream real."""

    status = 200

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FailAfterFirstChunkResponse(_ChunkedResponse):
    """Falha de transporte depois de já entregar um evento SSE."""

    def __init__(self, first_chunk: bytes) -> None:
        super().__init__([first_chunk])
        self._first = True

    def read(self, _size: int = -1) -> bytes:
        if self._first:
            self._first = False
            return super().read(_size)
        raise urllib.error.URLError("conexão interrompida")


def _event(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


class StreamingClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FreeLLMClient(
            "http://localhost:3001/v1", "unit-test-key", max_retries=0
        )

    def _mocked_stream(self, body: bytes, chunk_size: int = 13):
        chunks = [body[i:i + chunk_size] for i in range(0, len(body), chunk_size)]
        chunks.append(b"")
        return mock.patch(
            "urllib.request.urlopen", return_value=_ChunkedResponse(chunks)
        )

    def test_sse_text_is_emitted_incrementally_and_reassembled(self):
        body = b"".join([
            _event({
                "model": "test-model",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Ol"}, "finish_reason": None}],
            }),
            _event({
                "choices": [{"index": 0, "delta": {"content": "á Aurora!"}, "finish_reason": None}],
            }),
            _event({
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }),
            b"data: [DONE]\n\n",
        ])
        # O corte em blocos pode dividir os bytes UTF-8 de "á".
        split = body.index("á".encode("utf-8")) + 1
        chunks = [body[:split], body[split:split + 7], body[split + 7:], b""]
        emitted: list[str] = []
        with mock.patch(
            "urllib.request.urlopen", return_value=_ChunkedResponse(chunks)
        ):
            response = self.client.chat(LLMRequest(
                messages=[ChatMessage(role="user", content="Oi")],
                stream=True,
                on_text_delta=emitted.append,
            ))

        self.assertEqual(response.text, "Olá Aurora!")
        self.assertEqual("".join(emitted), "Olá Aurora!")
        self.assertGreaterEqual(len(emitted), 2)
        self.assertEqual(response.model, "test-model")

    def test_streamed_tool_call_deltas_are_reconstructed(self):
        body = b"".join([
            _event({
                "choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": 0, "id": "call_test", "type": "function",
                    "function": {"name": "pesquisar", "arguments": "{\"consulta\":"},
                }]}, "finish_reason": None}],
            }),
            _event({
                "choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": 0, "function": {"arguments": "\"Steam\"}"},
                }]}, "finish_reason": None}],
            }),
            _event({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
            b"data: [DONE]\n\n",
        ])
        with self._mocked_stream(body, chunk_size=17):
            response = self.client.chat(LLMRequest(
                messages=[ChatMessage(role="user", content="Pesquise na Steam")],
                stream=True,
            ))

        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "pesquisar")
        self.assertEqual(response.tool_calls[0].arguments, {"consulta": "Steam"})

    def test_partial_stream_is_not_retried_after_text_was_exposed(self):
        first_event = _event({
            "choices": [{"index": 0, "delta": {"content": "Começo"}, "finish_reason": None}],
        })
        response_source = _FailAfterFirstChunkResponse(first_event)
        self.client.max_retries = 2
        emitted: list[str] = []
        with mock.patch(
            "urllib.request.urlopen", return_value=response_source
        ) as urlopen:
            with self.assertRaises(ProviderError):
                self.client.chat(LLMRequest(
                    messages=[ChatMessage(role="user", content="Oi")],
                    stream=True,
                    on_text_delta=emitted.append,
                ))

        self.assertEqual(emitted, ["Começo"])
        self.assertEqual(urlopen.call_count, 1)

    def test_json_gateway_response_remains_compatible_when_stream_is_requested(self):
        body = json.dumps({
            "model": "json-fallback",
            "choices": [{"message": {"role": "assistant", "content": "Resposta pronta"}}],
        }).encode("utf-8")
        emitted: list[str] = []
        with self._mocked_stream(body, chunk_size=4096):
            response = self.client.chat(LLMRequest(
                messages=[ChatMessage(role="user", content="Oi")],
                stream=True,
                on_text_delta=emitted.append,
            ))

        self.assertEqual(response.text, "Resposta pronta")
        self.assertEqual(emitted, ["Resposta pronta"])


class StreamingPolicyTests(unittest.TestCase):
    def test_stream_preview_is_enabled_for_plain_conversation(self):
        messages = [ChatMessage(role="user", content="Me conte uma curiosidade")]
        self.assertTrue(_should_stream_response_preview(True, "Me conte uma curiosidade", messages))

    def test_stream_preview_is_disabled_for_desktop_actions(self):
        messages = [ChatMessage(role="user", content="Instale o jogo")]
        self.assertFalse(_should_stream_response_preview(True, "Instale o jogo", messages))

    def test_stream_preview_is_disabled_when_images_are_present(self):
        messages = [ChatMessage(role="user", content=[
            {"type": "text", "text": "O que está na tela?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ])]
        self.assertFalse(_should_stream_response_preview(True, "O que está na tela?", messages))

    def test_stream_preview_respects_explicit_opt_out(self):
        messages = [ChatMessage(role="user", content="Me conte uma curiosidade")]
        self.assertFalse(_should_stream_response_preview(False, "Me conte uma curiosidade", messages))


class StreamingSettingsTests(unittest.TestCase):
    def test_streaming_is_enabled_by_default_but_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / ".env"
            default = Settings.load(env={}, dotenv_path=dotenv)
            disabled = Settings.load(env={"AURORA_LLM_STREAM": "false"}, dotenv_path=dotenv)
        self.assertTrue(default.llm.stream)
        self.assertFalse(disabled.llm.stream)


if __name__ == "__main__":
    unittest.main()

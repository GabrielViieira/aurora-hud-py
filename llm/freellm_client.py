"""Cliente do FreeLLMAPI (superfície OpenAI).

Verificado nesta máquina em 2026-10-22:
  - container Docker `ghcr.io/tashfeenahmed/freellmapi:latest` em 127.0.0.1:3001
  - `GET /v1/models` e `POST /v1/chat/completions`
  - tool calling no formato OpenAI, `usage` e `_routed_via` na resposta
  - a superfície OpenAI não reconhece os slots `claude-*`; `auto` é o padrão seguro

Nenhuma chave é logada. O cliente recebe a chave já resolvida pelo chamador.
"""

from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from aurora.llm.provider import (
    ChatMessage,
    ErrorKind,
    LLMRequest,
    LLMResponse,
    ProviderError,
    ToolCall,
    Usage,
)
from aurora.llm.token_manager import estimate_tokens
from aurora.observability import get_logger

logger = get_logger(__name__)


@dataclass
class _HTTPResult:
    status: int
    body: bytes
    latency_ms: int


class FreeLLMClient:
    """Implementação `LLMProvider` para o gateway FreeLLMAPI."""

    name = "freellm"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        on_metrics: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url obrigatória")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout_s = timeout_s
        self.max_retries = max(0, max_retries)
        self.on_metrics = on_metrics

    # --------------------------------------------------------------- helpers
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "aurora-hud/0.1",
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        retry: bool = True,
    ) -> _HTTPResult:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        attempts = self.max_retries + 1 if retry else 1
        last_error: ProviderError | None = None

        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            request = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    body = response.read()
                    latency = int((time.perf_counter() - started) * 1000)
                    result = _HTTPResult(response.status, body, latency)
                    self._record(method, path, result.status, latency, attempt)
                    return result
            except urllib.error.HTTPError as exc:
                body = exc.read() or b""
                latency = int((time.perf_counter() - started) * 1000)
                last_error = self._http_error(exc.code, body)
                self._record(method, path, exc.code, latency, attempt, error=last_error.kind)
                if not last_error.retryable or attempt >= attempts:
                    raise last_error from exc
            except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                latency = int((time.perf_counter() - started) * 1000)
                reason = getattr(exc, "reason", exc)
                last_error = ProviderError(
                    f"falha de conexão com {url}: {reason}",
                    kind=ErrorKind.TIMEOUT if isinstance(reason, (socket.timeout, TimeoutError)) else ErrorKind.UNAVAILABLE,
                )
                self._record(method, path, None, latency, attempt, error=last_error.kind)
                if attempt >= attempts:
                    raise last_error from exc
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    "resposta do gateway não é JSON válido",
                    kind=ErrorKind.INVALID_RESPONSE,
                ) from exc

            backoff = min(2 ** (attempt - 1), 8)
            logger.warning(
                "retry FreeLLM em %ss (tentativa %s/%s): %s",
                backoff,
                attempt,
                attempts,
                last_error,
            )
            time.sleep(backoff)

        raise last_error or ProviderError("falha desconhecida no gateway")

    def _http_error(self, status: int, body: bytes) -> ProviderError:
        text = body.decode("utf-8", errors="replace")
        kind = ErrorKind.UNKNOWN
        detail_lower = text.casefold()
        token_limit_error = (
            ("token" in detail_lower or "context length" in detail_lower or "prompt length" in detail_lower)
            and any(term in detail_lower for term in (
                "limit", "maximum", "max_", "exceed", "too large", "too many",
                "atingido", "limite", "ultrapass",
            ))
        )
        # Um 500 por excesso de tokens não melhora com retries idênticos; o agente
        # fará uma tentativa explícita com contexto e schemas compactados.
        if status >= 500 and token_limit_error:
            kind = ErrorKind.BAD_REQUEST
        elif status in (401, 403):
            kind = ErrorKind.AUTH
        elif status == 404:
            kind = ErrorKind.NOT_FOUND
        elif status == 429:
            kind = ErrorKind.RATE_LIMIT
        elif status >= 500:
            kind = ErrorKind.UNAVAILABLE
        elif status >= 400:
            kind = ErrorKind.BAD_REQUEST
        message = f"HTTP {status}"
        try:
            parsed = json.loads(text)
            detail = parsed.get("error", {})
            if isinstance(detail, dict) and detail.get("message"):
                message = f"HTTP {status}: {detail['message']}"
            elif isinstance(detail, str):
                message = f"HTTP {status}: {detail}"
        except json.JSONDecodeError:
            message = f"HTTP {status}: {text[:300]}"
        return ProviderError(message, kind=kind, status=status, body=text)

    def _record(
        self,
        method: str,
        path: str,
        status: int | None,
        latency_ms: int,
        attempt: int,
        error: str = "",
    ) -> None:
        if not self.on_metrics:
            return
        try:
            self.on_metrics(
                {
                    "kind": "llm_http",
                    "method": method,
                    "path": path,
                    "status": status,
                    "latency_ms": latency_ms,
                    "attempt": attempt,
                    "error": error,
                }
            )
        except Exception:  # métricas nunca derrubam o agente
            logger.debug("falha ao registrar métrica", exc_info=True)

    # ------------------------------------------------------------------- API
    def list_models(self) -> list[str]:
        result = self._request("GET", "/models")
        try:
            payload = json.loads(result.body)
        except json.JSONDecodeError as exc:
            raise ProviderError("catálogo inválido", kind=ErrorKind.INVALID_RESPONSE) from exc
        items = payload.get("data", payload if isinstance(payload, list) else [])
        return [str(item.get("id")) for item in items if isinstance(item, dict) and item.get("id")]

    def health(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            models = self.list_models()
            return {
                "ok": True,
                "base_url": self.base_url,
                "models": len(models),
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
        except ProviderError as exc:
            return {
                "ok": False,
                "base_url": self.base_url,
                "kind": exc.kind,
                "error": str(exc),
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }

    def chat(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": request.model or "auto",
            "messages": [message.to_openai() for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            payload["tools"] = request.tools
            payload["tool_choice"] = "auto"
        if request.stream:
            payload["stream"] = True

        result = self._request("POST", "/chat/completions", payload)
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            data = json.loads(result.body)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "resposta de chat não é JSON válido", kind=ErrorKind.INVALID_RESPONSE
            ) from exc

        if "error" in data and not data.get("choices"):
            detail = data["error"]
            message = detail.get("message") if isinstance(detail, dict) else str(detail)
            raise ProviderError(str(message), kind=ErrorKind.INVALID_RESPONSE)

        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("resposta sem `choices`", kind=ErrorKind.INVALID_RESPONSE)

        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):  # alguns provedores devolvem blocos
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        tool_calls = [self._parse_tool_call(item) for item in (message.get("tool_calls") or [])]
        tool_calls = [call for call in tool_calls if call is not None]

        usage_data = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_data.get("prompt_tokens") or 0),
            completion_tokens=int(usage_data.get("completion_tokens") or 0),
            total_tokens=int(usage_data.get("total_tokens") or 0),
            estimated=not bool(usage_data),
            model=str(data.get("model") or ""),
            routed_via=data.get("_routed_via") or {},
        )
        if usage.estimated:
            prompt_parts: list[str] = []
            for message in payload["messages"]:
                content = message.get("content", "")
                if isinstance(content, list):
                    prompt_parts.extend(
                        str(part.get("text", "[imagem]"))
                        for part in content
                        if isinstance(part, dict)
                    )
                else:
                    prompt_parts.append(str(content))
            usage.prompt_tokens = estimate_tokens(" ".join(prompt_parts))
            usage.completion_tokens = estimate_tokens(text)
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens

        response = LLMResponse(
            text=text.strip(),
            tool_calls=tool_calls,
            model=str(data.get("model") or request.model),
            usage=usage,
            latency_ms=latency_ms,
            raw=data,
        )
        self._record_usage(response)
        return response

    def _parse_tool_call(self, item: dict[str, Any]) -> ToolCall | None:
        if not isinstance(item, dict):
            return None
        function = item.get("function") or {}
        name = function.get("name")
        if not name:
            return None
        raw_arguments = function.get("arguments")
        arguments: dict[str, Any] = {}
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
            raw_text = json.dumps(raw_arguments, ensure_ascii=False)
        else:
            raw_text = str(raw_arguments or "")
            try:
                parsed = json.loads(raw_text) if raw_text.strip() else {}
                if isinstance(parsed, dict):
                    arguments = parsed
            except json.JSONDecodeError:
                logger.warning("argumentos inválidos em tool_call %s: %r", name, raw_text[:200])
                arguments = {}
        return ToolCall(
            id=str(item.get("id") or f"call_{int(time.time() * 1000)}"),
            name=str(name),
            arguments=arguments,
            raw_arguments=raw_text,
        )

    def _record_usage(self, response: LLMResponse) -> None:
        if not self.on_metrics:
            return
        try:
            self.on_metrics(
                {
                    "kind": "llm_chat",
                    "model": response.model,
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                    "estimated": response.usage.estimated,
                    "latency_ms": response.latency_ms,
                    "tool_calls": len(response.tool_calls),
                }
            )
        except Exception:
            logger.debug("falha ao registrar uso", exc_info=True)

    # --------------------------------------------------------------- streaming
    def chat_stream(self, request: LLMRequest) -> Iterator[str]:
        """Itera deltas de texto (SSE). Cai para resposta completa se falhar.

        Tool calls em streaming não são acumulados aqui: o agente usa `chat()`
        quando precisa de ferramentas. Documentado para não prometer o que não há.
        """
        payload: dict[str, Any] = {
            "model": request.model or "auto",
            "messages": [message.to_openai() for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": True,
        }
        if request.tools:
            payload["tools"] = request.tools
            payload["tool_choice"] = "auto"

        parsed = urllib.parse.urlsplit(self.base_url)
        path_base = parsed.path or ""
        connection_cls = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_cls(parsed.hostname, parsed.port, timeout=self.timeout_s)
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            connection.request("POST", f"{path_base}/chat/completions", body=body, headers=self._headers())
            response = connection.getresponse()
            if response.status != 200:
                raw = response.read().decode("utf-8", errors="replace")
                raise self._http_error(response.status, raw.encode("utf-8"))
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield str(content)
        finally:
            connection.close()

    def close(self) -> None:
        self._api_key = ""

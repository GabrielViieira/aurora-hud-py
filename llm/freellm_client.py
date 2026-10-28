"""Cliente do FreeLLMAPI (superfície OpenAI).

Verificado nesta máquina em 2026-10-22:
  - container Docker `ghcr.io/tashfeenahmed/freellmapi:latest` em 127.0.0.1:3001
  - `GET /v1/models` e `POST /v1/chat/completions`
  - tool calling no formato OpenAI, `usage` e `_routed_via` na resposta
  - a superfície OpenAI não reconhece os slots `claude-*`; `auto` é o padrão seguro

Nenhuma chave é logada. O cliente recebe a chave já resolvida pelo chamador ou
uma função que a resolve a cada requisição, permitindo recarregar uma chave
regenerada no painel do FreeLLM sem reiniciar a Aurora.
"""

from __future__ import annotations

import codecs
import http.client
import json
import re
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


_DOTS_BLOCK_RE = re.compile(
    r"<dots_function_call>\s*(.*?)\s*</dots_function_call>", re.DOTALL
)
_DOTS_INVOKE_RE = re.compile(
    r"<invoke\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)</invoke>",
    re.DOTALL,
)
_DOTS_PARAMETER_RE = re.compile(
    r"<parameter\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)</parameter>",
    re.DOTALL,
)


def _convert_dots_argument(value: str, schema: dict[str, Any]) -> Any:
    """Converte o valor textual do formato Dots segundo o esquema JSON da ferramenta."""
    value = value.strip()
    kind = schema.get("type", "string")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")

    if kind == "string" or not value:
        return value
    if kind == "boolean":
        if value.casefold() == "true":
            return True
        if value.casefold() == "false":
            return False
        return value
    if kind == "integer":
        try:
            return int(value)
        except ValueError:
            return value
    if kind == "number":
        try:
            number = json.loads(value)
            return number if isinstance(number, (int, float)) and not isinstance(number, bool) else value
        except (json.JSONDecodeError, TypeError):
            return value
    if kind in {"array", "object"}:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        if kind == "array" and isinstance(parsed, list):
            return parsed
        if kind == "object" and isinstance(parsed, dict):
            return parsed
    return value


def _parse_dots_function_calls(
    content: str, tool_schemas: list[dict[str, Any]]
) -> list[ToolCall]:
    """Converte chamadas Dots em XML para chamadas nativas, usando esquemas OpenAI."""
    blocks = list(_DOTS_BLOCK_RE.finditer(content))
    if not blocks:
        raise ValueError("nenhum bloco <dots_function_call> completo foi encontrado")
    if (
        content.count("<dots_function_call>") != len(blocks)
        or content.count("</dots_function_call>") != len(blocks)
    ):
        raise ValueError("blocos <dots_function_call> incompletos ou malformados")

    schemas_by_name: dict[str, dict[str, Any]] = {}
    for item in tool_schemas:
        function = item.get("function") or {}
        name = function.get("name")
        if isinstance(name, str):
            schemas_by_name[name] = function.get("parameters") or {}

    calls: list[ToolCall] = []
    call_prefix = f"dots-{int(time.time() * 1000)}"
    for block_index, block in enumerate(blocks, start=1):
        body = block.group(1)
        invokes = list(_DOTS_INVOKE_RE.finditer(body))
        if not invokes or re.sub(_DOTS_INVOKE_RE, "", body).strip():
            raise ValueError("o bloco Dots contém invocações inválidas ou conteúdo inesperado")

        for invoke in invokes:
            name = invoke.group(1).strip()
            if not name:
                raise ValueError("a invocação Dots não contém o nome da ferramenta")
            parameters_schema = schemas_by_name.get(name, {}).get("properties") or {}
            arguments: dict[str, Any] = {}
            parameter_matches = list(_DOTS_PARAMETER_RE.finditer(invoke.group(2)))
            if re.sub(_DOTS_PARAMETER_RE, "", invoke.group(2)).strip():
                raise ValueError(f"parâmetros inválidos na chamada Dots para {name}")
            for parameter in parameter_matches:
                key = parameter.group(1).strip()
                if not key or key in arguments:
                    raise ValueError(f"parâmetro vazio ou duplicado na chamada Dots para {name}")
                arguments[key] = _convert_dots_argument(
                    parameter.group(2), parameters_schema.get(key) or {}
                )
            calls.append(
                ToolCall(
                    id=f"{call_prefix}-{block_index}-{len(calls) + 1}",
                    name=name,
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments, ensure_ascii=False),
                )
            )
    return calls


class _OpenAIStreamCollector:
    """Reconstrói uma resposta OpenAI SSE e emite fragmentos de texto assim que chegam."""

    def __init__(self, on_text_delta: Callable[[str], None] | None = None) -> None:
        self.on_text_delta = on_text_delta
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._text: list[str] = []
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._model = ""
        self._usage: dict[str, Any] = {}
        self._finish_reason: str | None = None
        self.saw_sse = False
        self.error: Any = None

    def feed(self, chunk: bytes) -> None:
        self._buffer += self._decoder.decode(chunk, final=False)
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()
        for line in lines:
            self._consume_line(line.rstrip("\r"))

    def finish(self) -> None:
        self._buffer += self._decoder.decode(b"", final=True)
        if self._buffer.strip():
            self._consume_line(self._buffer.rstrip("\r"))
            self._buffer = ""

    def _consume_line(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        self.saw_sse = True
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return
        try:
            item = json.loads(payload)
        except json.JSONDecodeError:
            return
        if item.get("model"):
            self._model = str(item["model"])
        if isinstance(item.get("usage"), dict):
            self._usage.update(item["usage"])
        if item.get("error"):
            self.error = item["error"]

        for choice in item.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                self._text.append(content)
                if self.on_text_delta is not None:
                    try:
                        self.on_text_delta(content)
                    except Exception:
                        logger.debug("callback de texto incremental falhou", exc_info=True)

            for tool_delta in delta.get("tool_calls") or []:
                try:
                    index = int(tool_delta.get("index", 0))
                except (TypeError, ValueError):
                    continue
                tool = self._tool_calls.setdefault(index, {
                    "id": "", "type": "function", "function": {"name": "", "arguments": ""}
                })
                if tool_delta.get("id"):
                    tool["id"] += str(tool_delta["id"])
                if tool_delta.get("type"):
                    tool["type"] = str(tool_delta["type"])
                function_delta = tool_delta.get("function") or {}
                if function_delta.get("name"):
                    tool["function"]["name"] += str(function_delta["name"])
                if function_delta.get("arguments"):
                    tool["function"]["arguments"] += str(function_delta["arguments"])
            if choice.get("finish_reason") is not None:
                self._finish_reason = str(choice["finish_reason"])

    def as_openai_response(self) -> dict[str, Any]:
        self.finish()
        message: dict[str, Any] = {"role": "assistant", "content": "".join(self._text)}
        if self._tool_calls:
            message["tool_calls"] = [
                self._tool_calls[index] for index in sorted(self._tool_calls)
            ]
        result: dict[str, Any] = {
            "choices": [{"message": message, "finish_reason": self._finish_reason}],
            "model": self._model,
        }
        if self._usage:
            result["usage"] = self._usage
        if self.error is not None:
            result["error"] = self.error
        return result


class FreeLLMClient:
    """Implementação `LLMProvider` para o gateway FreeLLMAPI."""

    name = "freellm"

    def __init__(
        self,
        base_url: str,
        api_key: str | Callable[[], str] = "",
        *,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        on_metrics: Callable[[dict[str, Any]], None] | None = None,
        key_env_hint: str = "",
    ) -> None:
        if not base_url:
            raise ValueError("base_url obrigatória")
        self.base_url = base_url.rstrip("/")
        # A chave pode ser fixa (str) ou uma função resolvida a cada requisição.
        # Com função, regenerar a chave no painel do FreeLLM não exige reiniciar.
        self._key_provider: Callable[[], str] | None = (
            None if isinstance(api_key, str) else api_key
        )
        self._static_key = api_key.strip() if isinstance(api_key, str) else ""
        self.key_env_hint = key_env_hint
        self.timeout_s = timeout_s
        self.max_retries = max(0, max_retries)
        self.on_metrics = on_metrics

    # --------------------------------------------------------------- helpers
    def _resolve_key(self) -> str:
        if self._key_provider is None:
            return self._static_key
        try:
            value = self._key_provider()
        except Exception:  # configuração ausente não derruba o cliente
            logger.debug("falha ao resolver a chave do FreeLLM", exc_info=True)
            return ""
        return str(value or "").strip()

    def _missing_key_error(self) -> ProviderError:
        hint = self.key_env_hint or "a variável de ambiente da chave"
        return ProviderError(
            f"chave do FreeLLM ausente; defina {hint} no .env ou no ambiente",
            kind=ErrorKind.AUTH,
            retryable=False,
        )

    def _headers(self, api_key: str | None = None) -> dict[str, str]:
        key = self._resolve_key() if api_key is None else api_key
        return {
            "Authorization": f"Bearer {key}",
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
        on_chunk: Callable[[bytes], None] | None = None,
    ) -> _HTTPResult:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        attempts = self.max_retries + 1 if retry else 1
        last_error: ProviderError | None = None

        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            api_key = self._resolve_key()
            if not api_key:
                raise self._missing_key_error()
            request = urllib.request.Request(
                url, data=data, headers=self._headers(api_key), method=method
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    if on_chunk is None:
                        body = response.read()
                    else:
                        chunks: list[bytes] = []
                        while True:
                            chunk = response.read(4096)
                            if not chunk:
                                break
                            chunks.append(chunk)
                            on_chunk(chunk)
                        body = b"".join(chunks)
                    latency = int((time.perf_counter() - started) * 1000)
                    result = _HTTPResult(response.status, body, latency)
                    self._record(method, path, result.status, latency, attempt)
                    return result
            except urllib.error.HTTPError as exc:
                body = exc.read() or b""
                latency = int((time.perf_counter() - started) * 1000)
                last_error = self._http_error(exc.code, body)
                self._record(method, path, exc.code, latency, attempt, error=last_error.kind)
                if (
                    last_error.kind == ErrorKind.AUTH
                    and self._key_provider is not None
                    and attempt < attempts
                ):
                    # A chave pode ter sido regenerada no painel do gateway.
                    # Recarrega e repete uma vez antes de desistir.
                    reloaded = self._resolve_key()
                    if reloaded and reloaded != api_key:
                        logger.warning(
                            "chave do FreeLLM recarregada após HTTP %s; repetindo a requisição",
                            exc.code,
                        )
                        continue
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

        stream_collector = (
            _OpenAIStreamCollector(request.on_text_delta)
            if request.stream else None
        )
        result = self._request(
            "POST", "/chat/completions", payload,
            on_chunk=stream_collector.feed if stream_collector is not None else None,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            data = json.loads(result.body)
            # Alguns gateways aceitam stream=true, mas ainda devolvem JSON completo.
            # Nesse caso, preserva compatibilidade e entrega um único delta final.
            if stream_collector is not None and not stream_collector.saw_sse:
                message = (data.get("choices") or [{}])[0].get("message") or {}
                content = message.get("content")
                if isinstance(content, str) and content and request.on_text_delta is not None:
                    request.on_text_delta(content)
        except json.JSONDecodeError as exc:
            if stream_collector is None or not stream_collector.saw_sse:
                raise ProviderError(
                    "resposta de chat não é JSON válido", kind=ErrorKind.INVALID_RESPONSE
                ) from exc
            data = stream_collector.as_openai_response()

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

        # Alguns modelos (incluindo modelos da família Dots) emitem chamadas
        # no corpo textual como XML. Se o gateway não as normalizou em
        # message.tool_calls, converta-as aqui antes de o agente ver o texto.
        if "<dots_function_call>" in text:
            if tool_calls:
                text = _DOTS_BLOCK_RE.sub("", text).strip()
            elif request.tools:
                try:
                    tool_calls = _parse_dots_function_calls(text, request.tools)
                    text = _DOTS_BLOCK_RE.sub("", text).strip()
                    logger.info(
                        "Chamadas Dots em XML convertidas para tool_calls nativas",
                        extra={"aurora": {"tool_calls": len(tool_calls)}},
                    )
                except (ValueError, re.error) as exc:
                    logger.warning(
                        "resposta contém chamada Dots inválida; nenhuma ação será executada: %s",
                        str(exc)[:180],
                    )
                    text = (
                        "O modelo retornou uma chamada de ferramenta em formato inválido. "
                        "Nenhuma ação foi executada. Tente novamente."
                    )
            else:
                # Não exponha o protocolo interno de ferramentas como resposta ao usuário,
                # especialmente na rota visual local, que não recebe esquemas de ferramentas.
                text = (
                    "O modelo retornou uma chamada de ferramenta onde era esperada uma resposta textual. "
                    "Nenhuma ação foi executada."
                )

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
        api_key = self._resolve_key()
        if not api_key:
            raise self._missing_key_error()
        connection_cls = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_cls(parsed.hostname, parsed.port, timeout=self.timeout_s)
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            connection.request(
                "POST",
                f"{path_base}/chat/completions",
                body=body,
                headers=self._headers(api_key),
            )
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
        self._static_key = ""
        self._key_provider = None

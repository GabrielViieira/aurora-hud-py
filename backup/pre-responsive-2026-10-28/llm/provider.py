"""Contrato do provedor de LLM.

O núcleo do agente conhece apenas este protocolo. Trocar FreeLLM por Ollama,
OpenAI ou um modelo local significa implementar `LLMProvider`, sem mudar o
agente, as ferramentas ou a memória.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ErrorKind(str):
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    INVALID_RESPONSE = "invalid_response"
    BAD_REQUEST = "bad_request"
    UNKNOWN = "unknown"


RETRYABLE_KINDS = {ErrorKind.TIMEOUT, ErrorKind.UNAVAILABLE, ErrorKind.RATE_LIMIT, ErrorKind.INVALID_RESPONSE}


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = ErrorKind.UNKNOWN,
        status: int | None = None,
        retryable: bool | None = None,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retryable = RETRYABLE_KINDS.__contains__(kind) if retryable is None else retryable
        self.body = body[:2000]

    def user_message(self) -> str:
        mapping = {
            ErrorKind.TIMEOUT: "o provedor de IA demorou demais para responder",
            ErrorKind.UNAVAILABLE: "o provedor de IA está indisponível no momento",
            ErrorKind.RATE_LIMIT: "o provedor de IA atingiu o limite de uso",
            ErrorKind.AUTH: "a chave da API foi recusada",
            ErrorKind.NOT_FOUND: "o modelo pedido não existe no catálogo do gateway",
            ErrorKind.INVALID_RESPONSE: "o provedor devolveu uma resposta inválida",
            ErrorKind.BAD_REQUEST: "o pedido foi recusado pelo provedor",
        }
        return mapping.get(self.kind, "falha ao falar com o provedor de IA")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False  # True quando calculado localmente
    model: str = ""
    routed_via: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatMessage:
    role: str
    content: str | list[dict[str, Any]] = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""

    def to_openai(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role}
        if self.role == "assistant" and self.tool_calls:
            payload["content"] = self.content or None
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": _dumps(call.arguments)},
                }
                for call in self.tool_calls
            ]
        else:
            payload["content"] = self.content
        if self.role == "tool":
            payload["tool_call_id"] = self.tool_call_id
            if self.name:
                payload["name"] = self.name
        return payload


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class LLMRequest:
    messages: list[ChatMessage]
    tools: list[dict[str, Any]] = field(default_factory=list)
    model: str = "auto"
    temperature: float = 0.3
    max_tokens: int = 1200
    stream: bool = False


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def chat(self, request: LLMRequest) -> LLMResponse: ...

    def list_models(self) -> list[str]: ...

    def health(self) -> dict[str, Any]: ...

    def close(self) -> None: ...

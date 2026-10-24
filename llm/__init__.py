"""Camada de LLM: contrato, cliente FreeLLM, roteamento e prompts."""

from aurora.llm.freellm_client import FreeLLMClient
from aurora.llm.model_router import ModelRouter
from aurora.llm.provider import (
    ChatMessage,
    ErrorKind,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderError,
    ToolCall,
    Usage,
)

__all__ = [
    "ChatMessage",
    "ErrorKind",
    "FreeLLMClient",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "ModelRouter",
    "ProviderError",
    "ToolCall",
    "Usage",
]

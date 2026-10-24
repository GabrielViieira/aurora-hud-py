"""Estimativa local de tokens (sem tokenizer oficial).

O FreeLLM informa `usage` nas respostas, mas o orçamento precisa ser estimado
*antes* da chamada. A heurística abaixo é conservadora para português/inglês:
~3,5 caracteres por token. Números são estimativas locais, nunca cobrança.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    # 1 token ≈ 4 chars em inglês; em pt-BR com acentos ~3,5. Usamos 3.5.
    return max(1, math.ceil(len(text) / 3.5))


def truncate_to_tokens(text: str, max_tokens: int, suffix: str = "\n[...truncado]") -> str:
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    budget_chars = max(1, int(max_tokens * 3.5) - len(suffix))
    return text[:budget_chars].rstrip() + suffix


def estimate_messages(messages: Iterable[object]) -> int:
    """Aceita ChatMessage ou dicts no formato OpenAI."""
    total = 0
    for message in messages:
        if isinstance(message, dict):
            content = message.get("content") or ""
            role = message.get("role") or ""
            tool_calls = message.get("tool_calls") or []
        else:
            content = getattr(message, "content", "") or ""
            role = getattr(message, "role", "") or ""
            tool_calls = getattr(message, "tool_calls", None) or []
        total += estimate_tokens(str(content)) + 4
        total += estimate_tokens(role)
        if tool_calls:
            total += estimate_tokens(str(tool_calls)) + 8
    return total


def trim_messages_to_budget(
    messages: Sequence[dict],
    max_tokens: int,
    keep_first: int = 1,
) -> list[dict]:
    """Remove mensagens do meio (mais antigas) preservando início e fim."""
    items = list(messages)
    if estimate_messages(items) <= max_tokens:
        return items
    keep_tail = max(2, len(items) // 2)
    head = items[:keep_first]
    tail = items[-keep_tail:]
    while estimate_messages(head + tail) > max_tokens and len(tail) > 2:
        tail.pop(0)
    return head + tail

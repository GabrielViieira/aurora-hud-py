"""Montagem de contexto com orçamento de tokens.

Blocos separados: sistema, memórias, resumo, histórico recente, mensagem atual
e reserva para a resposta. Instruções de sistema nunca são cortadas.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aurora.config.settings import Settings
from aurora.llm.prompts import SUMMARIZER_PROMPT
from aurora.llm.provider import ChatMessage, LLMRequest
from aurora.llm.token_manager import estimate_messages, estimate_tokens, truncate_to_tokens


@dataclass
class ContextReport:
    context_window: int
    reserve_output: int
    tokens_estimate: int = 0
    blocks: dict[str, int] = field(default_factory=dict)
    memories_used: int = 0
    recent_used: int = 0

    @property
    def usage_ratio(self) -> float:
        if not self.context_window:
            return 0.0
        return self.tokens_estimate / self.context_window


class ContextManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.context_window = max(2000, settings.llm.context_window)
        self.reserve_output = min(
            max(256, settings.llm.max_output_tokens), max(256, self.context_window // 4)
        )

    def build(
        self,
        *,
        system: str,
        memories: list[str],
        summary: str,
        recent: list[ChatMessage],
        user_text: str,
    ) -> tuple[list[ChatMessage], ContextReport]:
        budget = max(1000, self.context_window - self.reserve_output - 200)
        report = ContextReport(
            context_window=self.context_window, reserve_output=self.reserve_output
        )

        system_tokens = estimate_tokens(system)
        user_tokens = estimate_tokens(user_text)
        report.blocks["sistema"] = system_tokens
        report.blocks["mensagem_atual"] = user_tokens
        remaining = budget - system_tokens - user_tokens
        if remaining <= 0:
            # Caso extremo: corta a mensagem do usuário, nunca o sistema.
            user_text = truncate_to_tokens(user_text, max(64, budget - system_tokens))
            remaining = max(64, budget - system_tokens - estimate_tokens(user_text))
            report.blocks["mensagem_atual"] = estimate_tokens(user_text)

        memory_block = ""
        if memories and remaining > 120:
            memory_budget = min(remaining // 3, 1200)
            raw = "Memórias relevantes (use apenas se fizer sentido):\n" + "\n".join(
                f"- {item}" for item in memories
            )
            memory_block = truncate_to_tokens(raw, memory_budget)
            memory_tokens = estimate_tokens(memory_block)
            remaining -= memory_tokens
            report.blocks["memorias"] = memory_tokens
            report.memories_used = len(memories)

        summary_block = ""
        if summary and remaining > 120:
            summary_budget = min(remaining // 3, 900)
            summary_block = truncate_to_tokens(
                f"Resumo da conversa anterior:\n{summary}", summary_budget
            )
            summary_tokens = estimate_tokens(summary_block)
            remaining -= summary_tokens
            report.blocks["resumo"] = summary_tokens

        keep: list[ChatMessage] = []
        for message in reversed(recent):
            cost = estimate_messages([message])
            if cost > remaining:
                break
            remaining -= cost
            keep.append(message)
        keep.reverse()
        report.blocks["historico"] = estimate_messages(keep)
        report.recent_used = len(keep)

        messages: list[ChatMessage] = [ChatMessage(role="system", content=system)]
        if memory_block:
            messages.append(ChatMessage(role="system", content=memory_block))
        if summary_block:
            messages.append(ChatMessage(role="system", content=summary_block))
        messages.extend(keep)
        messages.append(ChatMessage(role="user", content=user_text))

        report.tokens_estimate = estimate_messages(messages)
        return messages, report

    def should_compact(self, message_count: int) -> bool:
        return message_count >= 40


class Summarizer:
    """Compacta histórico antigo usando o modelo utilitário."""

    def __init__(self, provider, model: str = "auto", timeout_s: float = 60.0) -> None:
        self.provider = provider
        self.model = model
        self.timeout_s = timeout_s

    def summarize(self, messages: list[ChatMessage]) -> str:
        if not messages:
            return ""
        transcript = "\n".join(f"{item.role}: {item.content}" for item in messages if item.content)
        transcript = truncate_to_tokens(transcript, 5000)
        request = LLMRequest(
            messages=[
                ChatMessage(role="system", content=SUMMARIZER_PROMPT),
                ChatMessage(role="user", content=transcript),
            ],
            model=self.model,
            temperature=0.1,
            max_tokens=500,
        )
        response = self.provider.chat(request)
        return response.text.strip()

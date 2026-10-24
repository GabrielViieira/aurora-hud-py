"""Memória de sessão em processo: janela rolante da conversa atual."""

from __future__ import annotations

from dataclasses import dataclass, field

from aurora.llm.provider import ChatMessage
from aurora.llm.token_manager import estimate_messages


@dataclass
class ShortTermMemory:
    max_messages: int = 30
    max_tokens: int = 6000
    messages: list[ChatMessage] = field(default_factory=list)

    def add(self, message: ChatMessage) -> None:
        self.messages.append(message)
        self._trim()

    def add_user(self, content: str) -> None:
        self.add(ChatMessage(role="user", content=content))

    def add_assistant(self, content: str) -> None:
        self.add(ChatMessage(role="assistant", content=content))

    def add_tool(self, tool_call_id: str, name: str, content: str) -> None:
        self.add(ChatMessage(role="tool", content=content, tool_call_id=tool_call_id, name=name))

    def extend(self, messages: list[ChatMessage]) -> None:
        for message in messages:
            self.add(message)

    def snapshot(self) -> list[ChatMessage]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()

    def token_estimate(self) -> int:
        return estimate_messages(self.messages)

    def _trim(self) -> None:
        while len(self.messages) > self.max_messages:
            self.messages.pop(0)
        while self.messages and self.token_estimate() > self.max_tokens and len(self.messages) > 2:
            self.messages.pop(0)

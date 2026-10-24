"""Memória semântica de alto nível: preferências e fatos sobre projetos."""

from __future__ import annotations

from aurora.memory.long_term import LongTermMemory, MemoryRecord


class KnowledgeStore:
    """Fachada seletiva sobre a camada semântica.

    Evita transformar toda mensagem em memória: só o que for explicitamente
    preferência/fato durável deve passar por aqui.
    """

    def __init__(self, memory: LongTermMemory) -> None:
        self.memory = memory

    def remember_preference(self, content: str, tags: tuple[str, ...] = ("preferencia",)) -> int | None:
        return self.memory.remember(
            content, layer="semantic", kind="preference", tags=tags, importance=0.75, source="user"
        )

    def remember_project_fact(
        self, content: str, project: str, tags: tuple[str, ...] = ()
    ) -> int | None:
        return self.memory.remember(
            content,
            layer="semantic",
            kind="project_fact",
            tags=(project, *tags),
            importance=0.65,
            source="user",
        )

    def about(self, topic: str, limit: int = 5) -> list[MemoryRecord]:
        return self.memory.search(topic, limit=limit, layers=("semantic",))

    def all_preferences(self, limit: int = 50) -> list[MemoryRecord]:
        return [
            record
            for record in self.memory.all(limit=limit, layer="semantic")
            if record.kind in {"preference", "project_fact"}
        ]

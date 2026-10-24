"""Memória de longo prazo: episódica e semântica em SQLite.

Recuperação combina relevância lexical, importância declarada e recência.
Embeddings ficam para uma fase opcional; nada aqui exige serviço externo.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from aurora.observability import get_logger, redact_text
from aurora.storage.database import Database

logger = get_logger(__name__)

LAYERS = ("episodic", "semantic")
_TOKEN_RE = re.compile(r"[\wÀ-ÿ]{3,}", re.UNICODE)
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|senha|password|authorization)\s*[:=]\s*\S+"
    r"|freellmapi-[A-Za-z0-9._-]{8,}"
    r"|\bsk-[A-Za-z0-9._-]{8,}"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(content: str) -> str:
    normalized = " ".join(content.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _tokens(text: str) -> set[str]:
    return {match.group(0).lower() for match in _TOKEN_RE.finditer(text)}


def contains_secret(text: str) -> bool:
    return bool(_SECRET_RE.search(text))


@dataclass(frozen=True)
class MemoryRecord:
    id: int
    layer: str
    kind: str
    content: str
    tags: tuple[str, ...]
    source: str
    importance: float
    created_at: str
    last_used_at: str
    use_count: int

    @classmethod
    def from_row(cls, row) -> "MemoryRecord":
        tags = tuple(part for part in str(row["tags"] or "").split(",") if part)
        return cls(
            id=int(row["id"]),
            layer=str(row["layer"]),
            kind=str(row["kind"]),
            content=str(row["content"]),
            tags=tags,
            source=str(row["source"]),
            importance=float(row["importance"]),
            created_at=str(row["created_at"]),
            last_used_at=str(row["last_used_at"] or ""),
            use_count=int(row["use_count"] or 0),
        )

    def render(self) -> str:
        tag_text = f" [{', '.join(self.tags)}]" if self.tags else ""
        return f"({self.layer}/{self.kind}) {self.content}{tag_text}"


class LongTermMemory:
    def __init__(self, db: Database) -> None:
        self.db = db

    def remember(
        self,
        content: str,
        *,
        layer: str = "semantic",
        kind: str = "fact",
        tags: Iterable[str] = (),
        importance: float = 0.5,
        source: str = "user",
        session_id: str | None = None,
    ) -> int | None:
        content = " ".join(content.split()).strip()
        if not content:
            return None
        if layer not in LAYERS:
            raise ValueError(f"camada inválida: {layer}")
        if contains_secret(content):
            logger.warning("memória recusada: conteúdo parece conter credencial")
            return None

        importance = max(0.0, min(1.0, float(importance)))
        digest = _hash(content)
        existing = self.db.query_one(
            "SELECT id, importance FROM memories WHERE layer = ? AND content_hash = ?",
            (layer, digest),
        )
        if existing:
            self.db.execute(
                "UPDATE memories SET importance = MAX(importance, ?) WHERE id = ?",
                (importance, existing["id"]),
            )
            return int(existing["id"])

        tag_text = ",".join(sorted({str(tag).strip() for tag in tags if str(tag).strip()}))
        memory_id = self.db.execute(
            "INSERT INTO memories "
            "(layer, kind, content, content_hash, tags, source, importance, session_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (layer, kind, content, digest, tag_text, source, importance, session_id, _now()),
        )
        logger.info("memória gravada", extra={"aurora": {"layer": layer, "kind": kind, "id": memory_id}})
        return memory_id

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        layers: tuple[str, ...] = LAYERS,
        min_score: float = 0.05,
    ) -> list[MemoryRecord]:
        placeholders = ",".join("?" for _ in layers)
        rows = self.db.query(
            f"SELECT * FROM memories WHERE layer IN ({placeholders}) "
            "ORDER BY created_at DESC LIMIT 400",
            tuple(layers),
        )
        if not rows:
            return []

        query_tokens = _tokens(query)
        now = datetime.now(timezone.utc)
        scored: list[tuple[float, MemoryRecord]] = []
        for row in rows:
            record = MemoryRecord.from_row(row)
            doc_tokens = _tokens(record.content + " " + " ".join(record.tags))
            if query_tokens:
                overlap = len(query_tokens & doc_tokens) / len(query_tokens)
            else:
                overlap = 0.0
            age_days = _age_days(record.created_at, now)
            recency = math.exp(-age_days / 45.0)
            score = 0.62 * overlap + 0.26 * record.importance + 0.12 * recency
            if query_tokens and overlap == 0:
                continue
            if score >= min_score:
                scored.append((score, record))

        scored.sort(key=lambda item: item[0], reverse=True)
        selected = [record for _, record in scored[: max(1, limit)]]

        for record in selected:
            self.db.execute(
                "UPDATE memories SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
                (_now(), record.id),
            )
        return selected

    def all(self, limit: int = 50, layer: str | None = None) -> list[MemoryRecord]:
        if layer:
            rows = self.db.query(
                "SELECT * FROM memories WHERE layer = ? ORDER BY created_at DESC LIMIT ?",
                (layer, limit),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        return [MemoryRecord.from_row(row) for row in rows]

    def get(self, memory_id: int) -> MemoryRecord | None:
        row = self.db.query_one("SELECT * FROM memories WHERE id = ?", (memory_id,))
        return MemoryRecord.from_row(row) if row else None

    def update(self, memory_id: int, *, content: str | None = None, importance: float | None = None) -> bool:
        record = self.get(memory_id)
        if record is None:
            return False
        new_content = record.content if content is None else " ".join(content.split()).strip()
        if not new_content:
            return False
        if contains_secret(new_content):
            raise ValueError("conteúdo parece conter credencial; recusado")
        new_importance = record.importance if importance is None else max(0.0, min(1.0, importance))
        self.db.execute(
            "UPDATE memories SET content = ?, content_hash = ?, importance = ? WHERE id = ?",
            (new_content, _hash(new_content), new_importance, memory_id),
        )
        return True

    def delete(self, memory_id: int) -> bool:
        if self.get(memory_id) is None:
            return False
        self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return True

    def export(self, path: Path) -> int:
        records = self.all(limit=100000)
        payload = [
            {
                "id": record.id,
                "layer": record.layer,
                "kind": record.kind,
                "content": record.content,
                "tags": list(record.tags),
                "source": record.source,
                "importance": record.importance,
                "created_at": record.created_at,
            }
            for record in records
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(payload)

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM memories")
        return int(row["n"]) if row else 0

    def forget_all(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM memories")
        total = int(row["n"]) if row else 0
        self.db.execute("DELETE FROM memories")
        return total


def _age_days(created_at: str, now: datetime) -> float:
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0.0, (now - created).total_seconds() / 86400.0)
    except ValueError:
        return 365.0

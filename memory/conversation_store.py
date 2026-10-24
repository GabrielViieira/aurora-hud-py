"""Persistência da conversa por sessão (camada A de memória)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from aurora.llm.token_manager import estimate_tokens
from aurora.storage.database import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class StoredMessage:
    id: int
    role: str
    content: str
    created_at: str


class ConversationStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create_session(self, title: str = "") -> str:
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        self.db.execute(
            "INSERT INTO sessions (id, started_at, title) VALUES (?, ?, ?)",
            (session_id, _now(), title),
        )
        return session_id

    def end_session(self, session_id: str) -> None:
        self.db.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?", (_now(), session_id)
        )

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        meta: dict | None = None,
    ) -> int:
        return self.db.execute(
            "INSERT INTO messages (session_id, role, content, created_at, tokens_est, meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                role,
                content,
                _now(),
                estimate_tokens(content),
                json.dumps(meta or {}, ensure_ascii=False),
            ),
        )

    def recent(self, session_id: str, limit: int = 20, exclude_last: int = 0) -> list[StoredMessage]:
        rows = self.db.query(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, max(0, limit) + max(0, exclude_last)),
        )
        items = [StoredMessage(int(r["id"]), str(r["role"]), str(r["content"]), str(r["created_at"])) for r in rows]
        if exclude_last:
            items = items[exclude_last:]
        items.reverse()
        return items

    def set_summary(self, session_id: str, summary: str) -> None:
        self.db.execute("UPDATE sessions SET summary = ? WHERE id = ?", (summary, session_id))

    def get_summary(self, session_id: str) -> str:
        row = self.db.query_one("SELECT summary FROM sessions WHERE id = ?", (session_id,))
        return str(row["summary"] or "") if row else ""

    def session_exists(self, session_id: str) -> bool:
        return self.db.query_one("SELECT id FROM sessions WHERE id = ?", (session_id,)) is not None

    def sessions(self, limit: int = 20) -> list[dict]:
        rows = self.db.query(
            "SELECT id, started_at, ended_at, title, summary FROM sessions "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def count_messages(self, session_id: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (session_id,)
        )
        return int(row["n"]) if row else 0

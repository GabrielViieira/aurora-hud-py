"""Estados explícitos de tarefa, passos e checkpoints em SQLite."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from aurora.storage.database import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TaskStatus(str, Enum):
    IDLE = "IDLE"
    PLANNING = "PLANNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}

VALID_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.IDLE: {TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.PLANNING: {
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.WAITING_APPROVAL: {
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.COMPLETED,
    },
    TaskStatus.RUNNING: {
        TaskStatus.VERIFYING,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.VERIFYING: {
        TaskStatus.RUNNING,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: {TaskStatus.RUNNING},
    TaskStatus.CANCELLED: set(),
}


class InvalidTransition(RuntimeError):
    pass


@dataclass
class Step:
    index: int
    description: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    status: str = "pendente"
    ok: bool | None = None
    summary: str = ""
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "description": self.description,
            "tool": self.tool,
            "args": self.args,
            "status": self.status,
            "ok": self.ok,
            "summary": self.summary,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Step":
        return cls(
            index=int(data.get("index", 0)),
            description=str(data.get("description", "")),
            tool=str(data.get("tool", "")),
            args=dict(data.get("args") or {}),
            status=str(data.get("status", "pendente")),
            ok=data.get("ok"),
            summary=str(data.get("summary", "")),
            attempts=int(data.get("attempts", 0)),
        )


@dataclass
class Task:
    id: str
    goal: str
    session_id: str = ""
    status: TaskStatus = TaskStatus.IDLE
    steps: list[Step] = field(default_factory=list)
    error: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @classmethod
    def create(cls, goal: str, session_id: str = "") -> "Task":
        return cls(id=f"task_{uuid.uuid4().hex[:12]}", goal=goal, session_id=session_id)

    def transition(self, new_status: TaskStatus) -> None:
        if new_status == self.status:
            return
        if new_status not in VALID_TRANSITIONS[self.status]:
            raise InvalidTransition(
                f"transição inválida: {self.status.value} -> {new_status.value}"
            )
        self.status = new_status
        self.updated_at = _now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "session_id": self.session_id,
            "status": self.status.value,
            "steps": [step.to_dict() for step in self.steps],
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            id=str(data["id"]),
            goal=str(data.get("goal", "")),
            session_id=str(data.get("session_id", "")),
            status=TaskStatus(str(data.get("status", "IDLE"))),
            steps=[Step.from_dict(item) for item in data.get("steps") or []],
            error=str(data.get("error", "")),
            created_at=str(data.get("created_at", _now())),
            updated_at=str(data.get("updated_at", _now())),
        )

    def pending_steps(self) -> list[Step]:
        return [step for step in self.steps if step.status in {"pendente", "falhou", "executando"}]


class TaskStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, task: Task) -> None:
        task.updated_at = _now()
        self.db.execute(
            "INSERT INTO tasks (id, session_id, goal, status, plan_json, checkpoint_json, error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, plan_json=excluded.plan_json, "
            "checkpoint_json=excluded.checkpoint_json, error=excluded.error, updated_at=excluded.updated_at",
            (
                task.id,
                task.session_id,
                task.goal,
                task.status.value,
                json.dumps([step.to_dict() for step in task.steps], ensure_ascii=False),
                json.dumps(task.to_dict(), ensure_ascii=False),
                task.error,
                task.created_at,
                task.updated_at,
            ),
        )

    def checkpoint(self, task: Task) -> None:
        self.save(task)

    def load(self, task_id: str) -> Task | None:
        row = self.db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            return None
        return self._from_row(row)

    def recent(self, limit: int = 10) -> list[Task]:
        rows = self.db.query(
            "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        return [self._from_row(row) for row in rows]

    def pending(self, limit: int = 20) -> list[Task]:
        rows = self.db.query(
            "SELECT * FROM tasks WHERE status IN ('PLANNING','WAITING_APPROVAL','RUNNING','VERIFYING') "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [self._from_row(row) for row in rows]

    def record_tool_run(
        self,
        *,
        task_id: str,
        tool: str,
        args: dict,
        risk: str,
        status: str,
        summary: str,
        started_at: str,
        duration_ms: int,
    ) -> None:
        import json as _json

        self.db.execute(
            "INSERT INTO tool_runs (task_id, tool, args_json, risk, status, summary, started_at, finished_at, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                tool,
                _json.dumps(args, ensure_ascii=False, default=str)[:4000],
                risk,
                status,
                summary[:1000],
                started_at,
                _now(),
                duration_ms,
            ),
        )

    def _from_row(self, row) -> Task:
        try:
            payload = json.loads(row["checkpoint_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not payload:
            payload = {
                "id": row["id"],
                "goal": row["goal"],
                "session_id": row["session_id"] or "",
                "status": row["status"],
                "plan": row["plan_json"],
                "error": row["error"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        if "steps" not in payload:
            try:
                payload["steps"] = json.loads(row["plan_json"] or "[]")
            except json.JSONDecodeError:
                payload["steps"] = []
        return Task.from_dict(payload)

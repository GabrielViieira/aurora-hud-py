"""Trilha de auditoria em JSONL.

Registra o que foi feito, com argumentos resumidos e redigidos. Conteúdo
integral de arquivos e credenciais nunca vai para o log de auditoria.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aurora.observability import redact, redact_text


def summarize_args(args: dict[str, Any], limit: int = 240) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str):
            text = redact_text(value)
            summary[key] = text if len(text) <= limit else f"{text[:limit]}… ({len(text)} chars)"
        elif isinstance(value, (int, float, bool)) or value is None:
            summary[key] = value
        else:
            summary[key] = redact(value)
    return summary


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, event: str, **fields: Any) -> None:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            **redact(fields),
        }
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def tail(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

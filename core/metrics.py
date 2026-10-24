"""Métricas locais (latência, tokens estimados, ferramentas)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from aurora.storage.database import Database


class Metrics:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, kind: str, *, value: float = 0, model: str = "", **meta: Any) -> None:
        self.db.execute(
            "INSERT INTO metrics (ts, kind, model, value, meta_json) VALUES (?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                kind,
                model,
                float(value),
                json.dumps(meta, ensure_ascii=False, default=str),
            ),
        )

    def summary(self, limit: int = 2000) -> dict[str, Any]:
        rows = self.db.query(
            "SELECT kind, model, value, meta_json FROM metrics ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        total_calls = 0
        errors = 0
        tokens = 0
        latencies: list[float] = []
        models: dict[str, int] = {}
        for row in rows:
            kind = row["kind"]
            if kind == "llm_chat":
                total_calls += 1
                meta = _loads(row["meta_json"])
                tokens += int(meta.get("total_tokens") or 0)
                if row["value"]:
                    latencies.append(float(row["value"]))
                model = str(row["model"] or "?")
                models[model] = models.get(model, 0) + 1
            if kind == "llm_http" and _loads(row["meta_json"]).get("error"):
                errors += 1
        latencies.sort()

        def pct(p: float) -> float:
            if not latencies:
                return 0.0
            index = min(len(latencies) - 1, int(round((len(latencies) - 1) * p)))
            return round(latencies[index], 1)

        return {
            "amostras": len(rows),
            "chamadas_llm": total_calls,
            "tokens_estimados": tokens,
            "erros_http": errors,
            "latencia_p50_ms": pct(0.5),
            "latencia_p95_ms": pct(0.95),
            "modelos": models,
        }


def _loads(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}

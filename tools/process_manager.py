"""Consulta de processos do Windows (somente leitura)."""

from __future__ import annotations

import csv
import io
import subprocess
from typing import Any

from aurora.config.permissions import Risk
from aurora.tools.base import ToolContext, ToolResult, ToolSpec


def _run_tasklist() -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if completed.returncode != 0:
        return False, completed.stderr.decode("utf-8", errors="replace")[:500]
    return True, completed.stdout.decode("utf-8-sig", errors="replace")


def _parse_kib(text: str) -> int:
    cleaned = text.replace("K", "").replace(".", "").replace(",", "").replace("\xa0", "").strip()
    try:
        return int(cleaned)
    except ValueError:
        return 0


def list_processes(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ok, output = _run_tasklist()
    if not ok:
        return ToolResult.fail(f"não foi possível listar processos: {output}")

    filtro = str(args.get("filtro") or "").lower()
    limit = min(int(args.get("max_resultados") or 40), 200)
    items: list[dict[str, Any]] = []
    total = 0
    reader = csv.reader(io.StringIO(output))
    for row in reader:
        if len(row) < 5:
            continue
        name, pid, session, session_num, memory = row[0], row[1], row[2], row[3], row[4]
        total += 1
        if filtro and filtro not in name.lower():
            continue
        if len(items) >= limit:
            continue
        items.append(
            {
                "nome": name,
                "pid": int(pid) if pid.isdigit() else pid,
                "sessao": session,
                "memoria_kb": _parse_kib(memory),
            }
        )

    items.sort(key=lambda item: item["memoria_kb"], reverse=True)
    return ToolResult(
        ok=True,
        summary=f"{len(items)} processo(s) exibido(s) de {total} no sistema",
        data={"processos": items, "total_sistema": total},
    )


def build_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="listar_processos",
            description="Lista processos do Windows com PID e uso de memória (somente leitura).",
            parameters={
                "type": "object",
                "properties": {
                    "filtro": {"type": "string", "description": "Trecho do nome do processo"},
                    "max_resultados": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "additionalProperties": False,
            },
            risk=Risk.L0,
            handler=list_processes,
            category="sistema",
        )
    ]

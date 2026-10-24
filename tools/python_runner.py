"""Execução controlada de Python.

Não é uma sandbox de segurança: é um executor com timeout, saída limitada,
diretório controlado e ambiente saneado. Isolamento forte (container/VM) fica
para uma fase própria e está documentado como limitação.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from aurora.config.permissions import PathDenied, Risk
from aurora.tools.base import ToolContext, ToolResult, ToolSpec

_SECRET_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")


def _sanitized_env(workspace: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if any(marker in upper for marker in _SECRET_ENV_MARKERS):
            continue
        if upper.startswith("AURORA_"):
            continue
        env[name] = value
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = ""
    env["AURORA_SANDBOX"] = "1"
    env["HOME"] = str(workspace)
    env["TEMP"] = str(workspace / ".aurora" / "tmp")
    env["TMP"] = env["TEMP"]
    return env


def _read_tail(path: Path, max_chars: int) -> str:
    if not path.is_file():
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        return f"[...início omitido...]\n{text[-max_chars:]}"
    return text


def run_python(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    workspace = ctx.workspace
    max_output = ctx.settings.limits.max_output_chars
    timeout_s = float(args.get("timeout_s") or 30)
    timeout_s = max(1.0, min(timeout_s, 120.0, ctx.settings.limits.max_task_seconds))

    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run_dir = workspace / ".aurora" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    code = args.get("codigo")
    script = args.get("script")
    if bool(code) == bool(script):
        return ToolResult.fail("informe exatamente um: 'codigo' ou 'script'")

    if code:
        script_path = run_dir / "main.py"
        script_path.write_text(str(code), encoding="utf-8", newline="\n")
        origin = "gerado"
    else:
        candidate = Path(str(script)).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            script_path = ctx.policy.resolve(candidate, must_exist=True)
        except (PathDenied, FileNotFoundError) as exc:
            return ToolResult.fail(str(exc))
        if script_path.suffix.lower() != ".py":
            return ToolResult.fail("script precisa ter extensão .py")
        origin = "arquivo"

    if ctx.policy.is_sensitive(script_path):
        return ToolResult.fail("script bloqueado por política de arquivos sensíveis")

    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    (workspace / ".aurora" / "tmp").mkdir(parents=True, exist_ok=True)

    extra_args = args.get("args") or []
    if not isinstance(extra_args, list) or any(not isinstance(item, str) for item in extra_args):
        return ToolResult.fail("'args' deve ser lista de strings")

    command = [sys.executable, "-I", str(script_path), *[str(item) for item in extra_args]]
    started = time.perf_counter()
    timed_out = False
    cancelled = False
    process: subprocess.Popen | None = None

    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        try:
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                env=_sanitized_env(workspace),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            return ToolResult.fail(f"falha ao iniciar Python: {exc}")

        while process.poll() is None:
            if ctx.cancel_event.is_set():
                cancelled = True
                process.kill()
                break
            if time.perf_counter() - started > timeout_s:
                timed_out = True
                process.kill()
                break
            time.sleep(0.05)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    duration_ms = int((time.perf_counter() - started) * 1000)
    code_return = process.returncode if process else None
    stdout = _read_tail(stdout_path, max_output)
    stderr = _read_tail(stderr_path, max_output)

    if cancelled:
        summary = f"execução cancelada após {duration_ms} ms"
    elif timed_out:
        summary = f"timeout de {timeout_s:g}s excedido"
    elif code_return == 0:
        summary = f"sucesso em {duration_ms} ms (exit 0)"
    else:
        summary = f"falhou com exit {code_return} em {duration_ms} ms"

    return ToolResult(
        ok=(code_return == 0 and not timed_out and not cancelled),
        summary=summary,
        data={
            "script": str(script_path),
            "origem": origin,
            "exit_code": code_return,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "duration_ms": duration_ms,
            "stdout": stdout,
            "stderr": stderr,
        },
        error="" if code_return == 0 else (stderr.strip()[-800:] or summary),
    )


def _which_python() -> str | None:
    return shutil.which("python")


def build_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="executar_python",
            description=(
                "Executa código Python novo ou um script .py existente no workspace. "
                "Tem timeout, saída limitada e ambiente sem variáveis de credencial."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "codigo": {"type": "string", "description": "Código Python a executar"},
                    "script": {"type": "string", "description": "Caminho de script .py no workspace"},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 20,
                        "description": "Argumentos posicionais para o script",
                    },
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 120},
                },
                "additionalProperties": False,
            },
            risk=Risk.L1,
            handler=run_python,
            category="execução",
        )
    ]

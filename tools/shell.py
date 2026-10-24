"""Ferramenta Shell restrita para a Aurora no Windows.

Esta primeira versão aceita apenas comandos de consulta explicitamente
permitidos. Não é um sandbox para PowerShell arbitrário.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from aurora.config.permissions import Risk
from aurora.tools.base import ToolContext, ToolResult, ToolSpec

TOOL_NAME = "shell_execute"
TOOL_DESCRIPTION = (
    "Executa comandos de consulta pré-aprovados no PowerShell do Windows, "
    "a partir de um diretório autorizado. Não executa comandos arbitrários."
)

TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "comando": {
            "type": "string",
            "description": (
                "Comando de consulta permitido, como Get-Location, Get-Date, "
                "Get-Process, Get-Service, Get-ChildItem ou python --version."
            ),
            "minLength": 1,
            "maxLength": 300,
        },
        "cwd": {
            "type": "string",
            "description": (
                "Diretório absoluto autorizado ou caminho relativo ao workspace. "
                "Se omitido, usa o workspace."
            ),
            "maxLength": 1000,
        },
        "timeout": {
            "type": "integer",
            "description": "Tempo limite em segundos, entre 1 e 30.",
            "minimum": 1,
            "maximum": 30,
        },
    },
    "required": ["comando"],
    "additionalProperties": False,
}

# Somente comandos de consulta, sem argumentos livres de caminho ou expressão.
_ALLOWLIST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)^Get-Location$"),
    re.compile(r"(?i)^Get-Date$"),
    re.compile(r"(?i)^Get-Process$"),
    re.compile(r"(?i)^Get-Service$"),
    re.compile(r"(?i)^Get-ComputerInfo$"),
    re.compile(r"(?i)^Get-Volume$"),
    re.compile(r"(?i)^Get-ChildItem(?:\s+-(?:Name|Directory|File|Force))*$"),
    re.compile(r"(?i)^hostname(?:\.exe)?$"),
    re.compile(r"(?i)^whoami(?:\.exe)?$"),
    re.compile(r"(?i)^python\s+--version$"),
    re.compile(r"(?i)^py\s+--version$"),
    re.compile(r"(?i)^git\s+--version$"),
    re.compile(r"(?i)^where\.exe\s+(?:python|py|git|powershell|pwsh)$"),
)

# Impede encadeamento, redirecionamento, interpolação e expressões do PowerShell.
_FORBIDDEN_CHARS = (";", "|", "&", "`", "$", "<", ">", "\n", "\r", "{", "}", "(", ")")
_MAX_OUTPUT_CHARS = 12_000
_MAX_COMMAND_CHARS = 300
_MAX_TIMEOUT_SECONDS = 30


def _is_allowed_command(command: str) -> bool:
    command = command.strip()
    if not command or len(command) > _MAX_COMMAND_CHARS:
        return False
    if any(char in command for char in _FORBIDDEN_CHARS):
        return False
    return any(pattern.fullmatch(command) for pattern in _ALLOWLIST_PATTERNS)


def _resolve_cwd(ctx: ToolContext, raw_cwd: str | None) -> Path:
    """Resolve cwd dentro das raízes explicitamente autorizadas."""
    workspace = Path(ctx.workspace).resolve()
    candidate = Path(raw_cwd).expanduser() if raw_cwd else workspace
    if not candidate.is_absolute():
        candidate = workspace / candidate

    resolved = Path(ctx.policy.resolve(str(candidate), must_exist=True))
    if not resolved.is_dir():
        raise ValueError(f"o diretório de trabalho não é uma pasta: {resolved}")
    return resolved


def execute_shell(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Handler compatível com ToolSpec: recebe ToolContext e retorna ToolResult."""
    command = str(args.get("comando") or "").strip()
    if not _is_allowed_command(command):
        return ToolResult.fail(
            "Comando não permitido na versão restrita da Shell.",
            exemplos_permitidos=[
                "Get-Location", "Get-ChildItem", "Get-ChildItem -Name",
                "Get-Date", "Get-Process", "Get-Service", "Get-ComputerInfo",
                "Get-Volume", "python --version", "py --version",
                "git --version", "hostname", "whoami",
            ],
            nota="Comandos arbitrários ainda não estão habilitados.",
        )

    try:
        cwd = _resolve_cwd(ctx, args.get("cwd"))
    except Exception as exc:
        return ToolResult.fail(f"Diretório de trabalho negado ou inválido: {exc}")

    try:
        timeout = int(args.get("timeout", 10))
    except (TypeError, ValueError):
        return ToolResult.fail("timeout deve ser um inteiro entre 1 e 30 segundos.")
    if not 1 <= timeout <= _MAX_TIMEOUT_SECONDS:
        return ToolResult.fail("timeout deve estar entre 1 e 30 segundos.")

    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not powershell:
        return ToolResult.fail("PowerShell não encontrado (powershell.exe ou pwsh.exe).")

    # Codificação UTF-16LE é exigida pelo parâmetro -EncodedCommand do PowerShell.
    # O comando do usuário já foi validado contra uma allowlist estrita.
    wrapped_command = (
        "$OutputEncoding = [Console]::OutputEncoding = "
        "New-Object System.Text.UTF8Encoding; " + command
    )
    encoded_command = base64.b64encode(wrapped_command.encode("utf-16le")).decode("ascii")
    argv = [
        powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
        "-EncodedCommand", encoded_command,
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    started = time.monotonic()

    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
            creationflags=creationflags,
        )
        duration_ms = round((time.monotonic() - started) * 1000)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        truncated = len(stdout) > _MAX_OUTPUT_CHARS or len(stderr) > _MAX_OUTPUT_CHARS
        data = {
            "comando": command,
            "cwd": str(cwd),
            "returncode": completed.returncode,
            "stdout": stdout[:_MAX_OUTPUT_CHARS],
            "stderr": stderr[:_MAX_OUTPUT_CHARS],
            "truncated": truncated,
            "duration_ms": duration_ms,
        }
        if completed.returncode != 0:
            return ToolResult(
                ok=False,
                summary=f"Comando terminou com código {completed.returncode}.",
                data=data,
                error=(stderr.strip() or f"código de saída {completed.returncode}"),
            )
        return ToolResult(ok=True, summary="Comando executado com sucesso.", data=data)

    except subprocess.TimeoutExpired:
        return ToolResult.fail(
            f"Comando excedeu o timeout de {timeout} segundos.",
            comando=command,
            cwd=str(cwd),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    except OSError as exc:
        return ToolResult.fail(
            f"Falha ao executar comando: {exc}", comando=command, cwd=str(cwd)
        )


def build_shell_tool() -> ToolSpec:
    """Constrói a definição que o registry disponibiliza ao agente/modelo."""
    return ToolSpec(
        name=TOOL_NAME,
        description=TOOL_DESCRIPTION,
        parameters=TOOL_SCHEMA,
        risk=Risk.L0,
        handler=execute_shell,
        category="sistema",
    )


# Alias útil se o registro existente usar uma convenção de nome genérica.
handler = execute_shell

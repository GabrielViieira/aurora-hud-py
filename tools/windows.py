"""Informações do sistema, abertura de URL e leitura da auditoria."""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import sys
import webbrowser
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from aurora.config.permissions import Risk
from aurora.tools.base import ToolContext, ToolResult, ToolSpec


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _memory_gb() -> tuple[float, float]:
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        return 0.0, 0.0
    total = status.ullTotalPhys / (1024**3)
    available = status.ullAvailPhys / (1024**3)
    return round(total, 1), round(available, 1)


def system_info(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    usage = shutil.disk_usage(str(ctx.settings.paths.app_dir))
    total_ram, free_ram = _memory_gb() if os.name == "nt" else (0.0, 0.0)
    data = {
        "so": platform.platform(),
        "hostname": platform.node(),
        "python": sys.version.split()[0],
        "executavel": sys.executable,
        "cpu_logico": os.cpu_count(),
        "ram_total_gb": total_ram,
        "ram_livre_gb": free_ram,
        "disco_livre_gb": round(usage.free / (1024**3), 1),
        "workspace": str(ctx.settings.paths.workspace_dir),
        "raizes_autorizadas": [str(root) for root in ctx.policy.allowed_roots],
        "hora_local": datetime.now().isoformat(timespec="seconds"),
    }
    return ToolResult(
        ok=True,
        summary=(
            f"{platform.system()} {platform.release()} • Python {data['python']} • "
            f"{data['disco_livre_gb']} GB livres"
        ),
        data=data,
    )


def open_url(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    url = str(args.get("url", "")).strip()
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return ToolResult.fail("apenas URLs http/https são permitidas")
    if not parts.netloc:
        return ToolResult.fail("URL sem domínio válido")
    try:
        opened = webbrowser.open(url)
    except Exception as exc:  # webbrowser pode falhar por falta de navegador
        return ToolResult.fail(f"falha ao abrir navegador: {exc}")
    if not opened:
        return ToolResult.fail("nenhum navegador disponível para abrir a URL")
    return ToolResult(ok=True, summary=f"URL aberta no navegador padrão: {parts.netloc}", data={"url": url})


def read_audit(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    limit = min(int(args.get("limite") or 20), 200)
    events = ctx.audit.tail(limit)
    return ToolResult(
        ok=True,
        summary=f"{len(events)} evento(s) de auditoria",
        data={"eventos": events},
    )


def build_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="info_sistema",
            description="Mostra SO, Python, CPU, memória, disco e raízes autorizadas.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            risk=Risk.L0,
            handler=system_info,
            category="sistema",
        ),
        ToolSpec(
            name="abrir_url",
            description="Abre uma URL http/https no navegador padrão do Windows.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "minLength": 8}},
                "required": ["url"],
                "additionalProperties": False,
            },
            risk=Risk.L2,
            handler=open_url,
            category="sistema",
        ),
        ToolSpec(
            name="ler_auditoria",
            description="Lê os últimos eventos da trilha de auditoria da Aurora.",
            parameters={
                "type": "object",
                "properties": {"limite": {"type": "integer", "minimum": 1, "maximum": 200}},
                "additionalProperties": False,
            },
            risk=Risk.L0,
            handler=read_audit,
            category="sistema",
        ),
    ]

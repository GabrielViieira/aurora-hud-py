"""Ferramentas de arquivos restritas às raízes autorizadas.

Não existe ferramenta de exclusão na Fase 1 — remoção exige autorização
explícita do usuário e um fluxo próprio (quarentena com manifesto).
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from aurora.config.permissions import PathDenied, Risk
from aurora.tools.base import ToolContext, ToolResult, ToolSpec

_SKIP_DIRS = {".git", ".aurora", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}
_MAX_SCAN_BYTES = 2_000_000


def _resolve(ctx: ToolContext, raw: str, *, for_write: bool = False, must_exist: bool = False) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = ctx.workspace / candidate
    return ctx.policy.resolve(candidate, for_write=for_write, must_exist=must_exist)


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"


def _entry(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
        size = stat.st_size
    except OSError:
        modified, size = "", 0
    return {
        "nome": path.name,
        "caminho": str(path),
        "tipo": "diretorio" if path.is_dir() else "arquivo",
        "bytes": size,
        "modificado": modified,
    }


def list_dir(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    base = _resolve(ctx, str(args.get("caminho") or ctx.workspace), must_exist=True)
    if not base.is_dir():
        return ToolResult.fail(f"não é um diretório: {base}")
    recursive = bool(args.get("recursivo", False))
    limit = min(int(args.get("max_entradas") or 200), 500)
    denied = 0
    entries: list[dict[str, Any]] = []

    def safe_add(path: Path) -> None:
        nonlocal denied
        try:
            if ctx.policy.is_sensitive(path):
                denied += 1
                return
            ctx.policy.resolve(path)
        except (PathDenied, OSError):
            denied += 1
            return
        entries.append(_entry(path))

    if recursive:
        for path in sorted(base.rglob("*")):
            if len(entries) >= limit:
                break
            if any(part in _SKIP_DIRS for part in path.relative_to(base).parts):
                continue
            if not path.is_dir():
                safe_add(path)
    else:
        for path in sorted(base.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if len(entries) >= limit:
                break
            safe_add(path)

    summary = f"{len(entries)} entrada(s) em {base}"
    if denied:
        summary += f"; {denied} ocultada(s) por política"
    return ToolResult(ok=True, summary=summary, data={"caminho": str(base), "entradas": entries})


def read_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve(ctx, str(args["caminho"]), must_exist=True)
    if path.is_dir():
        return ToolResult.fail(f"caminho é diretório, não arquivo: {path}")

    max_lines = min(int(args.get("max_linhas") or 120), ctx.settings.limits.max_read_lines)
    start_line = max(1, int(args.get("linha_inicial") or 1))
    try:
        with path.open("rb") as handle:
            head = handle.read(4096)
        if b"\x00" in head:
            return ToolResult.fail(f"arquivo binário não é suportado: {path.name}")
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolResult.fail(f"falha ao ler {path}: {exc}")

    lines = text.splitlines()
    total = len(lines)
    selected = lines[start_line - 1 : start_line - 1 + max_lines]
    numbered = [
        {"linha": start_line + index, "texto": content}
        for index, content in enumerate(selected)
    ]
    truncated = start_line - 1 + len(selected) < total
    summary = f"{len(selected)} de {total} linha(s) de {path.name}"
    if truncated:
        summary += f" (a partir da linha {start_line})"
    return ToolResult(
        ok=True,
        summary=summary,
        data={
            "caminho": str(path),
            "total_linhas": total,
            "linha_inicial": start_line,
            "linhas": numbered,
            "truncado": truncated,
        },
    )


def write_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve(ctx, str(args["caminho"]), for_write=True)
    content = str(args.get("conteudo", ""))
    overwrite = bool(args.get("sobrescrever", False))
    if path.exists() and not overwrite:
        return ToolResult.fail(
            f"arquivo já existe: {path}. Use sobrescrever=true ou editar_arquivo."
        )
    if path.exists() and path.is_dir():
        return ToolResult.fail(f"caminho é diretório: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    except OSError as exc:
        return ToolResult.fail(f"falha ao escrever {path}: {exc}")

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return ToolResult(
        ok=True,
        summary=f"escrito {path} ({len(content)} chars, sha256:{digest})",
        data={"caminho": str(path), "bytes": len(content.encode("utf-8")), "sha256": digest},
    )


def edit_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = _resolve(ctx, str(args["caminho"]), must_exist=True)
    old = str(args.get("antigo", ""))
    new = str(args.get("novo", ""))
    if not old:
        return ToolResult.fail("'antigo' não pode ser vazio")
    if old == new:
        return ToolResult.fail("'antigo' e 'novo' são iguais; nada a alterar")

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ToolResult.fail(f"falha ao ler {path}: {exc}")

    occurrences = text.count(old)
    if occurrences == 0:
        return ToolResult.fail("trecho não encontrado no arquivo")
    replace_all = bool(args.get("todas", False))
    if occurrences > 1 and not replace_all:
        return ToolResult.fail(
            f"trecho aparece {occurrences} vezes; use 'todas=true' ou inclua mais contexto"
        )

    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    try:
        path.write_text(updated, encoding="utf-8", newline="\n")
    except OSError as exc:
        return ToolResult.fail(f"falha ao gravar {path}: {exc}")

    changed = occurrences if replace_all else 1
    return ToolResult(
        ok=True,
        summary=f"{changed} substituição(ões) em {path.name}",
        data={"caminho": str(path), "substituicoes": changed, "bytes": len(updated.encode("utf-8"))},
    )


def search_text(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pattern = str(args.get("padrao", ""))
    if not pattern:
        return ToolResult.fail("'padrao' é obrigatório")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return ToolResult.fail(f"expressão regular inválida: {exc}")

    base = _resolve(ctx, str(args.get("caminho") or ctx.workspace), must_exist=True)
    if not base.is_dir():
        return ToolResult.fail(f"caminho precisa ser diretório: {base}")
    glob = str(args.get("glob") or "*")
    limit = min(int(args.get("max_resultados") or 50), ctx.settings.limits.max_search_results)

    results: list[dict[str, Any]] = []
    scanned = 0
    for path in base.rglob("*"):
        if len(results) >= limit:
            break
        if not path.is_file() or any(part in _SKIP_DIRS for part in path.relative_to(base).parts):
            continue
        if not fnmatch.fnmatch(path.name, glob):
            continue
        try:
            if path.stat().st_size > _MAX_SCAN_BYTES:
                continue
            ctx.policy.resolve(path)
            chunk = path.read_bytes()
            if b"\x00" in chunk[:4096]:
                continue
        except (OSError, PathDenied):
            continue
        scanned += 1
        text = chunk.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            match = regex.search(line)
            if match:
                results.append(
                    {
                        "caminho": str(path),
                        "linha": number,
                        "texto": line.strip()[:300],
                        "coluna": match.start() + 1,
                    }
                )
                if len(results) >= limit:
                    break

    summary = f"{len(results)} ocorrência(s) em {scanned} arquivo(s) varrido(s)"
    if not results:
        summary = f"nenhuma ocorrência de /{pattern}/ em {scanned} arquivo(s)"
    return ToolResult(ok=True, summary=summary, data={"resultados": results, "padrao": pattern})


def build_tools() -> list[ToolSpec]:
    path_property = {"type": "string", "description": "Caminho absoluto ou relativo ao workspace"}
    return [
        ToolSpec(
            name="listar_diretorio",
            description="Lista arquivos e diretórios dentro do workspace autorizado.",
            parameters={
                "type": "object",
                "properties": {
                    "caminho": {**path_property, "description": "Diretório a listar (padrão: workspace)"},
                    "recursivo": {"type": "boolean", "description": "Descer em subdiretórios"},
                    "max_entradas": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "additionalProperties": False,
            },
            risk=Risk.L0,
            handler=list_dir,
            category="arquivos",
        ),
        ToolSpec(
            name="ler_arquivo",
            description="Lê um arquivo de texto por trechos, com número de linha.",
            parameters={
                "type": "object",
                "properties": {
                    "caminho": path_property,
                    "linha_inicial": {"type": "integer", "minimum": 1},
                    "max_linhas": {"type": "integer", "minimum": 1, "maximum": 400},
                },
                "required": ["caminho"],
                "additionalProperties": False,
            },
            risk=Risk.L0,
            handler=read_file,
            category="arquivos",
        ),
        ToolSpec(
            name="escrever_arquivo",
            description="Cria ou sobrescreve um arquivo de texto dentro do workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "caminho": path_property,
                    "conteudo": {"type": "string"},
                    "sobrescrever": {"type": "boolean", "description": "Permitir sobrescrever existente"},
                },
                "required": ["caminho", "conteudo"],
                "additionalProperties": False,
            },
            risk=Risk.L1,
            handler=write_file,
            category="arquivos",
        ),
        ToolSpec(
            name="editar_arquivo",
            description="Substitui um trecho exato de um arquivo existente.",
            parameters={
                "type": "object",
                "properties": {
                    "caminho": path_property,
                    "antigo": {"type": "string", "minLength": 1},
                    "novo": {"type": "string"},
                    "todas": {"type": "boolean", "description": "Substituir todas as ocorrências"},
                },
                "required": ["caminho", "antigo", "novo"],
                "additionalProperties": False,
            },
            risk=Risk.L1,
            handler=edit_file,
            category="arquivos",
        ),
        ToolSpec(
            name="buscar_texto",
            description="Busca expressão regular em arquivos do workspace, retornando arquivo e linha.",
            parameters={
                "type": "object",
                "properties": {
                    "padrao": {"type": "string", "minLength": 1},
                    "caminho": {**path_property, "description": "Diretório base (padrão: workspace)"},
                    "glob": {"type": "string", "description": "Filtro de nome, ex.: *.py"},
                    "max_resultados": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["padrao"],
                "additionalProperties": False,
            },
            risk=Risk.L0,
            handler=search_text,
            category="arquivos",
        ),
    ]

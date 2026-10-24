"""Registro tipado de ferramentas."""

from __future__ import annotations

import time
from typing import Any, Iterable

from aurora.observability import get_logger
from aurora.tools.base import ToolContext, ToolResult, ToolSpec, validate_arguments

logger = get_logger(__name__)


class ToolNotFound(KeyError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"ferramenta duplicada: {spec.name}")
        self._tools[spec.name] = spec

    def register_many(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFound(name) from exc

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, enabled: tuple[str, ...] = ()) -> list[ToolSpec]:
        if not enabled:
            return [self._tools[name] for name in sorted(self._tools)]
        return [self._tools[name] for name in sorted(self._tools) if name in enabled]

    def schemas(self, enabled: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        return [spec.to_openai() for spec in self.specs(enabled)]

    def catalog_text(self, enabled: tuple[str, ...] = ()) -> str:
        lines = []
        for spec in self.specs(enabled):
            lines.append(f"- {spec.name} ({spec.risk.label}): {spec.description}")
        return "\n".join(lines) if lines else "Nenhuma ferramenta habilitada."

    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            spec = self.get(name)
        except ToolNotFound:
            return ToolResult.fail(f"ferramenta desconhecida: {name}")

        errors = validate_arguments(spec.parameters, args)
        if errors:
            return ToolResult.fail("argumentos inválidos: " + "; ".join(errors), errors=errors)

        started = time.perf_counter()
        try:
            result = spec.handler(ctx, dict(args))
        except Exception as exc:  # ferramenta nunca derruba o agente
            logger.exception("falha na ferramenta %s", name)
            return ToolResult.fail(f"{type(exc).__name__}: {exc}")
        finally:
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.info(
                "tool executada",
                extra={"aurora": {"tool": name, "duration_ms": elapsed, "task_id": ctx.task_id}},
            )
        return result

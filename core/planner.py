"""Planejamento de tarefas com validação contra o registro de ferramentas."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from aurora.llm.prompts import PLANNER_PROMPT
from aurora.llm.provider import ChatMessage, LLMRequest, ProviderError
from aurora.llm.token_manager import truncate_to_tokens
from aurora.observability import get_logger
from aurora.tools.base import validate_arguments
from aurora.tools.registry import ToolRegistry

logger = get_logger(__name__)


class PlannerError(RuntimeError):
    pass


@dataclass
class PlanStep:
    index: int
    tool: str
    args: dict[str, Any]
    why: str = ""


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    notes: str = ""
    raw: str = ""


class Planner:
    def __init__(
        self,
        provider,
        registry: ToolRegistry,
        *,
        model: str = "auto",
        max_steps: int = 8,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.model = model
        self.max_steps = max_steps

    def create_plan(self, goal: str, enabled_tools: tuple[str, ...] = ()) -> Plan:
        catalog = self.registry.catalog_text(enabled_tools)
        system = (
            f"{PLANNER_PROMPT}\n\nFerramentas disponíveis:\n{catalog}\n\n"
            f"Máximo de passos: {self.max_steps}."
        )
        request = LLMRequest(
            messages=[
                ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=truncate_to_tokens(goal, 1500)),
            ],
            model=self.model,
            temperature=0.1,
            max_tokens=900,
        )
        response = self.provider.chat(request)
        payload = _extract_json(response.text)
        if payload is None:
            raise PlannerError(
                "o modelo não devolveu JSON de plano válido; reformule o pedido ou use o modo conversa"
            )

        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list):
            raise PlannerError("plano sem lista 'steps'")

        steps: list[PlanStep] = []
        notes = str(payload.get("notes", ""))
        for item in raw_steps:
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool", "")).strip()
            if not tool or not self.registry.has(tool):
                notes += f" [passo ignorado: ferramenta desconhecida {tool!r}]"
                continue
            if enabled_tools and tool not in enabled_tools:
                notes += f" [passo ignorado: ferramenta desabilitada {tool}]"
                continue
            args = item.get("args") or {}
            if not isinstance(args, dict):
                notes += f" [passo ignorado: args inválidos em {tool}]"
                continue
            errors = validate_arguments(self.registry.get(tool).parameters, args)
            if errors:
                notes += f" [passo ignorado: {tool}: {'; '.join(errors)}]"
                continue
            steps.append(
                PlanStep(
                    index=len(steps) + 1,
                    tool=tool,
                    args=dict(args),
                    why=str(item.get("why", "")),
                )
            )
            if len(steps) >= self.max_steps:
                break

        return Plan(goal=goal, steps=steps, notes=notes.strip(), raw=response.text)


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None

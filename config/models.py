"""Catálogo local de modelos/perfis.

O gateway é quem sabe quais modelos existem. Aqui guardamos apenas o que o
núcleo precisa: apelido de papel -> nome de modelo e uma janela de contexto
conservadora quando o provedor não informa o limite.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProfile:
    role: str
    model: str
    context_window: int
    supports_tools: bool = True
    notes: str = ""


def default_profiles(model: str, utility: str, planner: str, coder: str, context: int) -> dict[str, ModelProfile]:
    return {
        "chat": ModelProfile("chat", model, context),
        "utility": ModelProfile("utility", utility, min(context, 16000), notes="resumos e títulos"),
        "planner": ModelProfile("planner", planner, context, notes="planejamento"),
        "coder": ModelProfile("coder", coder, context, notes="código"),
    }


__all__ = ["ModelProfile", "default_profiles"]

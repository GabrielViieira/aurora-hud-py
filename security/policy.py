"""Decisão de autorização por risco (menor privilégio)."""

from __future__ import annotations

from dataclasses import dataclass

from aurora.config.permissions import Risk
from aurora.observability import redact_text


@dataclass(frozen=True)
class Decision:
    allowed: bool
    risk: Risk
    requires_approval: bool
    reason: str


class SecurityPolicy:
    def __init__(
        self,
        *,
        auto_approve_max_risk: Risk = Risk.L1,
        enabled_tools: tuple[str, ...] = (),
        allow_l3: bool = False,
    ) -> None:
        self.auto_approve_max_risk = auto_approve_max_risk
        self.enabled_tools = tuple(enabled_tools)
        self.allow_l3 = allow_l3

    def tool_enabled(self, name: str) -> bool:
        return not self.enabled_tools or name in self.enabled_tools

    def evaluate(self, name: str, risk: Risk) -> Decision:
        if not self.tool_enabled(name):
            return Decision(False, risk, False, f"ferramenta '{name}' desabilitada na configuração")

        # L3 nunca é automático, mesmo que a configuração tente afrouxar.
        if risk >= Risk.L3:
            if not self.allow_l3:
                return Decision(
                    True,
                    risk,
                    True,
                    "risco L3: exige confirmação explícita e allow_l3 habilitado",
                )
            return Decision(True, risk, True, "risco L3: confirmação explícita obrigatória")

        requires = risk > self.auto_approve_max_risk
        reason = (
            f"risco {risk.label} acima do limite automático "
            f"({self.auto_approve_max_risk.label})"
            if requires
            else f"risco {risk.label} dentro do limite automático"
        )
        return Decision(True, risk, requires, reason)

    @staticmethod
    def describe_call(name: str, args: dict, risk: Risk) -> str:
        parts = ", ".join(f"{key}={_short(value)}" for key, value in args.items())
        return f"{name}({parts}) [{risk.label}]"


def _short(value: object, limit: int = 120) -> str:
    text = redact_text(str(value)).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"

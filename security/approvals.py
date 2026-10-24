"""Portões de aprovação: console, automático e roteirizado (testes)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from aurora.config.permissions import Risk
from aurora.security.policy import SecurityPolicy


@dataclass(frozen=True)
class ApprovalRequest:
    task_id: str
    tool: str
    args: dict
    risk: Risk
    reason: str

    def render(self) -> str:
        return (
            f"Aprovação necessária ({self.risk.label})\n"
            f"  ferramenta: {self.tool}\n"
            f"  chamada: {SecurityPolicy.describe_call(self.tool, self.args, self.risk)}\n"
            f"  motivo: {self.reason}"
        )


@runtime_checkable
class Approver(Protocol):
    def approve(self, request: ApprovalRequest) -> bool: ...


class ConsoleApprover:
    """Pergunta no terminal. Nega por padrão (Enter vazio = não)."""

    def __init__(self, ask: Callable[[str], str] | None = None) -> None:
        self._ask = ask or input

    def approve(self, request: ApprovalRequest) -> bool:
        print()
        print(request.render())
        try:
            answer = self._ask("Autorizar? [s/N] ").strip().lower()
        except EOFError:
            return False
        return answer in {"s", "sim", "y", "yes"}


class AutoApprove:
    def approve(self, request: ApprovalRequest) -> bool:
        return True


class AutoDeny:
    def approve(self, request: ApprovalRequest) -> bool:
        return False


class ScriptedApprover:
    """Respostas pré-definidas em ordem; útil para testes e automação."""

    def __init__(self, answers: list[bool]) -> None:
        self.answers = list(answers)
        self.requests: list[ApprovalRequest] = []

    def approve(self, request: ApprovalRequest) -> bool:
        self.requests.append(request)
        return self.answers.pop(0) if self.answers else False

"""Executor de planos com aprovação, verificação e checkpoints.

Diferente do agente conversacional, o orquestrador executa uma sequência
determinística de ferramentas. Cada passo é verificado pelo resultado real da
ferramenta; falhas recuperáveis têm número limitado de tentativas.
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Any

from aurora.config.permissions import Risk
from aurora.config.settings import Settings
from aurora.core.events import EventBus
from aurora.core.planner import Plan, PlanStep
from aurora.core.task_state import Step, Task, TaskStatus, TaskStore
from aurora.memory.long_term import LongTermMemory
from aurora.observability import get_logger
from aurora.security.approvals import ApprovalRequest, Approver
from aurora.security.audit import AuditLog, summarize_args
from aurora.security.policy import SecurityPolicy
from aurora.tools.base import ToolContext, ToolResult
from aurora.tools.registry import ToolRegistry

logger = get_logger(__name__)


class OrchestratorError(RuntimeError):
    pass


@dataclass
class Orchestrator:
    settings: Settings
    registry: ToolRegistry
    security: SecurityPolicy
    approver: Approver
    audit: AuditLog
    tasks: TaskStore
    memory: LongTermMemory
    events: EventBus
    policy: Any  # PermissionPolicy

    def run(
        self,
        goal: str,
        plan: Plan,
        *,
        session_id: str = "",
        cancel_event: threading.Event | None = None,
    ) -> Task:
        task = Task.create(goal, session_id=session_id)
        task.steps = [
            Step(
                index=index,
                description=item.why or f"{item.tool}",
                tool=item.tool,
                args=item.args,
            )
            for index, item in enumerate(plan.steps, start=1)
        ]
        task.transition(TaskStatus.PLANNING)
        self.tasks.save(task)
        self.events.emit("task.created", task_id=task.id, goal=goal, steps=len(task.steps))
        self.audit.record(
            "task.created",
            task_id=task.id,
            goal=goal,
            steps=[{"tool": step.tool, "args": summarize_args(step.args)} for step in task.steps],
            plan_notes=plan.notes,
        )

        if not task.steps:
            task.transition(TaskStatus.FAILED)
            task.error = plan.notes or "plano sem passos executáveis"
            self.tasks.save(task)
            return task

        ctx = ToolContext(
            settings=self.settings,
            policy=self.policy,
            audit=self.audit,
            task_id=task.id,
            cancel_event=cancel_event or threading.Event(),
        )
        started = time.monotonic()
        task.transition(TaskStatus.RUNNING)
        self._execute_pending(task, plan, ctx, started)
        return task

    def resume(
        self,
        task_id: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> Task:
        task = self.tasks.load(task_id)
        if task is None:
            raise OrchestratorError(f"tarefa não encontrada: {task_id}")
        if task.status in {TaskStatus.COMPLETED}:
            return task
        if task.status is TaskStatus.CANCELLED:
            raise OrchestratorError("tarefa foi cancelada e não é retomável")

        plan = Plan(
            goal=task.goal,
            steps=[
                PlanStep(index=step.index, tool=step.tool, args=step.args, why=step.description)
                for step in task.steps
            ],
        )
        if task.status in {TaskStatus.PLANNING, TaskStatus.FAILED}:
            task.transition(TaskStatus.RUNNING)
        elif task.status is TaskStatus.WAITING_APPROVAL:
            task.transition(TaskStatus.RUNNING)
        self.tasks.save(task)
        self.audit.record("task.resumed", task_id=task.id, status=task.status.value)

        ctx = ToolContext(
            settings=self.settings,
            policy=self.policy,
            audit=self.audit,
            task_id=task.id,
            cancel_event=cancel_event or threading.Event(),
        )
        self._execute_pending(task, plan, ctx, time.monotonic())
        return task

    # ------------------------------------------------------------------ interno
    def _execute_pending(
        self,
        task: Task,
        plan: Plan,
        ctx: ToolContext,
        started: float,
    ) -> None:
        for step in task.steps:
            if step.status == "concluido":
                continue
            if ctx.cancel_event.is_set():
                self._cancel(task)
                return
            if time.monotonic() - started > self.settings.limits.max_task_seconds:
                task.error = "limite de tempo da tarefa excedido"
                task.transition(TaskStatus.FAILED)
                self.tasks.save(task)
                self.events.emit("task.failed", task_id=task.id, reason=task.error)
                return

            spec = self.registry.get(step.tool)
            decision = self.security.evaluate(spec.name, spec.risk)
            if not decision.allowed:
                step.status = "bloqueado"
                step.summary = decision.reason
                task.error = decision.reason
                task.transition(TaskStatus.FAILED)
                self.tasks.save(task)
                self.audit.record("tool.blocked", task_id=task.id, tool=step.tool, reason=decision.reason)
                return

            if decision.requires_approval:
                task.transition(TaskStatus.WAITING_APPROVAL)
                self.tasks.save(task)
                self.events.emit("task.waiting_approval", task_id=task.id, tool=step.tool)
                request = ApprovalRequest(
                    task_id=task.id,
                    tool=step.tool,
                    args=step.args,
                    risk=spec.risk,
                    reason=decision.reason,
                )
                self.audit.record(
                    "approval.requested",
                    task_id=task.id,
                    tool=step.tool,
                    risk=spec.risk.label,
                    args=summarize_args(step.args),
                )
                approved = self.approver.approve(request)
                self.audit.record(
                    "approval.decided", task_id=task.id, tool=step.tool, approved=approved
                )
                if not approved:
                    step.status = "negado"
                    step.summary = "não autorizado pelo usuário"
                    task.error = f"ação não autorizada: {step.tool}"
                    task.transition(TaskStatus.CANCELLED)
                    self.tasks.save(task)
                    self.events.emit("task.cancelled", task_id=task.id, reason=task.error)
                    return
                task.transition(TaskStatus.RUNNING)
                self.tasks.save(task)

            self._execute_step(task, step, ctx)

            if step.ok:
                continue
            if step.attempts <= self.settings.limits.max_retries_per_step and _retryable(step):
                self.audit.record("step.retry", task_id=task.id, tool=step.tool, attempt=step.attempts)
                step.status = "pendente"
                self._execute_step(task, step, ctx)
                if step.ok:
                    continue

            task.error = f"passo {step.index} falhou: {step.summary or step.tool}"
            task.transition(TaskStatus.FAILED)
            self.tasks.save(task)
            self.events.emit("task.failed", task_id=task.id, reason=task.error)
            self.audit.record("task.failed", task_id=task.id, reason=task.error)
            return

        if ctx.cancel_event.is_set():
            self._cancel(task)
            return

        task.transition(TaskStatus.COMPLETED)
        self.tasks.save(task)
        self.events.emit("task.completed", task_id=task.id, goal=task.goal)
        self.audit.record("task.completed", task_id=task.id, goal=task.goal)
        self._remember_episode(task, plan)

    def _execute_step(self, task: Task, step: Step, ctx: ToolContext) -> None:
        step.attempts += 1
        step.status = "executando"
        self.tasks.save(task)
        self.events.emit("step.started", task_id=task.id, step=step.index, tool=step.tool)
        if task.status is TaskStatus.VERIFYING:
            task.transition(TaskStatus.RUNNING)

        started = time.perf_counter()
        result: ToolResult = self.registry.execute(step.tool, step.args, ctx)
        duration_ms = int((time.perf_counter() - started) * 1000)

        task.transition(TaskStatus.VERIFYING)
        step.ok = bool(result.ok) and bool(result.summary)
        step.status = "concluido" if step.ok else "falhou"
        step.summary = result.summary or result.error
        self.tasks.save(task)
        self.audit.record(
            "tool.executed",
            task_id=task.id,
            tool=step.tool,
            risk=self.registry.get(step.tool).risk.label,
            args=summarize_args(step.args),
            ok=step.ok,
            summary=result.summary[:500],
            duration_ms=duration_ms,
        )
        self.events.emit(
            "step.finished",
            task_id=task.id,
            step=step.index,
            tool=step.tool,
            ok=step.ok,
            summary=result.summary[:200],
        )
        if step.ok:
            task.transition(TaskStatus.RUNNING)

    def _cancel(self, task: Task) -> None:
        task.transition(TaskStatus.CANCELLED)
        task.error = task.error or "cancelada pelo usuário"
        self.tasks.save(task)
        self.events.emit("task.cancelled", task_id=task.id, reason=task.error)
        self.audit.record("task.cancelled", task_id=task.id, reason=task.error)

    def _remember_episode(self, task: Task, plan: Plan) -> None:
        lines = [f"Tarefa: {task.goal}"]
        for step in task.steps:
            lines.append(f"- {step.tool}: {step.summary[:160]}")
        content = "\n".join(lines)
        self.memory.remember(
            content,
            layer="episodic",
            kind="task_result",
            tags=("tarefa", "concluida"),
            importance=0.5,
            source="orchestrator",
            session_id=task.session_id or None,
        )


def _retryable(step: Step) -> bool:
    text = (step.summary or "").lower()
    fatal_markers = ("não autorizado", "não encontrado", "argumentos inválidos", "desconhecida")
    return not any(marker in text for marker in fatal_markers)

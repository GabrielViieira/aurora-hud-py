"""Agente conversacional com ferramentas, aprovação e memória."""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aurora.config.settings import Settings
from aurora.config.permissions import PermissionPolicy
from aurora.core.context_manager import ContextManager, ContextReport, Summarizer
from aurora.core.events import EventBus
from aurora.core.metrics import Metrics
from aurora.core.task_state import Task, TaskStatus, TaskStore
from aurora.llm.prompts import system_prompt
from aurora.llm.provider import ChatMessage, LLMRequest, LLMResponse, ProviderError, ToolCall, Usage
from aurora.llm.model_router import ModelRouter
from aurora.memory.conversation_store import ConversationStore
from aurora.memory.long_term import LongTermMemory
from aurora.observability import get_logger
from aurora.security.approvals import ApprovalRequest, Approver
from aurora.security.audit import AuditLog, summarize_args
from aurora.security.policy import SecurityPolicy
from aurora.tools.base import ToolContext, ToolResult
from aurora.tools.registry import ToolRegistry

logger = get_logger(__name__)

_MEMORY_REQUEST_RE = re.compile(
    r"(?i)\b(lembre(?:-se)?|guarde|anote|não esqueça|prefiro|minha preferência|sempre use|nunca use)\b"
)
_DO_NOT_REMEMBER_RE = re.compile(r"(?i)\b(não (?:registre|guarde|salve|anote)|esqueça isso)\b")


@dataclass
class AgentReply:
    text: str
    task_id: str = ""
    status: str = TaskStatus.COMPLETED.value
    model: str = ""
    tool_calls: int = 0
    tools_used: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    context: ContextReport | None = None


class AuroraAgent:
    def __init__(
        self,
        *,
        settings: Settings,
        provider,
        registry: ToolRegistry,
        router: ModelRouter,
        memory: LongTermMemory,
        conversations: ConversationStore,
        tasks: TaskStore,
        audit: AuditLog,
        security: SecurityPolicy,
        approver: Approver,
        events: EventBus,
        metrics: Metrics,
        policy: PermissionPolicy,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.registry = registry
        self.router = router
        self.memory = memory
        self.conversations = conversations
        self.tasks = tasks
        self.audit = audit
        self.security = security
        self.approver = approver
        self.events = events
        self.metrics = metrics
        self.policy = policy
        self.context_manager = ContextManager(settings)
        self.session_id = ""
        self.cancel_event = threading.Event()
        self._last_context: ContextReport | None = None

    # ------------------------------------------------------------------ sessão
    def new_session(self, title: str = "") -> str:
        if self.session_id:
            self.conversations.end_session(self.session_id)
        self.session_id = self.conversations.create_session(title)
        self.events.emit("session.created", session_id=self.session_id)
        return self.session_id

    def ensure_session(self) -> str:
        if not self.session_id:
            self.new_session()
        return self.session_id

    def cancel(self) -> None:
        self.cancel_event.set()
        self.events.emit("turn.cancel_requested", session_id=self.session_id)

    # ------------------------------------------------------------------ turno
    def handle(self, user_text: str) -> AgentReply:
        text = (user_text or "").strip()
        if not text:
            return AgentReply(text="", status=TaskStatus.CANCELLED.value)

        session_id = self.ensure_session()
        self.cancel_event.clear()
        started = time.monotonic()

        self.conversations.add_message(session_id, "user", text)
        task = Task.create(text, session_id=session_id)
        task.transition(TaskStatus.RUNNING)
        self.tasks.save(task)
        self.events.emit("turn.started", task_id=task.id, session_id=session_id)
        self.audit.record("turn.started", task_id=task.id, message=text[:500])

        messages, report = self._build_context(text, session_id)
        self._last_context = report
        tools = self.registry.schemas(self.settings.enabled_tools)
        usage = Usage()
        tools_used: list[str] = []
        final_text = ""
        model_used = ""
        tool_call_count = 0
        failures: dict[str, int] = {}
        ctx = ToolContext(
            settings=self.settings,
            policy=self.policy,
            audit=self.audit,
            task_id=task.id,
            cancel_event=self.cancel_event,
        )

        try:
            iterations = 0
            while True:
                if self.cancel_event.is_set():
                    final_text = "Tarefa cancelada a pedido do usuário."
                    task.transition(TaskStatus.CANCELLED)
                    break
                if time.monotonic() - started > self.settings.limits.max_task_seconds:
                    final_text = (
                        "Parei porque o limite de tempo desta tarefa foi atingido. "
                        "O que já foi executado está registrado na auditoria."
                    )
                    task.error = "limite de tempo do turno excedido"
                    task.transition(TaskStatus.FAILED)
                    break
                if iterations >= self.settings.limits.max_tool_iterations:
                    final_text = (
                        "Atingi o limite de passos com ferramentas sem concluir. "
                        f"Ferramentas usadas: {', '.join(tools_used) or 'nenhuma'}. "
                        "Veja a auditoria para o que já foi executado."
                    )
                    task.error = "limite de iterações de ferramentas"
                    task.transition(TaskStatus.FAILED)
                    break

                iterations += 1
                request = LLMRequest(
                    messages=messages,
                    tools=tools,
                    model=self.router.model_for("chat"),
                    temperature=self.settings.llm.temperature,
                    max_tokens=self.settings.llm.max_output_tokens,
                )
                response: LLMResponse = self.provider.chat(request)
                model_used = response.model or model_used
                _accumulate_usage(usage, response.usage)

                if not response.tool_calls:
                    final_text = response.text.strip() or "(o modelo não devolveu texto)"
                    task.transition(TaskStatus.COMPLETED)
                    break

                assistant = ChatMessage(
                    role="assistant",
                    content=response.text,
                    tool_calls=list(response.tool_calls),
                )
                messages.append(assistant)

                vision_attachments: list[tuple[str, str]] = []
                desktop_tool_names = {"capturar_tela", "controlar_mouse", "controlar_teclado"}
                desktop_calls = [call.name for call in response.tool_calls if call.name in desktop_tool_names]
                preferred_desktop_tool = ("capturar_tela" if "capturar_tela" in desktop_calls else (desktop_calls[0] if desktop_calls else ""))
                desktop_batch_requires_serial = len(desktop_calls) > 1
                for call in response.tool_calls:
                    tool_call_count += 1
                    if (call.name in desktop_tool_names and desktop_batch_requires_serial
                            and call.name != preferred_desktop_tool):
                        result = ToolResult(
                            ok=True,
                            summary=("AÇÃO NÃO EXECUTADA: etapa adiada para manter o ciclo visual "
                                     "capturar/analisar/agir/verificar em sequência. Execute-a em uma chamada posterior."),
                            data={"adiada": True},
                        )
                        fatal = False
                    else:
                        result, fatal = self._run_tool(call, task, ctx)
                    if call.name == "capturar_tela" and result.ok and not result.data.get("adiada"):
                        image_url = result.data.pop("data_url", "")
                        if isinstance(image_url, str) and image_url.startswith("data:image/"):
                            vision_attachments.append((image_url, result.summary))
                    tools_used.append(call.name)
                    messages.append(
                        ChatMessage(
                            role="tool",
                            content=result.to_content(self.settings.limits.max_output_chars),
                            tool_call_id=call.id,
                            name=call.name,
                        )
                    )
                    key = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)}"
                    if not result.ok:
                        failures[key] = failures.get(key, 0) + 1
                    if fatal or failures.get(key, 0) >= 2:
                        final_text = (
                            f"Não consegui concluir usando `{call.name}`. "
                            f"Motivo: {result.error or result.summary}"
                        )
                        task.error = final_text
                        task.transition(TaskStatus.FAILED)
                        break
                for image_url, caption in vision_attachments:
                    messages.append(
                        ChatMessage(
                            role="user",
                            content=[
                                {"type": "text", "text": "Captura visual da área de trabalho para a tarefa atual. " + caption + " Analise a imagem antes de decidir o próximo passo."},
                                {"type": "image_url", "image_url": {"url": image_url}},
                            ],
                        )
                    )
                if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
                    break

        except ProviderError as exc:
            logger.warning("falha do provedor", extra={"aurora": {"kind": exc.kind}})
            final_text = (
                f"Não consegui falar com o provedor de IA: {exc.user_message()}."
                + (f" Detalhe: {exc}" if len(str(exc)) < 300 else "")
                + " Nenhuma ação foi executada com base em resposta inventada."
            )
            task.error = f"{exc.kind}: {exc}"
            if task.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
                task.transition(TaskStatus.FAILED)
        except Exception as exc:  # nunca deixar o REPL em estado inconsistente
            logger.exception("erro inesperado no turno")
            final_text = f"Erro interno ao processar o pedido: {type(exc).__name__}: {exc}"
            task.error = final_text
            if task.status not in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
                task.transition(TaskStatus.FAILED)

        latency_ms = int((time.monotonic() - started) * 1000)
        if not final_text:
            final_text = "Não consegui produzir uma resposta final para este pedido."

        self.tasks.save(task)
        self.conversations.add_message(
            session_id,
            "assistant",
            final_text,
            meta={"task_id": task.id, "model": model_used, "status": task.status.value},
        )

        reply = AgentReply(
            text=final_text,
            task_id=task.id,
            status=task.status.value,
            model=model_used,
            tool_calls=tool_call_count,
            tools_used=tools_used,
            usage=usage,
            latency_ms=latency_ms,
            context=report,
        )
        self.events.emit(
            "turn.finished",
            task_id=task.id,
            status=reply.status,
            tools=len(tools_used),
            latency_ms=latency_ms,
        )
        self.audit.record(
            "turn.finished",
            task_id=task.id,
            status=reply.status,
            model=model_used,
            tools=tools_used,
            duration_ms=latency_ms,
            usage={"prompt": usage.prompt_tokens, "completion": usage.completion_tokens},
        )
        self.metrics.record("turn", value=latency_ms, model=model_used, tools=len(tools_used))

        self._after_turn(text, final_text, task, tools_used)
        return reply

    # --------------------------------------------------------------- ferramentas
    def _run_tool(
        self, call: ToolCall, task: Task, ctx: ToolContext
    ) -> tuple[ToolResult, bool]:
        fatal = False
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        started = time.perf_counter()

        if not self.registry.has(call.name):
            result = ToolResult.fail(f"ferramenta desconhecida: {call.name}")
            self.audit.record("tool.unknown", task_id=task.id, tool=call.name)
            return result, True

        spec = self.registry.get(call.name)
        decision = self.security.evaluate(spec.name, spec.risk)
        if not decision.allowed:
            self.audit.record(
                "tool.blocked", task_id=task.id, tool=call.name, reason=decision.reason
            )
            return ToolResult.fail(decision.reason), True

        if decision.requires_approval:
            task.transition(TaskStatus.WAITING_APPROVAL)
            self.tasks.save(task)
            self.events.emit(
                "tool.waiting_approval", task_id=task.id, tool=call.name, risk=spec.risk.label
            )
            request = ApprovalRequest(
                task_id=task.id,
                tool=call.name,
                args=call.arguments,
                risk=spec.risk,
                reason=decision.reason,
            )
            self.audit.record(
                "approval.requested",
                task_id=task.id,
                tool=call.name,
                risk=spec.risk.label,
                args=summarize_args(call.arguments),
            )
            approved = self.approver.approve(request)
            self.audit.record(
                "approval.decided", task_id=task.id, tool=call.name, approved=approved
            )
            task.transition(TaskStatus.RUNNING)
            self.tasks.save(task)
            if not approved:
                result = ToolResult.fail("ação não autorizada pelo usuário")
                self.tasks.record_tool_run(
                    task_id=task.id,
                    tool=call.name,
                    args=call.arguments,
                    risk=spec.risk.label,
                    status="negado",
                    summary=result.summary,
                    started_at=started_at,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
                return result, False

        result = self.registry.execute(call.name, call.arguments, ctx)
        duration_ms = int((time.perf_counter() - started) * 1000)
        self.tasks.record_tool_run(
            task_id=task.id,
            tool=call.name,
            args=call.arguments,
            risk=spec.risk.label,
            status="ok" if result.ok else "falhou",
            summary=result.summary,
            started_at=started_at,
            duration_ms=duration_ms,
        )
        self.audit.record(
            "tool.executed",
            task_id=task.id,
            tool=call.name,
            risk=spec.risk.label,
            args=summarize_args(call.arguments),
            ok=result.ok,
            summary=result.summary[:500],
            duration_ms=duration_ms,
        )
        self.events.emit(
            "tool.executed", task_id=task.id, tool=call.name, ok=result.ok, duration_ms=duration_ms
        )
        return result, fatal

    # ------------------------------------------------------------------ contexto
    def _build_context(self, user_text: str, session_id: str) -> tuple[list[ChatMessage], ContextReport]:
        summary = self.conversations.get_summary(session_id)
        stored = self.conversations.recent(
            session_id, limit=self.settings.limits.max_recent_messages, exclude_last=1
        )
        recent = [
            ChatMessage(role=item.role, content=item.content)
            for item in stored
            if item.role in {"user", "assistant"} and item.content
        ]
        found = self.memory.search(
            user_text, limit=self.settings.memory.retrieval_limit, min_score=0.18
        )
        memories = [record.render() for record in found]
        system = system_prompt(
            user_name=self.settings.user_name, voice_enabled=self.settings.voice.enabled
        )
        return self.context_manager.build(
            system=system,
            memories=memories,
            summary=summary,
            recent=recent,
            user_text=user_text,
        )

    # --------------------------------------------------------------- pós-turno
    def _after_turn(self, user_text: str, reply_text: str, task: Task, tools_used: list[str]) -> None:
        try:
            if _DO_NOT_REMEMBER_RE.search(user_text):
                self.audit.record("memory.skipped", task_id=task.id, reason="usuário pediu para não registrar")
                return
            if _MEMORY_REQUEST_RE.search(user_text):
                remembered = self.memory.remember(
                    user_text,
                    layer="semantic",
                    kind="preference",
                    tags=("preferencia",),
                    importance=0.8,
                    source="user",
                    session_id=task.session_id or None,
                )
                self.audit.record("memory.written", task_id=task.id, kind="semantic", memory_id=remembered)
            elif tools_used and task.status is TaskStatus.COMPLETED:
                content = (
                    f"Pedido: {user_text[:200]} | Ferramentas: {', '.join(tools_used)} | "
                    f"Resposta: {reply_text[:240]}"
                )
                remembered = self.memory.remember(
                    content,
                    layer="episodic",
                    kind="task_result",
                    tags=("tarefa",),
                    importance=0.4,
                    source="agent",
                    session_id=task.session_id or None,
                )
                self.audit.record("memory.written", task_id=task.id, kind="episodic", memory_id=remembered)
        except Exception:
            logger.exception("falha ao gravar memória pós-turno")

        self._maybe_compact()

    def _maybe_compact(self) -> None:
        if not self.settings.memory.auto_remember:
            return
        session_id = self.session_id
        if not session_id:
            return
        count = self.conversations.count_messages(session_id)
        if count == 0 or count % 20 != 0:
            return
        try:
            stored = self.conversations.recent(session_id, limit=40)
            older = stored[:-8]
            if len(older) < 6:
                return
            summarizer = Summarizer(
                self.provider, model=self.router.model_for("utility")
            )
            summary = summarizer.summarize(
                [ChatMessage(role=item.role, content=item.content) for item in older]
            )
            if summary:
                self.conversations.set_summary(session_id, summary)
                self.audit.record(
                    "conversation.compacted", session_id=session_id, messages=len(older)
                )
        except ProviderError as exc:
            logger.warning("compactação ignorada: %s", exc)
        except Exception:
            logger.exception("falha ao compactar conversa")

    # ------------------------------------------------------------------ acesso
    @property
    def last_context(self) -> ContextReport | None:
        return self._last_context


def _accumulate_usage(total: Usage, part: Usage) -> None:
    total.prompt_tokens += part.prompt_tokens
    total.completion_tokens += part.completion_tokens
    total.total_tokens += part.total_tokens
    total.estimated = total.estimated or part.estimated
    total.model = part.model or total.model
    if part.routed_via:
        total.routed_via = part.routed_via

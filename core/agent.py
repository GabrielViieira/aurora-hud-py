"""Agente conversacional com ferramentas, aprovação e memória."""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
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

# A proteção permite até duas execuções idênticas por tarefa. A terceira é
# bloqueada para impedir ciclos do LLM que insistem na mesma ação sem progresso.
_MAX_IDENTICAL_ACTION_EXECUTIONS = 2
_NON_MUTATING_DESKTOP_TOOLS = frozenset({"capturar_tela", "listar_aplicativos"})

# Um provedor pode reiniciar seu timeout de socket conforme recebe bytes.
# Impomos também um prazo de parede por chamada para que o HUD não fique
# indefinidamente em thinking se a conexão permanecer parcialmente aberta.
_MAX_LLM_CALL_WALL_SECONDS = 90.0
_LLM_CALL_TIMEOUT_PADDING_SECONDS = 15.0


class TaskDeadlineExceeded(TimeoutError):
    """O prazo máximo da chamada ao provedor ou do turno foi excedido."""



def _tool_call_signature(call: ToolCall) -> str:
    """Chave estável para detectar repetição da mesma ferramenta e argumentos."""
    args = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"{call.name}:{args}"


def _messages_contain_image(messages: list[ChatMessage]) -> bool:
    """Retorna True quando o contexto contém uma imagem que exige modelo visual."""
    for message in messages:
        content = message.content
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"image", "image_url"}:
                return True
    return False


def _is_action_tool(name: str, spec: Any) -> bool:
    """Detecta ações mutáveis, incluindo ferramentas desktop marcadas L0 por legado."""
    try:
        risk_level = int(spec.risk)
    except (AttributeError, TypeError, ValueError):
        risk_level = 0
    if risk_level >= 1:
        return True
    return (
        str(getattr(spec, "category", "")).casefold() == "desktop"
        and name not in _NON_MUTATING_DESKTOP_TOOLS
    )

def _should_auto_capture_after_action(
    name: str, arguments: dict[str, Any], registry: ToolRegistry
) -> bool:
    """Diz se uma execução bem-sucedida deve ser seguida de captura visual automática."""
    if name in _NON_MUTATING_DESKTOP_TOOLS:
        return False
    # Abrir uma URL inicia/navega no navegador, embora sua categoria seja "sistema".
    if name == "abrir_url":
        return True
    # Mover o cursor não altera o estado útil da interface; evite capturas desnecessárias.
    if name == "controlar_mouse" and str(arguments.get("acao", "")).strip().lower() == "mover":
        return False
    if not registry.has(name):
        return False
    try:
        spec = registry.get(name)
    except Exception:
        return False
    return str(getattr(spec, "category", "")).casefold() == "desktop"


# AURORA_LOCAL_APP_ROUTER_V1
def _local_memory_intent(text: str) -> tuple[bool, str] | None:
    """Detecta pedidos explícitos para guardar uma instrução sem chamar o LLM."""
    raw = (text or "").strip()
    raw = re.sub(r"^(?:(?:ei|hey)\s+)?aurora[\s,!:;.-]*", "", raw, flags=re.IGNORECASE).strip()

    patterns = (
        r"^(?:por favor\s+)?(?:guarde|guardar|guarda)\s+"
        r"(?:(?:na mem[oó]ria|para lembrar)(?:\s*[:,-])?\s+)?"
        r"(?:(?:a|uma|esta|essa|seguinte)\s+)?"
        r"(?:(?:instru[cç][aã]o|regra|prefer[eê]ncia|nota|fato)\s*)?"
        r"(?::|,|-)?\s*(.*)$",
        r"^(?:por favor\s+)?(?:lembre-se de|lembre de|lembra de|anote|anotar|memorize|memorizar|registre|registrar)\s*"
        r"(?:(?:que|o seguinte)\s+)?(.*)$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, raw, flags=re.IGNORECASE)
        if match:
            instruction = (match.group(1) or "").strip().strip(" \t\r\n,;:-")
            if instruction.casefold().startswith("que "):
                instruction = instruction[4:].strip()
            return True, instruction
    return None


def _local_app_intent(text: str) -> tuple[str, dict[str, str]] | None:
    """Roteia comandos curtos e explícitos para ferramentas locais, sem consultar o LLM."""
    value = unicodedata.normalize("NFKD", (text or "").strip().casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"^[\s,!:;.-]*(?:ei\s+)?aurora[\s,!:;.-]*", "", value)
    value = re.sub(r"[\s.!?]+$", "", value).strip()
    if re.fullmatch(r"(?:liste|listar|mostre|mostrar)\s+(?:(?:os|as)\s+)?aplicativos(?:\s+abertos)?", value) or value in {"quais aplicativos estao abertos", "listar apps", "liste apps"}:
        return ("listar_aplicativos", {"filtro": "", "max_resultados": 40})
    open_match = re.fullmatch(
        r"(?:por favor\s+)?(?:abra|abre|abrir|inicie|inicia|iniciar|execute|executar|open|launch|start)\s+(?:(?:o|a|os|as|um|uma|the)\s+)?([a-z0-9][a-z0-9 ._+#()\-]{0,79})",
        value,
    )
    if open_match:
        name = open_match.group(1).strip(" ._-")
        if name:
            return ("abrir_aplicativo", {"nome": name})
    restore_match = re.fullmatch(
        r"(?:por favor\s+)?(?:restaure|restaurar|mostre|mostrar|traga(?:\s+para\s+frente)?|reabra|reabrir|restore|show)\s+(?:(?:o|a|os|as|the)\s+)?([a-z0-9][a-z0-9 ._+#()\-]{0,79})",
        value,
    )
    if restore_match:
        name = restore_match.group(1).strip(" ._-")
        if name:
            return ("restaurar_aplicativo", {"nome": name})
    return None


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
        # Compactação do histórico não deve bloquear a resposta ao usuário.
        self._compaction_lock = threading.Lock()
        self._compaction_pending: set[str] = set()

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

    def reset_cancel(self) -> None:
        """Limpa um cancelamento anterior antes de iniciar explicitamente um novo turno."""
        self.cancel_event.clear()

    def _chat_with_cancel(
        self, request: LLMRequest, *, deadline: float
    ) -> LLMResponse | None:
        """Espera o provedor respeitando cancelamento e prazo global do turno.

        A requisição HTTP subjacente não é interrompida à força pela stdlib; se o
        usuário cancelar ou o prazo vencer, sua thread daemon é abandonada e a
        resposta tardia é ignorada. O agente e a interface, porém, são liberados.
        """
        completed = threading.Event()
        outcome: dict[str, Any] = {}

        def request_worker() -> None:
            try:
                outcome["response"] = self.provider.chat(request)
            except Exception as exc:
                outcome["error"] = exc
            finally:
                completed.set()

        worker = threading.Thread(
            target=request_worker,
            daemon=True,
            name="AuroraCancellableLLMRequest",
        )
        worker.start()
        while not completed.is_set():
            if self.cancel_event.is_set():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TaskDeadlineExceeded(
                    "O prazo máximo da tarefa venceu enquanto aguardava resposta do provedor de IA."
                )
            completed.wait(min(0.05, remaining))
        if self.cancel_event.is_set():
            return None
        error = outcome.get("error")
        if isinstance(error, Exception):
            raise error
        response = outcome.get("response")
        if response is None:
            raise RuntimeError("O provedor terminou sem resposta nem erro.")
        return response

    # ------------------------------------------------------------------ turno
    def handle(self, user_text: str) -> AgentReply:
        text = (user_text or "").strip()
        if not text:
            return AgentReply(text="", status=TaskStatus.CANCELLED.value)

        session_id = self.ensure_session()
        started = time.monotonic()
        deadline = started + max(0.0, float(self.settings.limits.max_task_seconds))
        if self.cancel_event.is_set():
            return AgentReply(
                text="Tarefa cancelada a pedido do usuário.",
                status=TaskStatus.CANCELLED.value,
            )

        self.conversations.add_message(session_id, "user", text)
        task = Task.create(text, session_id=session_id)
        task.transition(TaskStatus.RUNNING)
        self.tasks.save(task)
        self.events.emit("turn.started", task_id=task.id, session_id=session_id)
        self.audit.record("turn.started", task_id=task.id, message=text[:500])

        # Salvar instruções explícitas não deve depender do provedor de linguagem.
        # O caminho local responde mesmo quando o LLM está lento ou indisponível.
        memory_intent = _local_memory_intent(text)
        if memory_intent is not None:
            _, instruction = memory_intent
            memory_id = None
            if not instruction:
                final_text = (
                    "Qual instrução devo guardar? Diga, por exemplo: "
                    "'Aurora, guarde esta instrução: tire uma captura de tela antes de clicar.'"
                )
                task.transition(TaskStatus.COMPLETED)
                memory_status = "needs_details"
            elif len(instruction) > 1500:
                final_text = "A instrução é muito longa para uma memória única. Resuma-a para até 1500 caracteres e tente novamente."
                task.error = "instrução excede 1500 caracteres"
                task.transition(TaskStatus.FAILED)
                memory_status = "too_long"
            else:
                try:
                    memory_id = self.memory.remember(
                        instruction,
                        layer="semantic",
                        kind="instruction",
                        tags=("instrucao", "preferencia"),
                        importance=0.95,
                        source="user",
                        session_id=session_id,
                    )
                    if memory_id is None:
                        final_text = (
                            "Não consegui guardar essa instrução. A memória recusou o conteúdo "
                            "por estar vazio ou parecer conter credenciais; nada foi salvo."
                        )
                        task.error = "instrução não gravada pela política de memória"
                        task.transition(TaskStatus.FAILED)
                        memory_status = "rejected"
                    else:
                        final_text = f"Guardei a instrução na memória local: {instruction}"
                        task.transition(TaskStatus.COMPLETED)
                        memory_status = "saved"
                        self.audit.record(
                            "memory.written",
                            task_id=task.id,
                            kind="instruction",
                            memory_id=memory_id,
                            content=instruction[:300],
                        )
                except Exception as exc:
                    logger.exception("falha ao salvar instrução solicitada pelo usuário")
                    final_text = f"Não consegui guardar a instrução: {type(exc).__name__}: {exc}"
                    task.error = final_text
                    task.transition(TaskStatus.FAILED)
                    memory_status = "error"

            self.tasks.save(task)
            self.conversations.add_message(
                session_id,
                "assistant",
                final_text,
                meta={"task_id": task.id, "model": "local-memory", "status": task.status.value},
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            reply = AgentReply(
                text=final_text,
                task_id=task.id,
                status=task.status.value,
                model="local-memory",
                tool_calls=0,
                tools_used=["memoria_local"] if memory_status == "saved" else [],
                latency_ms=latency_ms,
                context=None,
            )
            self.events.emit(
                "turn.finished",
                task_id=task.id,
                status=reply.status,
                tools=len(reply.tools_used),
                latency_ms=latency_ms,
            )
            self.audit.record(
                "turn.finished",
                task_id=task.id,
                status=reply.status,
                model=reply.model,
                tools=reply.tools_used,
                duration_ms=latency_ms,
                memory_status=memory_status,
            )
            self.metrics.record("turn", value=latency_ms, model=reply.model, tools=len(reply.tools_used))
            return reply

        messages, report = self._build_context(text, session_id)
        self._last_context = report
        tools = self.registry.schemas(self.settings.enabled_tools)
        usage = Usage()
        tools_used: list[str] = []
        final_text = ""
        model_used = ""
        tool_call_count = 0
        failures: dict[str, int] = {}
        # Conta execuções reais por assinatura durante este turno, não apenas falhas.
        action_call_counts: dict[str, int] = {}
        ctx = ToolContext(
            settings=self.settings,
            policy=self.policy,
            audit=self.audit,
            task_id=task.id,
            cancel_event=self.cancel_event,
        )
        local_app_intent = _local_app_intent(text)

        try:
            iterations = 0
            while True:
                if self.cancel_event.is_set():
                    final_text = "Tarefa cancelada a pedido do usuário."
                    task.transition(TaskStatus.CANCELLED)
                    break
                if time.monotonic() >= deadline:
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
                if local_app_intent is not None:
                    local_name, local_args = local_app_intent
                    local_call = ToolCall(
                        id=f"local-{task.id}", name=local_name, arguments=local_args
                    )
                    local_result, local_fatal = self._run_tool(local_call, task, ctx)
                    tool_call_count += 1
                    tools_used.append(local_name)
                    final_text = local_result.summary if local_result.ok else (
                        f"Não consegui concluir o comando local: {local_result.error or local_result.summary}"
                    )
                    if local_result.ok:
                        task.transition(TaskStatus.COMPLETED)
                    else:
                        task.error = final_text
                        task.transition(TaskStatus.FAILED)
                    break
                request = LLMRequest(
                    messages=messages,
                    tools=tools,
                    model=self.router.model_for(
                        "vision" if _messages_contain_image(messages) else "chat"
                    ),
                    temperature=self.settings.llm.temperature,
                    max_tokens=self.settings.llm.max_output_tokens,
                )
                llm_started = time.monotonic()
                configured_timeout = max(1.0, float(self.settings.llm.timeout_s))
                request_timeout = max(
                    15.0,
                    min(
                        _MAX_LLM_CALL_WALL_SECONDS,
                        configured_timeout + _LLM_CALL_TIMEOUT_PADDING_SECONDS,
                    ),
                )
                request_deadline = min(deadline, llm_started + request_timeout)
                response = self._chat_with_cancel(request, deadline=request_deadline)
                if response is None or self.cancel_event.is_set():
                    final_text = "Tarefa cancelada a pedido do usuário."
                    task.error = "cancelada pelo usuário"
                    if task.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}:
                        task.transition(TaskStatus.CANCELLED)
                    break
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
                vision_warnings: list[str] = []
                desktop_tool_names = {
                    "capturar_tela", "controlar_mouse", "controlar_teclado",
                    "abrir_aplicativo", "restaurar_aplicativo", "abrir_url",
                }
                desktop_calls = [call.name for call in response.tool_calls if call.name in desktop_tool_names]
                preferred_desktop_tool = (
                    next((name for name in ("abrir_aplicativo", "restaurar_aplicativo", "abrir_url") if name in desktop_calls), "")
                    or ("capturar_tela" if "capturar_tela" in desktop_calls else (desktop_calls[0] if desktop_calls else ""))
                )
                desktop_batch_requires_serial = len(desktop_calls) > 1
                for call in response.tool_calls:
                    # Uma única resposta do modelo pode conter várias chamadas; ao cancelar,
                    # não inicie a próxima ferramenta do lote.
                    if self.cancel_event.is_set():
                        final_text = "Tarefa cancelada a pedido do usuário."
                        task.error = "cancelada pelo usuário"
                        if task.status not in {TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.FAILED}:
                            task.transition(TaskStatus.CANCELLED)
                        break
                    tool_call_count += 1
                    signature = _tool_call_signature(call)
                    deferred = (
                        call.name in desktop_tool_names
                        and desktop_batch_requires_serial
                        and call.name != preferred_desktop_tool
                    )
                    if deferred:
                        result = ToolResult(
                            ok=True,
                            summary=("AÇÃO NÃO EXECUTADA: etapa adiada para manter o ciclo visual "
                                     "capturar/analisar/agir/verificar em sequência. Execute-a em uma chamada posterior."),
                            data={"adiada": True},
                        )
                        fatal = False
                    else:
                        guarded_action = False
                        if self.registry.has(call.name):
                            guarded_action = _is_action_tool(call.name, self.registry.get(call.name))
                        attempts = action_call_counts.get(signature, 0)
                        if guarded_action and attempts >= _MAX_IDENTICAL_ACTION_EXECUTIONS:
                            reason = (
                                "Proteção anti-loop: bloqueei a ferramenta " + call.name
                                + " porque a mesma ação com os mesmos argumentos já foi executada "
                                + str(attempts) + " vezes nesta tarefa. Não executei esta tentativa repetida. "
                                "Reavalie o resultado anterior e escolha uma etapa diferente; não repita a ação sem evidência de progresso."
                            )
                            result = ToolResult.fail(
                                reason, anti_loop=True, ferramenta=call.name, execucoes_anteriores=attempts
                            )
                            fatal = True
                            self.audit.record(
                                "tool.repeat_blocked", task_id=task.id, tool=call.name,
                                signature=signature, previous_executions=attempts,
                            )
                        else:
                            if guarded_action:
                                action_call_counts[signature] = attempts + 1
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
                    # Se o usuário cancelou durante a execução da ferramenta, não faça
                    # a captura seguinte nem continue com outras ações do mesmo lote.
                    if self.cancel_event.is_set():
                        final_text = "Tarefa cancelada a pedido do usuário."
                        task.error = "cancelada pelo usuário"
                        if task.status not in {TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.FAILED}:
                            task.transition(TaskStatus.CANCELLED)
                        break

                    # Não dependa do LLM lembrar de pedir outro print: após cada ação
                    # visual bem-sucedida, observe o estado real antes da próxima decisão.
                    capture_enabled = (
                        not self.settings.enabled_tools
                        or "capturar_tela" in self.settings.enabled_tools
                    )
                    if (
                        result.ok
                        and not result.data.get("adiada")
                        and call.name != "capturar_tela"
                        and capture_enabled
                        and self.registry.has("capturar_tela")
                        and _should_auto_capture_after_action(call.name, call.arguments, self.registry)
                    ):
                        # Dá um intervalo curto para a janela responder. A captura pode
                        # mostrar carregamento; o modelo poderá esperar/observar de novo.
                        time.sleep(0.6 if call.name in {"abrir_aplicativo", "restaurar_aplicativo", "abrir_url"} else 0.2)
                        capture_call = ToolCall(
                            id=f"auto-capture-{call.id}",
                            name="capturar_tela",
                            arguments={"monitor": 1, "largura_maxima": 1920},
                        )
                        capture_result, _ = self._run_tool(capture_call, task, ctx)
                        tool_call_count += 1
                        tools_used.append("capturar_tela")
                        image_url = capture_result.data.pop("data_url", "") if capture_result.ok else ""
                        if isinstance(image_url, str) and image_url.startswith("data:image/"):
                            vision_attachments.append((
                                image_url,
                                f"Captura automática após ação {call.name}. Examine a imagem para recalcular o próximo passo; "
                                f"o sucesso da chamada não significa, por si só, que o objetivo final foi concluído. {capture_result.summary}",
                            ))
                        else:
                            detail = capture_result.error or capture_result.summary or "a captura não retornou uma imagem válida"
                            vision_warnings.append(
                                f"A ação {call.name} foi executada, mas a captura visual automática falhou: {detail}. "
                                "Não execute outro clique/digitação às cegas; tente capturar_tela como próxima etapa."
                            )

                    key = signature
                    # Uma etapa adiada não é uma ação executada. Conte repetições
                    # idênticas para evitar consumir todas as iterações em loop.
                    if result.data.get("adiada"):
                        failures[key] = failures.get(key, 0) + 1
                    elif not result.ok:
                        failures[key] = failures.get(key, 0) + 1
                    if fatal or failures.get(key, 0) >= 2:
                        if result.data.get("adiada"):
                            final_text = (
                                f"Interrompi porque a etapa `{call.name}` foi adiada repetidamente "
                                "no mesmo ciclo visual. Nenhuma ação foi realizada por esta chamada. "
                                "Tente novamente com uma etapa por vez: abrir/restaurar, capturar a tela, analisar, agir e verificar."
                            )
                        else:
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
                for warning in vision_warnings:
                    messages.append(ChatMessage(role="user", content=warning))
                if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
                    break

        except TaskDeadlineExceeded as exc:
            elapsed_s = time.monotonic() - started
            logger.warning(
                "prazo de espera do modelo excedido",
                extra={"aurora": {"elapsed_s": round(elapsed_s, 2)}},
            )
            final_text = (
                "Interrompi esta tarefa porque o modelo de IA não respondeu dentro do "
                "prazo de segurança. A Aurora foi liberada e não usará uma resposta atrasada "
                "para iniciar novas ações. Tente novamente; se acontecer de novo, verifique "
                "a conexão e a disponibilidade do provedor."
            )
            task.error = f"prazo de espera do modelo/turno excedido após {elapsed_s:.1f}s: {exc}"
            if task.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}:
                task.transition(TaskStatus.FAILED)
        except ProviderError as exc:
            logger.warning("falha do provedor", extra={"aurora": {"kind": exc.kind}})
            if self.cancel_event.is_set():
                final_text = "Tarefa cancelada a pedido do usuário."
                task.error = "cancelada pelo usuário"
                if task.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}:
                    task.transition(TaskStatus.CANCELLED)
            else:
                final_text = (
                    f"Não consegui falar com o provedor de IA: {exc.user_message()}."
                    + (f" Detalhe: {exc}" if len(str(exc)) < 300 else "")
                    + " Nenhuma ação foi executada com base em resposta inventada."
                )
                task.error = f"{exc.kind}: {exc}"
                if task.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
                    task.transition(TaskStatus.FAILED)
        except Exception as exc:  # nunca deixar o REPL em estado inconsistente
            if self.cancel_event.is_set():
                final_text = "Tarefa cancelada a pedido do usuário."
                task.error = "cancelada pelo usuário"
                if task.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}:
                    task.transition(TaskStatus.CANCELLED)
            else:
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

        if local_app_intent is None:
            self._after_turn(text, final_text, task, tools_used)
        return reply

    # --------------------------------------------------------------- ferramentas
    def _run_tool(
        self, call: ToolCall, task: Task, ctx: ToolContext
    ) -> tuple[ToolResult, bool]:
        fatal = False
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        started = time.perf_counter()

        # Última barreira antes de executar: cancelar pode acontecer entre a decisão
        # do modelo e a entrada nesta função.
        if self.cancel_event.is_set():
            final_statuses = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
            if task.status not in final_statuses:
                task.transition(TaskStatus.CANCELLED)
            task.error = "cancelada pelo usuário"
            self.tasks.save(task)
            self.audit.record("tool.cancelled", task_id=task.id, tool=call.name, reason="pedido de cancelamento")
            return ToolResult.fail("Tarefa cancelada antes da execução da ferramenta."), False

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
            if self.cancel_event.is_set():
                task.transition(TaskStatus.CANCELLED)
                task.error = "cancelada pelo usuário"
                self.tasks.save(task)
                result = ToolResult.fail("Tarefa cancelada; a ação protegida não foi executada.")
                self.tasks.record_tool_run(
                    task_id=task.id,
                    tool=call.name,
                    args=call.arguments,
                    risk=spec.risk.label,
                    status="cancelado",
                    summary=result.summary,
                    started_at=started_at,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
                self.audit.record("tool.cancelled", task_id=task.id, tool=call.name)
                return result, False
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
        """Agenda resumo do histórico sem atrasar a entrega da resposta atual."""
        if not self.settings.memory.auto_remember:
            return
        session_id = self.session_id
        if not session_id:
            return
        count = self.conversations.count_messages(session_id)
        if count == 0 or count % 20 != 0:
            return

        with self._compaction_lock:
            if session_id in self._compaction_pending:
                return
            self._compaction_pending.add(session_id)

        worker = threading.Thread(
            target=self._compact_session,
            args=(session_id,),
            daemon=True,
            name="AuroraContextCompaction",
        )
        try:
            worker.start()
        except Exception:
            with self._compaction_lock:
                self._compaction_pending.discard(session_id)
            logger.exception("não foi possível iniciar compactação em segundo plano")

    def _compact_session(self, session_id: str) -> None:
        """Resumo auxiliar em background; falhas não interrompem a conversa."""
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
        finally:
            with self._compaction_lock:
                self._compaction_pending.discard(session_id)

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

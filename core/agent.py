"""Agente conversacional com ferramentas, aprovação e memória."""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import unicodedata
from io import BytesIO
from urllib.parse import quote_plus
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
from aurora.llm.token_manager import estimate_tokens, truncate_to_tokens
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
# Ferramentas que já confirmam o próprio efeito e não precisam de captura automática.
_SELF_VERIFYING_DESKTOP_TOOLS = frozenset({"pesquisar"})

# Um provedor pode reiniciar seu timeout de socket conforme recebe bytes.
# Impomos também um prazo de parede por chamada para que o HUD não fique
# indefinidamente em thinking se a conexão permanecer parcialmente aberta.
_MAX_LLM_CALL_WALL_SECONDS = 90.0
# Modelos visuais locais podem precisar de mais tempo na primeira inferência.
_MAX_LOCAL_VISION_CALL_WALL_SECONDS = 150.0
_LLM_CALL_TIMEOUT_PADDING_SECONDS = 15.0


class TaskDeadlineExceeded(TimeoutError):
    """O prazo máximo da chamada ao provedor ou do turno foi excedido."""



def _tool_call_signature(call: ToolCall) -> str:
    """Chave estável para detectar repetição da mesma ferramenta e argumentos."""
    args = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"{call.name}:{args}"

def _unknown_tool_names(calls: list[ToolCall], available_names: set[str]) -> list[str]:
    """Lista nomes únicos de ferramentas que não estão disponíveis nesta chamada."""
    return sorted({call.name for call in calls if call.name not in available_names})


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

def _has_useful_accessibility_snapshot(messages: list[ChatMessage]) -> bool:
    """Retorna True se uma mensagem de ferramenta contém controles UIA utilizáveis."""
    for message in messages:
        if message.role != "tool" or not isinstance(message.content, str):
            continue
        try:
            payload = json.loads(message.content)
        except (json.JSONDecodeError, TypeError):
            continue
        data = payload.get("data")
        if not isinstance(data, dict):
            continue
        snapshot = data.get("interface_acessivel")
        if not isinstance(snapshot, dict) or not snapshot.get("disponivel"):
            continue
        elements = snapshot.get("elementos")
        if isinstance(elements, list) and elements:
            return True
    return False


def _shrink_local_vision_image_part(part: dict[str, Any], max_width: int) -> dict[str, Any]:
    """Reduz imagens data URL antes de enviá-las ao modelo visual local."""
    try:
        image_url = part.get("image_url")
        if isinstance(image_url, dict):
            url = image_url.get("url")
            if not isinstance(url, str) or not url.startswith("data:image/") or "," not in url:
                return part
            _, encoded = url.split(",", 1)
            raw = base64.b64decode(encoded)
            from PIL import Image

            with Image.open(BytesIO(raw)) as source:
                image = source.convert("RGB")
            if image.width <= max_width and image.height <= max_width * 2:
                return part
            image.thumbnail((max_width, max_width * 2), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=76, optimize=True)
            resized_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
            updated = dict(part)
            updated_url = dict(image_url)
            updated_url["url"] = resized_url
            updated["image_url"] = updated_url
            return updated

        raw_url = part.get("data")
        if isinstance(raw_url, str) and raw_url.startswith("data:image/") and "," in raw_url:
            _, encoded = raw_url.split(",", 1)
            raw = base64.b64decode(encoded)
            from PIL import Image

            with Image.open(BytesIO(raw)) as source:
                image = source.convert("RGB")
            if image.width <= max_width and image.height <= max_width * 2:
                return part
            image.thumbnail((max_width, max_width * 2), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=76, optimize=True)
            updated = dict(part)
            updated["data"] = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
            return updated
    except Exception:
        # Não quebrar uma solicitação por imagem não reconhecida; o provedor responderá
        # normalmente ou devolverá um erro explícito para o caminho de fallback.
        return part
    return part


def _is_vision_repetition_error(exc: ProviderError) -> bool:
    """Reconhece o aborto por geração repetitiva do backend local.

    O Ollama responde `prediction aborted, token repeat limit reached` quando a
    decodificação degenera em loop. Não é falta de contexto: encolher a imagem
    não corrige, e cada tentativa custa uma inferência inteira.
    """
    detail = f"{exc} {getattr(exc, 'body', '')}".casefold()
    return "repeat" in detail or "repeti" in detail or "degener" in detail


def _is_vision_token_limit_error(exc: ProviderError) -> bool:
    """Reconhece erros de contexto/tokens do backend visual para tentar uma imagem menor."""
    if _is_vision_repetition_error(exc):
        return False
    detail = f"{exc} {getattr(exc, 'body', '')}".casefold()
    has_token_or_context = any(
        word in detail for word in ("token", "context length", "context window", "prompt length", "sequence length")
    )
    has_limit = any(
        word in detail for word in (
            "limit", "maximum", "max_", "exceed", "too large", "too many",
            "reached", "exceeded", "atingido", "limite", "ultrapass", "context length",
        )
    )
    status = getattr(exc, "status", None)
    return has_token_or_context and has_limit and (status is None or int(status) >= 400)


def _is_vision_backend_unreachable(exc: ProviderError) -> bool:
    """Falha de transporte com o backend visual local (Ollama desligado, porta fechada).

    Diferente de erro de modelo ou de imagem ruim, aqui não há resposta para
    analisar: insistir na chamada só gasta tempo. Nesse caso o agente remove os
    pixels e segue com os controles de acessibilidade.
    """
    kind = str(getattr(exc, "kind", "") or "")
    status = getattr(exc, "status", None)
    return status is None and kind in {"unavailable", "timeout"}


def _is_token_limit_error(exc: ProviderError) -> bool:
    """Detecta recusas que parecem vir do limite de contexto/tokens do gateway."""
    detail = f"{exc} {getattr(exc, 'body', '')}".casefold()
    has_token_signal = any(term in detail for term in (
        "token", "context window", "context length", "prompt length",
    ))
    has_limit_signal = any(term in detail for term in (
        "limit", "maximum", "max_", "exceed", "too large", "too many",
        "atingido", "limite", "ultrapass",
    ))
    return has_token_signal and has_limit_signal


def _estimate_request_tokens(messages: list[ChatMessage], tools: list[dict[str, Any]]) -> int:
    """Estima mensagens + schemas sem contar todo o base64 de imagens como tokens."""
    total = estimate_tokens(json.dumps(tools, ensure_ascii=False, default=str)) + 64
    for message in messages:
        content = message.content
        if isinstance(content, list):
            visible_text: list[str] = []
            image_count = 0
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"image", "image_url"}:
                    image_count += 1400
                elif part.get("type") == "text":
                    visible_text.append(str(part.get("text") or ""))
                else:
                    visible_text.append(json.dumps(part, ensure_ascii=False, default=str))
            content_tokens = estimate_tokens("\n".join(visible_text)) + image_count
        else:
            content_tokens = estimate_tokens(str(content or ""))
        total += content_tokens + estimate_tokens(message.role) + 4
        if message.tool_calls:
            calls = [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in message.tool_calls
            ]
            total += estimate_tokens(json.dumps(calls, ensure_ascii=False, default=str)) + 8
    return total


def _compact_tool_schemas(
    tools: list[dict[str, Any]], description_tokens: int = 24
) -> list[dict[str, Any]]:
    """Encurta descrições sem alterar nomes, tipos, enums ou campos obrigatórios."""
    compacted = json.loads(json.dumps(tools, ensure_ascii=False, default=str))

    def shorten(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in list(value.items()):
                if key == "description" and isinstance(item, str):
                    value[key] = truncate_to_tokens(item, description_tokens, suffix=" […]")
                else:
                    shorten(item)
        elif isinstance(value, list):
            for item in value:
                shorten(item)

    shorten(compacted)
    return compacted


def _compact_messages_for_token_retry(
    messages: list[ChatMessage],
    user_text: str,
    tools: list[dict[str, Any]],
    *,
    budget: int,
    aggressive: bool = False,
) -> list[ChatMessage]:
    """Preserva instruções-base, pedido atual e ciclos recentes com resultados de ferramentas."""
    if not messages:
        return []

    current_user_index = max(
        (
            index for index, message in enumerate(messages)
            if message.role == "user"
            and isinstance(message.content, str)
            and message.content.strip() == user_text
        ),
        default=-1,
    )
    if current_user_index < 0:
        return messages

    system_message = next((message for message in messages if message.role == "system"), None)
    current_user = messages[current_user_index]

    def compact_message(message: ChatMessage) -> ChatMessage:
        content = message.content
        if isinstance(content, str):
            if message.role == "tool":
                content = truncate_to_tokens(
                    content, 180 if aggressive else 420, suffix=" […]"
                )
            elif message.role == "user" and message is not current_user:
                content = truncate_to_tokens(
                    content, 280 if aggressive else 650, suffix=" […]"
                )
        elif isinstance(content, list):
            copied_parts = [dict(part) if isinstance(part, dict) else part for part in content]
            for part in copied_parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    part["text"] = truncate_to_tokens(
                        str(part.get("text") or ""),
                        220 if aggressive else 500,
                        suffix=" […]",
                    )
            content = copied_parts
        return ChatMessage(
            role=message.role,
            content=content,
            tool_calls=list(message.tool_calls),
            tool_call_id=message.tool_call_id,
            name=message.name,
        )

    # Um assistant com tool_calls e os tool results seguintes precisam ficar juntos.
    groups: list[list[ChatMessage]] = []
    active_group: list[ChatMessage] = []
    for message in messages[current_user_index + 1:]:
        if message.role == "assistant" and message.tool_calls:
            if active_group:
                groups.append(active_group)
            active_group = [message]
        elif active_group:
            active_group.append(message)
        else:
            groups.append([message])
    if active_group:
        groups.append(active_group)

    base: list[ChatMessage] = []
    if system_message is not None:
        base.append(system_message)
    base.append(current_user)

    selected_groups: list[list[ChatMessage]] = []
    max_groups = 2 if aggressive else 4
    for group in reversed(groups):
        if len(selected_groups) >= max_groups:
            break
        compact_group = [compact_message(message) for message in group]
        candidate_groups = [compact_group] + selected_groups
        candidate = base + [message for items in candidate_groups for message in items]
        if _estimate_request_tokens(candidate, tools) <= budget:
            selected_groups = candidate_groups
        elif not selected_groups:
            selected_groups = [[compact_message(message) for message in group]]
            break
        else:
            break

    return base + [message for items in selected_groups for message in items]


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

def _should_use_accessibility_only_verification(name: str, arguments: dict[str, Any]) -> bool:
    """Escolhe verificação pós-ação rápida quando pixels não são necessários para o próximo passo.

    A ferramenta `capturar_tela` com `somente_acessibilidade` já captura a imagem
    sozinha quando o UIA não devolve controles úteis, então preferir a árvore de
    acessibilidade não é verificação cega: é tentar o caminho de 0,6s antes do de 13s.
    """
    action = str(arguments.get("acao", "")).strip().lower()
    if name == "controlar_teclado":
        # digitar e pressionar tecla mudam o estado que o UIA já expõe.
        return action in {"digitar", "pressionar"}
    if name == "controlar_mouse":
        # Clique por coordenada também: se nada mudar na árvore, a ferramenta
        # devolve imagem por conta própria.
        return action in {"clicar", "duplo_clique", "clique_direito"}
    return False


def _verification_settle_seconds(name: str, arguments: dict[str, Any]) -> float:
    """Espera antes de observar o resultado de uma ação que já foi executada."""
    if name in {"abrir_aplicativo", "restaurar_aplicativo", "abrir_url"}:
        return 0.45
    if _should_use_accessibility_only_verification(name, arguments):
        # Um clique dispara evento e a árvore leva alguns quadros para refletir;
        # 40ms costumava ler o estado anterior à ação.
        return 0.12 if name == "controlar_mouse" else 0.04
    return 0.10


def _should_auto_capture_after_action(
    name: str, arguments: dict[str, Any], registry: ToolRegistry
) -> bool:
    """Diz se uma execução bem-sucedida deve ser seguida de captura visual automática."""
    if name in _NON_MUTATING_DESKTOP_TOOLS:
        return False
    # "pesquisar" já devolve o valor lido no campo; uma captura extra só custaria tempo.
    if name in _SELF_VERIFYING_DESKTOP_TOOLS:
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

    # Atalho determinístico para pesquisa explicitamente pedida na Steam.
    # Abre os resultados diretamente, sem depender de visão/cliques no campo de busca.
    steam_match = re.fullmatch(
        r"(?:por favor\s+)?(?:pesquise|pesquisar|pesquisa|procure|procurar|busque|buscar|encontre|encontrar)\s+"
        r"(?:por\s+)?(.+?)\s+(?:na|no)\s+(?:loja\s+da\s+)?steam",
        value,
    )
    if steam_match:
        game_query = (steam_match.group(1) or "").strip(" \t.,!?;:")
        game_query = re.sub(r"^(?:(?:o|a|um|uma)\s+)?(?:jogo|game)\s+", "", game_query)
        game_query = re.sub(r"^(?:o|a|um|uma)\s+", "", game_query).strip()
        if game_query and len(game_query) <= 120:
            return (
                "abrir_url",
                {"url": "https://store.steampowered.com/search/?term=" + quote_plus(game_query)},
            )

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
        vision_provider=None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        # Se configurado, usa um endpoint separado apenas para mensagens com imagens.
        self.vision_provider = vision_provider
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

    def _provider_for_request(self, request: LLMRequest):
        """Escolhe a visão local para pedidos com imagens; o chat segue no provedor principal."""
        if _messages_contain_image(request.messages):
            vision_provider = getattr(self, "vision_provider", None)
            if vision_provider is not None:
                return vision_provider
        return self.provider

    def _prepare_provider_request(
        self,
        request: LLMRequest,
        request_provider,
        *,
        vision_max_width: int = 640,
        repetition_retry: bool = False,
    ) -> LLMRequest:
        """Prepare a compact perception-only prompt and image for the local vision model."""
        vision_provider = getattr(self, "vision_provider", None)
        if not (
            vision_provider is not None
            and request_provider is vision_provider
            and request_provider is not self.provider
            and _messages_contain_image(request.messages)
        ):
            return request

        image_index = next(
            index
            for index in range(len(request.messages) - 1, -1, -1)
            if _messages_contain_image([request.messages[index]])
        )
        image_message = request.messages[image_index]
        user_goal = ""
        for message in reversed(request.messages[:image_index]):
            if message.role == "user" and isinstance(message.content, str) and message.content.strip():
                user_goal = message.content.strip()[:800]
                break

        image_parts = [
            _shrink_local_vision_image_part(part, vision_max_width)
            for part in (image_message.content if isinstance(image_message.content, list) else [])
            if isinstance(part, dict) and part.get("type") in {"image_url", "image"}
        ]
        if not image_parts:
            return request

        anti_repetition = (
            " Sua descrição anterior entrou em repetição: escreva no máximo quatro fatos "
            "curtos e distintos, sem repetir palavras nem listar o mesmo item duas vezes."
            if repetition_retry
            else ""
        )
        prompt = (
            "Você é o módulo de percepção visual local da Aurora. Não responda ao usuário, "
            "não dê passo a passo e não diga o que a Aurora deveria fazer. Descreva somente "
            "fatos visíveis que ajudem outro agente a executar o pedido; identifique a janela "
            "em primeiro plano, elementos relevantes, estado atual e texto legível. "
            "Se coordenadas não estiverem disponíveis, não invente coordenadas. "
            "Responda em português com observações objetivas em até 600 caracteres. "
            f"Pedido original: {user_goal or 'Identifique a janela atual e seus elementos relevantes.'}"
            f"{anti_repetition}"
        )
        compact_request = LLMRequest(
            messages=[
                ChatMessage(
                    role="user",
                    content=[{"type": "text", "text": prompt}, *image_parts],
                )
            ],
            tools=[],
            model=request.model,
            # Amostragem baixa demais degenera em loop em modelo 3B; a nova tentativa
            # sobe a temperatura e encurta a saída para limitar o custo da inferência.
            temperature=0.35 if repetition_retry else 0.1,
            max_tokens=(
                120 if repetition_retry else min(max(100, request.max_tokens), 300)
            ),
            stream=False,
        )
        logger.info(
            "Visão local em modo percepção: 1 mensagem, sem histórico/ferramentas, imagem <= "
            + str(vision_max_width)
            + "px, saída limitada"
            + (", retomada por repetição." if repetition_retry else ".")
        )
        return compact_request

    def _handoff_local_vision_observation(
        self, messages: list[ChatMessage], observation: str
    ) -> bool:
        """Replace image payloads with a compact textual observation for the main agent."""
        image_indexes = [
            index for index, message in enumerate(messages)
            if _messages_contain_image([message])
        ]
        if not image_indexes:
            return False

        latest_image_index = image_indexes[-1]
        compact_observation = (observation or "").strip()[:1800]
        for index in image_indexes:
            message = messages[index]
            parts = message.content if isinstance(message.content, list) else []
            text_parts = [
                str(part.get("text", "")).strip()
                for part in parts
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
            ]
            content = "\n".join(text_parts).strip()
            if index == latest_image_index:
                content = (
                    f"{content}\nObservação visual objetiva (Qwen2.5-VL local):\n"
                    f"{compact_observation}"
                ).strip()
            else:
                content = f"{content}\n(Captura anterior já analisada localmente.)".strip()
            messages[index] = ChatMessage(role=message.role, content=content)
        logger.info(
            "Observação visual local entregue ao provedor principal; payloads de imagem removidos."
        )
        return True

    def _handoff_local_vision_failure_to_accessibility(
        self, messages: list[ChatMessage], error: ProviderError
    ) -> bool:
        """Remove pixels after local vision failure when structured UIA data is available."""
        if not _has_useful_accessibility_snapshot(messages):
            return False
        image_indexes = [
            index for index, message in enumerate(messages)
            if _messages_contain_image([message])
        ]
        if not image_indexes:
            return False

        note = (
            "A análise de pixels pelo modelo visual local falhou. Use somente os controles "
            "estruturados retornados pela ferramenta capturar_tela; não finja ter visto a "
            "imagem nem invente posições. Se esses dados não forem suficientes para uma ação "
            "segura, informe a limitação."
        )
        for index in image_indexes:
            message = messages[index]
            parts = message.content if isinstance(message.content, list) else []
            text_parts = [
                str(part.get("text", "")).strip()
                for part in parts
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
            ]
            content = "\n".join(text_parts).strip()
            if index == image_indexes[-1]:
                content = (content + "\n" + note).strip()
            elif content:
                content += "\n(Pixels desta captura removidos; use apenas os dados estruturados já registrados.)"
            else:
                content = "(Pixels desta captura indisponíveis; use apenas os dados estruturados já registrados.)"
            messages[index] = ChatMessage(role=message.role, content=content)

        print(
            "[Aurora agente] Visão local falhou; "
            f"HTTP={getattr(error, 'status', None)}; detalhe={str(error)[:220]}. "
            "Removi as imagens e continuarei com os controles UIA disponíveis."
        )
        return True

    def _handoff_local_vision_outage(
        self, messages: list[ChatMessage], error: ProviderError
    ) -> bool:
        """Remove pixels quando o backend visual local está fora do ar.

        Sem Ollama (ou outro endpoint local de visão) não há como descrever a
        imagem. Em vez de derrubar o turno inteiro, o agente perde a imagem e
        continua com os dados de acessibilidade, que é o que o modelo principal
        sabe usar sem inventar elementos.
        """
        image_indexes = [
            index for index, message in enumerate(messages)
            if _messages_contain_image([message])
        ]
        if not image_indexes:
            return False

        note = (
            "A visão local está fora do ar: o modelo visual não pôde ser consultado "
            f"({str(error)[:180]}). Não descreva a imagem nem invente elementos. "
            "Use capturar_tela com somente_acessibilidade=True para obter os controles "
            "estruturados e siga por eles; se não bastarem, explique a limitação ao "
            "usuário e sugira iniciar o Ollama."
        )
        for index in image_indexes:
            message = messages[index]
            parts = message.content if isinstance(message.content, list) else []
            text_parts = [
                str(part.get("text", "")).strip()
                for part in parts
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
            ]
            content = "\n".join(text_parts).strip()
            if index == image_indexes[-1]:
                content = (content + "\n" + note).strip()
            elif content:
                content += "\n(Pixels desta captura removidos; a visão local está indisponível.)"
            else:
                content = "(Pixels desta captura indisponíveis; a visão local está indisponível.)"
            messages[index] = ChatMessage(role=message.role, content=content)

        print(
            "[Aurora agente] Visão local indisponível; "
            f"tipo={getattr(error, 'kind', None)}; detalhe={str(error)[:220]}. "
            "Removi as imagens e continuarei com os controles UIA disponíveis."
        )
        return True

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
        request_provider = self._provider_for_request(request)
        provider_request = self._prepare_provider_request(request, request_provider)

        def request_worker() -> None:
            try:
                try:
                    outcome["response"] = request_provider.chat(provider_request)
                except ProviderError as exc:
                    is_local_vision = (
                        getattr(self, "vision_provider", None) is not None
                        and request_provider is getattr(self, "vision_provider", None)
                        and request_provider is not self.provider
                    )
                    if not is_local_vision:
                        raise

                    # Repetição é degeneração de decodificação: a nova tentativa troca a
                    # amostragem (e encurta a saída) em vez de encolher a imagem. Reduzir
                    # a resolução perderia detalhe e ainda somaria mais duas inferências.
                    if _is_vision_repetition_error(exc):
                        print(
                            "[Aurora agente] Geração repetitiva na visão local; "
                            "tentando novamente com amostragem diferente e saída mais curta."
                        )
                        retry_request = self._prepare_provider_request(
                            request, request_provider, repetition_retry=True
                        )
                        outcome["response"] = request_provider.chat(retry_request)
                        outcome["vision_retry_repetition"] = True
                        return

                    if not _is_vision_token_limit_error(exc):
                        raise

                    last_error = exc
                    for width in (448, 320):
                        smaller_request = self._prepare_provider_request(
                            request, request_provider, vision_max_width=width
                        )
                        print(
                            "[Aurora agente] Limite de tokens/contexto na visão local; "
                            f"tentando novamente com imagem de até {width} px."
                        )
                        try:
                            outcome["response"] = request_provider.chat(smaller_request)
                        except ProviderError as retry_error:
                            if not _is_vision_token_limit_error(retry_error):
                                raise
                            last_error = retry_error
                            continue
                        outcome["vision_retry_downscaled"] = width
                        return
                    raise last_error
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
        print(
            "[Aurora agente] Limites efetivos: "
            f"iterações={self.settings.limits.max_tool_iterations}; "
            f"tempo_da_tarefa={self.settings.limits.max_task_seconds:.0f}s."
        )
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
        # Um nome de ferramenta inválido recebe uma única oportunidade de correção pelo modelo.
        unknown_tool_recovery_used = False
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
        effective_max_output_tokens = self.settings.llm.max_output_tokens

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
                    configured_limit = self.settings.limits.max_tool_iterations
                    print(
                        f"[Aurora agente] Limite de iterações atingido: "
                        f"executadas={iterations}; configurado={configured_limit}."
                    )
                    final_text = (
                        f"Atingi o limite configurado de {configured_limit} iterações com ferramentas sem concluir. "
                        f"Ferramentas usadas: {', '.join(tools_used) or 'nenhuma'}. "
                        "Veja a auditoria para o que já foi executado."
                    )
                    task.error = f"limite de iterações de ferramentas ({configured_limit})"
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
                    max_tokens=effective_max_output_tokens,
                )
                llm_started = time.monotonic()
                is_vision_request = _messages_contain_image(messages)
                uses_local_vision = (
                    is_vision_request and getattr(self, "vision_provider", None) is not None
                )
                configured_timeout = max(
                    1.0,
                    float(
                        self.settings.llm.vision_timeout_s
                        if uses_local_vision
                        else self.settings.llm.timeout_s
                    ),
                )
                max_call_wall_seconds = (
                    _MAX_LOCAL_VISION_CALL_WALL_SECONDS
                    if uses_local_vision
                    else _MAX_LLM_CALL_WALL_SECONDS
                )
                request_timeout = max(
                    15.0,
                    min(
                        max_call_wall_seconds,
                        configured_timeout + _LLM_CALL_TIMEOUT_PADDING_SECONDS,
                    ),
                )
                request_deadline = min(deadline, llm_started + request_timeout)
                llm_wait_started = time.monotonic()
                print(
                    f"[Aurora agente] Chamando modelo na iteração {iterations}; "
                    f"modelo={request.model}; imagem={is_vision_request}; "
                    f"provedor={'visao-local' if uses_local_vision else 'principal'}; "
                    f"prazo_de_espera={request_deadline - llm_wait_started:.1f}s."
                )
                try:
                    response = self._chat_with_cancel(request, deadline=request_deadline)
                except ProviderError as call_error:
                    if uses_local_vision:
                        if self._handoff_local_vision_failure_to_accessibility(messages, call_error):
                            continue
                        if _is_vision_backend_unreachable(call_error) and (
                            self._handoff_local_vision_outage(messages, call_error)
                        ):
                            continue
                        raise
                    if not _is_token_limit_error(call_error):
                        raise
                    recovery_error = call_error
                    recovered = False
                    configured_context = max(3000, int(self.settings.llm.context_window))
                    recovery_budgets = (
                        (min(10000, max(4000, configured_context - 1000)), False),
                        (min(6500, max(3000, configured_context - 1500)), True),
                    )
                    for recovery_index, (budget, aggressive) in enumerate(recovery_budgets, start=1):
                        compacted_messages = _compact_messages_for_token_retry(
                            messages, text, tools, budget=budget, aggressive=aggressive
                        )
                        retry_tools = _compact_tool_schemas(tools) if aggressive else tools
                        estimated = _estimate_request_tokens(compacted_messages, retry_tools)
                        if compacted_messages == messages and retry_tools is tools:
                            continue
                        messages[:] = compacted_messages
                        retry_max_tokens = min(
                            effective_max_output_tokens, 300 if aggressive else 500
                        )
                        retry_request = LLMRequest(
                            messages=messages,
                            tools=retry_tools,
                            model=request.model,
                            temperature=request.temperature,
                            max_tokens=retry_max_tokens,
                            stream=False,
                        )
                        print(
                            "[Aurora agente] Limite de tokens detectado; "
                            f"tentativa de recuperação {recovery_index}/2; "
                            f"contexto_estimado={estimated}; orçamento={budget}; "
                            f"schemas_compactados={aggressive}."
                        )
                        try:
                            response = self._chat_with_cancel(
                                retry_request, deadline=request_deadline
                            )
                        except ProviderError as retry_error:
                            if not _is_token_limit_error(retry_error):
                                raise
                            recovery_error = retry_error
                            continue
                        else:
                            tools = retry_tools
                            effective_max_output_tokens = retry_max_tokens
                            recovered = True
                            print(
                                "[Aurora agente] Recuperação do limite de tokens concluída; "
                                "o turno continuará com contexto compacto."
                            )
                            break
                    if not recovered:
                        raise recovery_error
                print(
                    f"[Aurora agente] Modelo respondeu em {time.monotonic() - llm_wait_started:.1f}s; "
                    f"tool_calls={len(response.tool_calls) if response is not None else 0}; "
                    f"texto={len(response.text or '') if response is not None else 0} caracteres."
                )
                if response is None or self.cancel_event.is_set():
                    final_text = "Tarefa cancelada a pedido do usuário."
                    task.error = "cancelada pelo usuário"
                    if task.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}:
                        task.transition(TaskStatus.CANCELLED)
                    break
                model_used = response.model or model_used
                _accumulate_usage(usage, response.usage)

                if uses_local_vision:
                    # O modelo visual é apenas percepção: não tem schemas de ferramentas.
                    # Encaminhe suas observações ao agente principal, que decide ações reais.
                    observation = (response.text or "").strip()
                    if observation and self._handoff_local_vision_observation(messages, observation):
                        print(
                            "[Aurora agente] Percepção visual local encaminhada ao provedor principal; "
                            "continuando o ciclo de ferramentas."
                        )
                        continue
                    final_text = (
                        "O modelo visual local não retornou uma observação utilizável da captura."
                    )
                    task.error = final_text
                    task.transition(TaskStatus.FAILED)
                    break

                if not response.tool_calls:
                    final_text = response.text.strip() or "(o modelo não devolveu texto)"
                    task.transition(TaskStatus.COMPLETED)
                    break

                available_tool_names = {
                    spec.name for spec in self.registry.specs(self.settings.enabled_tools)
                }
                unknown_tool_names = _unknown_tool_names(
                    response.tool_calls, available_tool_names
                )
                if unknown_tool_names:
                    # Não execute nenhuma ação de um lote que contém ferramenta inválida.
                    # Cada tool_call_id recebe um resultado para manter o protocolo do provedor.
                    assistant = ChatMessage(
                        role="assistant",
                        content=response.text,
                        tool_calls=list(response.tool_calls),
                    )
                    messages.append(assistant)
                    unknown_tool_recovery_used = unknown_tool_recovery_used
                    retry_allowed = not unknown_tool_recovery_used
                    unknown_tool_recovery_used = True
                    available_text = ", ".join(sorted(available_tool_names)) or "(nenhuma)"
                    for call in response.tool_calls:
                        tool_call_count += 1
                        tools_used.append(call.name)
                        if call.name in unknown_tool_names:
                            self.audit.record(
                                "tool.unknown",
                                task_id=task.id,
                                tool=call.name,
                                available_tools=sorted(available_tool_names),
                                recovery_attempt=1 if retry_allowed else 2,
                            )
                            if retry_allowed:
                                detail = (
                                    f"A ferramenta '{call.name}' não está disponível e NÃO foi executada. "
                                    f"Use exatamente um dos nomes registrados: {available_text}. "
                                    "Corrija a chamada usando o nome exato e os argumentos do esquema. "
                                    "Não invente variantes nem tente executar a ação por outro caminho "
                                    "sem reavaliar a solicitação."
                                )
                            else:
                                detail = (
                                    f"A ferramenta '{call.name}' continua inválida após a única tentativa "
                                    f"de correção. Nenhuma ação deste lote foi executada. "
                                    f"Nomes disponíveis: {available_text}."
                                )
                            result = ToolResult.fail(
                                detail,
                                ferramenta_desconhecida=call.name,
                                ferramentas_disponiveis=sorted(available_tool_names),
                            )
                            tool_message_name = ""
                        else:
                            detail = (
                                "AÇÃO NÃO EXECUTADA: a resposta continha outra ferramenta com nome inválido. "
                                "Nenhuma ação deste lote foi executada. Reenvie somente chamadas com nomes "
                                "exatamente iguais aos esquemas de ferramenta disponíveis."
                            )
                            result = ToolResult.fail(detail, lote_cancelado=True)
                            tool_message_name = call.name
                            self.audit.record(
                                "tool.batch_aborted",
                                task_id=task.id,
                                tool=call.name,
                                reason="a mesma resposta continha nome de ferramenta desconhecido",
                            )
                        messages.append(
                            ChatMessage(
                                role="tool",
                                content=result.to_content(self.settings.limits.max_output_chars),
                                tool_call_id=call.id,
                                name=tool_message_name,
                            )
                        )

                    if retry_allowed:
                        print(
                            "[Aurora agente] Nome de ferramenta desconhecido; "
                            "nenhuma ação foi executada. Solicitando uma única correção ao modelo."
                        )
                        continue

                    final_text = (
                        "Interrompi a tarefa porque o modelo voltou a solicitar uma ferramenta "
                        f"desconhecida ({', '.join(unknown_tool_names)}). "
                        "Nenhuma ação desse lote foi executada. "
                        f"Ferramentas disponíveis: {available_text}."
                    )
                    task.error = final_text
                    if task.status not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}:
                        task.transition(TaskStatus.FAILED)
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
                    "capturar_tela", "controlar_mouse", "controlar_teclado", "pesquisar",
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
                    print(
                        f"[Aurora agente] Ferramenta solicitada: {call.name}; "
                        f"campos={','.join(sorted(call.arguments)) or 'nenhum'}."
                    )
                    deferred = (
                        call.name in desktop_tool_names
                        and desktop_batch_requires_serial
                        and call.name != preferred_desktop_tool
                    )
                    tool_elapsed_ms = 0
                    if deferred:
                        result = ToolResult.fail(
                            "AÇÃO NÃO EXECUTADA: etapa adiada para manter o ciclo visual "
                            "capturar/analisar/agir/verificar em sequência. Esta chamada não foi executada; "
                            "reavalie a ordem das etapas e solicite-a novamente somente quando for a próxima ação.",
                            adiada=True,
                        )
                        fatal = False
                    else:
                        tool_started = time.perf_counter()
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
                        tool_elapsed_ms = round((time.perf_counter() - tool_started) * 1000)
                    print(
                        f"[Aurora agente] Ferramenta concluída: {call.name}; "
                        f"ok={result.ok}; duração={tool_elapsed_ms}ms; "
                        f"resumo={str(result.summary)[:180]}"
                    )
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
                        # Caminho rápido: para teclas e cliques, tenta primeiro a UI Automation
                        # sem codificar uma imagem. A ferramenta cai automaticamente para
                        # captura visual se o UIA não devolver controles úteis.
                        fast_verification = _should_use_accessibility_only_verification(
                            call.name, call.arguments
                        )
                        time.sleep(_verification_settle_seconds(call.name, call.arguments))
                        capture_args = {"monitor": 1, "largura_maxima": 640}
                        if fast_verification:
                            capture_args["somente_acessibilidade"] = True
                        else:
                            # A próxima decisão já vai usar a imagem localmente; evite gastar
                            # mais tempo percorrendo UIA em paralelo à captura de pixels.
                            capture_args["somente_imagem"] = True
                        capture_call = ToolCall(
                            id=f"auto-capture-{call.id}",
                            name="capturar_tela",
                            arguments=capture_args,
                        )
                        verification_started = time.perf_counter()
                        capture_result, _ = self._run_tool(capture_call, task, ctx)
                        verification_elapsed_ms = round((time.perf_counter() - verification_started) * 1000)
                        tool_call_count += 1
                        tools_used.append("capturar_tela")
                        image_url = capture_result.data.pop("data_url", "") if capture_result.ok else ""
                        ui_snapshot = capture_result.data.get("interface_acessivel", {}) if capture_result.ok else {}
                        ui_elements = ui_snapshot.get("elementos", []) if isinstance(ui_snapshot, dict) else []
                        if isinstance(image_url, str) and image_url.startswith("data:image/"):
                            vision_attachments.append((
                                image_url,
                                f"Captura automática após ação {call.name}. Examine a imagem para recalcular o próximo passo; "
                                f"o sucesso da chamada não significa, por si só, que o objetivo final foi concluído. {capture_result.summary}",
                            ))
                            verification_kind = "imagem"
                        elif (
                            capture_result.ok
                            and isinstance(ui_snapshot, dict)
                            and ui_snapshot.get("disponivel")
                            and isinstance(ui_elements, list)
                            and ui_elements
                        ):
                            # Observação textual estruturada: mantém a verificação, mas evita
                            # pagar uma inferência de visão local em ações que UIA descreve.
                            element_fields = (
                                "tipo", "nome", "centro_x", "centro_y", "esquerda", "topo",
                                "direita", "base", "id_automacao", "ajuda", "pontuacao_busca",
                                "confianca_busca", "evidencias_busca",
                            )
                            compact_elements = [
                                {key: item[key] for key in element_fields if key in item}
                                for item in ui_elements[:25] if isinstance(item, dict)
                            ]
                            observation = {
                                "janela_ativa": ui_snapshot.get("janela_ativa", ""),
                                "retangulo_janela": ui_snapshot.get("retangulo_janela", {}),
                                "total_elementos_lidos": ui_snapshot.get("total_elementos_lidos", len(ui_elements)),
                                "elementos": compact_elements,
                                "resumo_pesquisa": ui_snapshot.get("resumo_pesquisa", [])[:8],
                                "nota_coordenadas": ui_snapshot.get("nota_coordenadas", ""),
                                "area_trabalho_virtual": capture_result.data.get("area_trabalho_virtual"),
                                "tempo_uia_ms": capture_result.data.get("tempo_uia_ms"),
                                "tempo_total_ms": capture_result.data.get("tempo_total_ms"),
                            }
                            messages.append(ChatMessage(
                                role="user",
                                content=(
                                    "Verificação rápida após a ação via UI Automation (sem imagem). "
                                    "Use os controles e seus limites como evidência estruturada. "
                                    "Se o resultado exigir interpretar ícones, layout ou conteúdo não acessível, "
                                    "peça capturar_tela antes de agir novamente. Dados: "
                                    + json.dumps(observation, ensure_ascii=False, default=str)
                                ),
                            ))
                            verification_kind = f"UIA/{len(compact_elements)} controles"
                            print(
                                "[Aurora agente] Verificação rápida concluída via UI Automation; "
                                "imagem e modelo visual local omitidos."
                            )
                        else:
                            detail = capture_result.error or capture_result.summary or "a captura não retornou evidência válida"
                            vision_warnings.append(
                                f"A ação {call.name} foi executada, mas a verificação automática falhou: {detail}. "
                                "Não execute outro clique/digitação às cegas; tente capturar_tela como próxima etapa."
                            )
                            verification_kind = "falha"
                        print(
                            f"[Aurora agente] Verificação pós-ação: tipo={verification_kind}; "
                            f"duração={verification_elapsed_ms}ms; "
                            f"captura={capture_result.data.get('tempo_captura_ms', '?')}ms; "
                            f"UIA={capture_result.data.get('tempo_uia_ms', '?')}ms."
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
            print(f"[Aurora agente] Timeout de modelo/tarefa após {elapsed_s:.1f}s: {exc}")
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
            print(
                f"[Aurora agente] Detalhes da falha do provedor: tipo={exc.kind}; "
                f"HTTP={getattr(exc, 'status', None)}; detalhe={str(exc)[:260]}"
            )
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

        print(
            f"[Aurora agente] Turno finalizado: status={task.status.value}; "
            f"duração={latency_ms / 1000:.1f}s; ferramentas={','.join(tools_used) or 'nenhuma'}."
        )
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

"""Montagem do runtime: liga configuração, banco, ferramentas, agente e voz."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from aurora.config.permissions import PermissionPolicy, Risk
from aurora.config.settings import ConfigError, Settings
from aurora.core.agent import AuroraAgent
from aurora.core.context_manager import Summarizer
from aurora.core.events import EventBus
from aurora.core.metrics import Metrics
from aurora.core.orchestrator import Orchestrator
from aurora.core.planner import Planner
from aurora.core.task_state import TaskStore
from aurora.llm.freellm_client import FreeLLMClient
from aurora.llm.model_router import ModelRouter
from aurora.memory.conversation_store import ConversationStore
from aurora.memory.long_term import LongTermMemory
from aurora.observability import get_logger
from aurora.security.approvals import Approver, ConsoleApprover
from aurora.security.audit import AuditLog
from aurora.security.policy import SecurityPolicy
from aurora.storage.database import Database
from aurora.tools import ToolRegistry, build_registry
from aurora.voice.output import SpeechChannel
from aurora.voice.tts_engine import create_tts

logger = get_logger(__name__)


@dataclass
class Runtime:
    settings: Settings
    db: Database
    metrics: Metrics
    events: EventBus
    registry: ToolRegistry
    policy: PermissionPolicy
    security: SecurityPolicy
    approver: Approver
    audit: AuditLog
    memory: LongTermMemory
    conversations: ConversationStore
    tasks: TaskStore
    provider: FreeLLMClient | None
    vision_provider: FreeLLMClient | None
    router: ModelRouter
    agent: AuroraAgent | None
    planner: Planner | None
    orchestrator: Orchestrator | None
    summarizer: Summarizer | None
    speech: SpeechChannel | None

    def close(self) -> None:
        if self.speech:
            self.speech.close()
        if self.provider:
            self.provider.close()
        if self.vision_provider and self.vision_provider is not self.provider:
            self.vision_provider.close()
        self.db.close()


def _metrics_recorder(metrics: Metrics) -> Callable[[dict[str, Any]], None]:
    def record(payload: dict[str, Any]) -> None:
        if payload.get("kind") == "llm_chat":
            metrics.record(
                "llm_chat",
                model=str(payload.get("model") or ""),
                value=float(payload.get("latency_ms") or 0),
                prompt_tokens=payload.get("prompt_tokens", 0),
                completion_tokens=payload.get("completion_tokens", 0),
                total_tokens=payload.get("total_tokens", 0),
                estimated=payload.get("estimated", False),
                tool_calls=payload.get("tool_calls", 0),
            )
        elif payload.get("kind") == "llm_http":
            metrics.record(
                "llm_http",
                model="",
                value=float(payload.get("latency_ms") or 0),
                status=payload.get("status"),
                error=payload.get("error", ""),
                path=payload.get("path", ""),
            )

    return record


def build_runtime(
    settings: Settings,
    *,
    approver: Approver | None = None,
    voice: bool = True,
    provider_override: Any | None = None,
) -> Runtime:
    settings.ensure_directories()
    db = Database(settings.paths.db_path)
    db.migrate()
    metrics = Metrics(db)
    policy = PermissionPolicy(allowed_roots=settings.paths.allowed_roots)
    security = SecurityPolicy(
        auto_approve_max_risk=Risk.parse(settings.auto_approve_max_risk, Risk.L1),
        enabled_tools=settings.enabled_tools,
    )
    audit = AuditLog(settings.paths.audit_path)
    events = EventBus()
    registry = build_registry()
    memory = LongTermMemory(db)
    conversations = ConversationStore(db)
    tasks = TaskStore(db)
    router = ModelRouter.from_settings(settings.llm)

    provider: FreeLLMClient | None = None
    vision_provider: FreeLLMClient | None = None
    if provider_override is not None:
        provider = provider_override
    elif settings.has_api_key():
        try:
            provider = FreeLLMClient(
                settings.llm.base_url,
                settings.api_key(),
                timeout_s=settings.llm.timeout_s,
                max_retries=settings.llm.max_retries,
                on_metrics=_metrics_recorder(metrics),
            )
        except ConfigError:
            provider = None

    # O endpoint de visão é independente para manter chat/planner/summarizer
    # no provedor existente e enviar imagens somente ao backend local configurado.
    if provider_override is None and settings.llm.vision_base_url:
        vision_provider = FreeLLMClient(
            settings.llm.vision_base_url,
            "ollama-local",
            timeout_s=settings.llm.vision_timeout_s,
            max_retries=settings.llm.vision_max_retries,
            on_metrics=_metrics_recorder(metrics),
        )
        logger.info(
            "Provedor local de visão configurado: %s (modelo=%s)",
            settings.llm.vision_base_url,
            settings.llm.vision_model or settings.llm.model,
        )

    agent = None
    planner = None
    orchestrator = None
    summarizer = None
    if provider is not None:
        agent = AuroraAgent(
            settings=settings,
            provider=provider,
            registry=registry,
            router=router,
            memory=memory,
            conversations=conversations,
            tasks=tasks,
            audit=audit,
            security=security,
            approver=approver or ConsoleApprover(),
            events=events,
            metrics=metrics,
            policy=policy,
            vision_provider=vision_provider,
        )
        planner = Planner(
            provider,
            registry,
            model=router.model_for("planner"),
            max_steps=settings.limits.max_plan_steps,
        )
        orchestrator = Orchestrator(
            settings=settings,
            registry=registry,
            security=security,
            approver=approver or ConsoleApprover(),
            audit=audit,
            tasks=tasks,
            memory=memory,
            events=events,
            policy=policy,
        )
        summarizer = Summarizer(provider, model=router.model_for("utility"))

    speech = None
    if voice:
        engine = create_tts(settings.voice, work_dir=settings.paths.data_dir / "voice")
        speech = SpeechChannel(engine, max_chars=settings.voice.max_chars)

    events.subscribe(
        "*",
        lambda event: logger.debug(
            "evento %s", event.name, extra={"aurora": {"event": event.name, **event.payload}}
        ),
    )

    return Runtime(
        settings=settings,
        db=db,
        metrics=metrics,
        events=events,
        registry=registry,
        policy=policy,
        security=security,
        approver=approver or ConsoleApprover(),
        audit=audit,
        memory=memory,
        conversations=conversations,
        tasks=tasks,
        provider=provider,
        vision_provider=vision_provider,
        router=router,
        agent=agent,
        planner=planner,
        orchestrator=orchestrator,
        summarizer=summarizer,
        speech=speech,
    )

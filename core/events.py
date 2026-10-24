"""Barramento de eventos simples (síncrono).

Handlers com erro são registrados e ignorados para não derrubar o turno.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from aurora.observability import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Event:
    name: str
    ts: str
    payload: dict[str, Any] = field(default_factory=dict)


class EventBus:
    def __init__(self, history: int = 100) -> None:
        self._subscribers: dict[str, list[Callable[[Event], None]]] = {}
        self._history: deque[Event] = deque(maxlen=history)

    def subscribe(self, name: str, handler: Callable[[Event], None]) -> None:
        self._subscribers.setdefault(name, []).append(handler)

    def emit(self, name: str, **payload: Any) -> Event:
        event = Event(
            name=name,
            ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            payload=payload,
        )
        self._history.append(event)
        for handler in [*self._subscribers.get(name, []), *self._subscribers.get("*", [])]:
            try:
                handler(event)
            except Exception:
                logger.exception("handler de evento falhou: %s", name)
        return event

    def recent(self, limit: int = 20) -> list[Event]:
        return list(self._history)[-limit:]

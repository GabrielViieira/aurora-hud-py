"""Proactive conversation loop: speak-only nudges, never tool/action execution."""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import Callable

from aurora.core.observers import CheapObservers, ObserverSnapshot
from aurora.llm.provider import ChatMessage, LLMRequest

_PAIR_RE = re.compile(r"\[PT\](.*?)\[JA\](.*)", re.IGNORECASE | re.DOTALL)


def _extract_pair(text: str) -> tuple[str, str]:
    match = _PAIR_RE.search(text or "")
    if not match:
        return "", ""
    return match.group(1).strip(), match.group(2).strip()


class ProactiveLoop:
    """Low-cost observer + chat-model nudge generator, with cooldown and busy gates."""
    def __init__(
        self,
        *,
        provider,
        model_for_chat: Callable[[], str],
        speak_callback: Callable[[str, str], None],
        busy_callback: Callable[[], bool],
        observers: CheapObservers | None = None,
        interval_seconds: float = 45.0,
        cooldown_seconds: float = 1800.0,
        game_milestone_seconds: float = 3 * 60 * 60,
        quiet_start_hour: int = 23,
        quiet_end_hour: int = 8,
    ) -> None:
        self.provider = provider
        self.model_for_chat = model_for_chat
        self.speak_callback = speak_callback
        self.busy_callback = busy_callback
        self.observers = observers or CheapObservers()
        self.interval_seconds = max(15.0, interval_seconds)
        self.cooldown_seconds = max(300.0, cooldown_seconds)
        self.game_milestone_seconds = max(60.0, game_milestone_seconds)
        self.quiet_start_hour = quiet_start_hour
        self.quiet_end_hour = quiet_end_hour
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_nudge = 0.0
        self._game_milestone_sent = False
        self._last_download_notice = ""
        self._pending_download_notice = ""
        self._pending_game_milestone = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="AuroraProactiveLoop")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def _quiet_hours(self, local_now: datetime) -> bool:
        hour = local_now.hour
        if self.quiet_start_hour == self.quiet_end_hour:
            return False
        if self.quiet_start_hour > self.quiet_end_hour:
            return hour >= self.quiet_start_hour or hour < self.quiet_end_hour
        return self.quiet_start_hour <= hour < self.quiet_end_hour

    def _choose_event(self, snapshot: ObserverSnapshot) -> str:
        if snapshot.download_completed:
            self._pending_download_notice = snapshot.download_completed
        if snapshot.is_game and snapshot.game_seconds >= self.game_milestone_seconds:
            self._pending_game_milestone = True
        if self._pending_download_notice:
            notice = self._pending_download_notice
            self._pending_download_notice = ""
            if notice != self._last_download_notice:
                self._last_download_notice = notice
                return "download_completed"
        if self._pending_game_milestone and not self._game_milestone_sent:
            self._pending_game_milestone = False
            self._game_milestone_sent = True
            return "game_milestone"
        return ""

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                snapshot = self.observers.snapshot()
                if snapshot.download_completed:
                    self._pending_download_notice = snapshot.download_completed
                if snapshot.is_game and snapshot.game_seconds >= self.game_milestone_seconds:
                    self._pending_game_milestone = True
                if self._quiet_hours(datetime.now()) or self._stop.is_set():
                    continue
                now = time.monotonic()
                if now - self._last_nudge < self.cooldown_seconds or self.busy_callback():
                    continue
                event = self._choose_event(snapshot)
                if not event:
                    continue
                self._last_nudge = now
                self._generate_and_speak(event, snapshot)
            except Exception as exc:
                print(f"[Aurora proativa] Observer/loop falhou: {type(exc).__name__}: {exc}")

    def _generate_and_speak(self, event: str, snapshot: ObserverSnapshot) -> None:
        event_pt = {
            "game_milestone": "A pessoa está há cerca de três horas em uma janela reconhecida como jogo.",
            "download_completed": "Um arquivo parece ter terminado de baixar.",
        }.get(event, "Uma observação local.")
        system = (
            "Você é Aurora, uma companheira conversacional. Gere apenas uma fala curta e natural, "
            "sem realizar ações, sem chamar ferramentas, sem abrir arquivos, sem clicar e sem pedir "
            "permissão para executar nada. Seja respeitosa, não interrompa com insistência. "
            "Responda estritamente no formato [PT]frase em português brasileiro [JA]frase em japonês. "
            "No máximo uma frase em cada idioma; não inclua explicações."
        )
        user = (
            f"Janela em foco: {(snapshot.active_window if snapshot.is_game else 'título omitido por privacidade') or 'não identificada'}. "
            f"Hora local: {snapshot.local_time}. Tempo acumulado de jogo: "
            f"{int(snapshot.game_seconds // 60)} minutos. Crie uma fala breve, contextual e não invasiva."
        )
        request = LLMRequest(
            messages=[ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)],
            tools=[],
            model=self.model_for_chat(),
            temperature=0.65,
            max_tokens=96,
            stream=False,
        )
        response = self.provider.chat(request)
        pt, ja = _extract_pair(getattr(response, "text", "") or "")
        if pt and ja:
            if not self.busy_callback() and not self._stop.is_set():
                self.speak_callback(pt, ja)
        else:
            print("[Aurora proativa] Modelo não retornou par PT/JA válido; nudge descartado.")

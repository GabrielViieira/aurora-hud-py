"""Estado da conversa contínua por voz e reconhecimento do comando de descanso."""
from __future__ import annotations

import re
import unicodedata


MIN_CONVERSATION_TIMEOUT_SECONDS = 15
MAX_CONVERSATION_TIMEOUT_SECONDS = 600
DEFAULT_CONVERSATION_TIMEOUT_SECONDS = 60


def _bounded_timeout(seconds: float) -> int:
    try:
        value = int(float(seconds))
    except (TypeError, ValueError, OverflowError):
        value = DEFAULT_CONVERSATION_TIMEOUT_SECONDS
    return max(MIN_CONVERSATION_TIMEOUT_SECONDS, min(MAX_CONVERSATION_TIMEOUT_SECONDS, value))


def normalize_voice_command(text: str) -> str:
    """Normaliza pontuação e acentos para comparar comandos falados."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    words = re.sub(r"[^a-z0-9\s]", " ", without_marks)
    return re.sub(r"\s+", " ", words).strip()


_END_COMMANDS = {
    "pode descansar", "pode descansar agora", "descansa", "descanse",
    "pode dormir", "pode dormir agora", "volta a dormir", "volte a dormir",
    "encerra conversa", "encerra a conversa", "encerrar conversa", "encerrar a conversa",
    "finaliza conversa", "finaliza a conversa", "finalizar conversa", "finalizar a conversa",
    "termina conversa", "termina a conversa", "terminar conversa", "terminar a conversa",
    "fecha conversa", "fecha a conversa",
    "sair do modo conversa", "sair do modo de conversa",
    "sai do modo conversa", "sai do modo de conversa",
    "volta a espera", "volta para espera", "volta para o modo de espera",
    "volta ao modo de espera", "retorna para espera",
    "desativa a escuta", "desativar a escuta", "desliga a escuta", "desligar a escuta",
    "encerrar sessao", "encerra sessao", "encerra a sessao",
}


def is_conversation_end_command(text: str, wakeword: str = "aurora") -> bool:
    """Retorna True apenas para frases curtas e explícitas de encerramento."""
    command = normalize_voice_command(text)
    wake = normalize_voice_command(wakeword)
    if wake and command.startswith(wake + " "):
        command = command[len(wake):].strip()
    command = re.sub(r"\s+(?:por favor|agora)$", "", command).strip()
    return command in _END_COMMANDS


class VoiceConversationSession:
    """Mantém uma janela de conversa que é renovada a cada fala do usuário."""

    def __init__(self, timeout_seconds: float = DEFAULT_CONVERSATION_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = _bounded_timeout(timeout_seconds)
        self._expires_at = 0.0
        self._last_activity_at: float | None = None

    @property
    def is_active(self) -> bool:
        return self._expires_at > 0.0

    def set_timeout(self, timeout_seconds: float) -> None:
        self.timeout_seconds = _bounded_timeout(timeout_seconds)
        if self.is_active and self._last_activity_at is not None:
            self._expires_at = self._last_activity_at + self.timeout_seconds

    def activate(self, now: float) -> None:
        self._last_activity_at = float(now)
        self._expires_at = self._last_activity_at + self.timeout_seconds

    def refresh(self, now: float) -> None:
        if self.is_active:
            self.activate(now)

    def expire_if_needed(self, now: float) -> bool:
        if self.is_active and float(now) >= self._expires_at:
            self.end()
            return True
        return False

    def end(self) -> bool:
        was_active = self.is_active
        self._expires_at = 0.0
        self._last_activity_at = None
        return was_active

    def seconds_remaining(self, now: float) -> int:
        if not self.is_active:
            return 0
        return max(0, int(self._expires_at - float(now)))

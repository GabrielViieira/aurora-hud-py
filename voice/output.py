"""Canal de voz não bloqueante com fila e interrupção."""

from __future__ import annotations

import json
import os
import queue
import re
import threading

from aurora.observability import get_logger
from aurora.voice.tts_engine import NullTts, TtsEngine, TtsUnavailable

logger = get_logger(__name__)

_MARKDOWN_RE = re.compile(r"[`*_#>\[\]\(\)]")
_JA_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]")
_JA_KANA_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF]")

try:
    import httpx

    HTTPX_AVAILABLE = True
except Exception:
    httpx = None
    HTTPX_AVAILABLE = False

FREELLM_URL = os.environ.get("AURORA_FREELLM_URL", "http://localhost:3001")
_TRANSLATION_CACHE: dict[str, str] = {}


def _translate_to_japanese(text: str) -> str:
    if not text or not text.strip():
        return text
    if _JA_KANA_RE.search(text):
        return text
    key = text.strip()
    if key in _TRANSLATION_CACHE:
        cached = _TRANSLATION_CACHE[key]
        if _JA_KANA_RE.search(cached):
            return cached
        _TRANSLATION_CACHE.pop(key, None)
    if not HTTPX_AVAILABLE:
        logger.warning("TTS japonês silenciado: pacote httpx não está disponível para traduzir.")
        return ""
    try:
        payload = {
            "model": os.environ.get("AURORA_TTS_TRANSLATION_MODEL", "auto").strip() or "auto",
            "messages": [
                {
                    "role": "system",
                    "content": "Traduza para japonês natural, falado e adequado para síntese de voz (TTS). Responda APENAS com o texto em japonês. Não use romaji, não explique, não coloque aspas.",
                },
                {"role": "user", "content": key},
            ],
            "temperature": 0.2,
            "max_tokens": 256,
        }
        resp = httpx.post(f"{FREELLM_URL}/v1/chat/completions", json=payload, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            if content:
                translated = str(content).strip().strip('"').strip("'")
                if translated and _JA_KANA_RE.search(translated):
                    _TRANSLATION_CACHE[key] = translated
                    return translated
            logger.warning("TTS japonês: o gateway não retornou uma tradução japonesa válida (HTTP %s).", resp.status_code)
    except Exception as exc:
        logger.warning("falha ao traduzir para japonês: %s", exc)
    logger.warning("TTS japonês silenciado: tradução inválida; não será lido o texto original em português.")
    return ""


def text_for_speech(text: str, max_chars: int = 400) -> str:
    """Limpa markdown/código para uma fala curta e natural."""
    cleaned = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    cleaned = _MARKDOWN_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rsplit(" ", 1)[0] + "…"
    return cleaned


class SpeechChannel:
    """Fila de fala em thread própria; o turno nunca espera o áudio."""

    def __init__(self, engine: TtsEngine, *, max_chars: int = 400) -> None:
        self.engine = engine
        self.max_chars = max_chars
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=8)
        self._thread = threading.Thread(target=self._worker, name="aurora-voice", daemon=True)
        self._thread.start()
        self._busy = threading.Event()
        self.last_error = ""
        self.spoken = 0

    @property
    def enabled(self) -> bool:
        return not isinstance(self.engine, NullTts)

    @property
    def backend(self) -> str:
        return self.engine.name

    def say(self, text: str) -> bool:
        speech = text_for_speech(text, self.max_chars)
        if not speech or not self.enabled:
            return False
        speech = _translate_to_japanese(speech)
        if not speech or not _JA_KANA_RE.search(speech):
            return False
        try:
            self._queue.put_nowait(speech)
            return True
        except queue.Full:
            logger.warning("fila de voz cheia; fala descartada")
            return False

    def stop(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        try:
            self.engine.stop()
        except Exception:
            logger.debug("falha ao interromper voz", exc_info=True)

    def close(self) -> None:
        self.stop()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self.engine.close()

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            self._busy.set()
            try:
                self.engine.speak(item)
                self.spoken += 1
            except TtsUnavailable as exc:
                self.last_error = str(exc)
                logger.warning("TTS falhou; seguindo em texto: %s", exc)
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("erro inesperado no TTS")
            finally:
                self._busy.clear()
                self._queue.task_done()

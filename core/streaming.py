"""Primitivas leves para segmentar texto gerado para fala incremental."""
from __future__ import annotations

import queue
import re
import threading


_BOUNDARY_RE = re.compile(r"(?:[.!?。！？…]+["'’”)\]]*\s+|\n+)")


class SentenceSegmenter:
    """Agrupa fragmentos de texto em segmentos de fala de tamanho moderado."""

    def __init__(self, *, max_chars: int = 140) -> None:
        if max_chars < 20:
            raise ValueError("max_chars deve ser pelo menos 20")
        self.max_chars = max_chars
        self._buffer = ""

    @property
    def buffered_text(self) -> str:
        return self._buffer

    def feed(self, fragment: str) -> list[str]:
        if fragment:
            self._buffer += str(fragment)
        return self._take_ready_segments()

    def flush(self) -> list[str]:
        """Libera o resto do texto no fim da resposta."""
        ready = self._take_ready_segments(force=True)
        tail = self._buffer.strip()
        self._buffer = ""
        if tail:
            ready.append(tail)
        return ready

    def reset(self) -> None:
        self._buffer = ""

    def _take_ready_segments(self, *, force: bool = False) -> list[str]:
        ready: list[str] = []
        while self._buffer:
            boundary = _BOUNDARY_RE.search(self._buffer)
            if boundary:
                segment = self._buffer[:boundary.end()].strip()
                self._buffer = self._buffer[boundary.end():].lstrip()
                if segment:
                    ready.append(segment)
                continue

            if len(self._buffer) < self.max_chars and not force:
                break

            # Evite cortar no meio de uma palavra. Se o modelo gerar uma frase
            # sem pontuação, ainda assim libere partes para que a fala não espere.
            split_at = self._buffer.rfind(" ", 0, self.max_chars + 1)
            if split_at < max(12, self.max_chars // 3):
                split_at = self._buffer.find(" ", self.max_chars, self.max_chars + 32)
            if split_at <= 0:
                if not force and len(self._buffer) < self.max_chars + 32:
                    break
                split_at = min(len(self._buffer), self.max_chars)

            segment = self._buffer[:split_at].strip()
            self._buffer = self._buffer[split_at:].lstrip()
            if segment:
                ready.append(segment)

        return ready


class StreamingSpeechSession:
    """Estado thread-safe de uma resposta cuja fala é produzida por segmentos."""

    def __init__(self, token: int) -> None:
        self.token = token
        self.segmenter = SentenceSegmenter()
        self.segments: queue.Queue[str | None] = queue.Queue()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.generation: int | None = None
        self.started = False
        self.closed = False
        self.cancelled = False
        self.final_state = "completed"

    @property
    def is_accepting(self) -> bool:
        with self.lock:
            return not self.closed and not self.cancelled and not self.stop_event.is_set()

    def feed(self, fragment: str) -> list[str]:
        with self.lock:
            if self.closed or self.cancelled or self.stop_event.is_set():
                return []
            return self.segmenter.feed(fragment)

    def finish(self, final_state: str = "completed") -> list[str]:
        with self.lock:
            if self.closed or self.cancelled or self.stop_event.is_set():
                return []
            self.closed = True
            self.final_state = final_state
            return self.segmenter.flush()

    def cancel(self) -> None:
        with self.lock:
            self.cancelled = True
            self.closed = True
            self.stop_event.set()
            self.segmenter.reset()
        # Queue ilimitada: o sentinel sempre consegue acordar o consumidor.
        self.segments.put(None)

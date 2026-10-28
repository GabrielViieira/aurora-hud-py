"""Primitivas leves para segmentar texto gerado para fala incremental.

O segmento só é liberado em um limite de frase/linha ou quando excede o tamanho
máximo; isso evita enviar fragmentos de tokens pequenos demais ao TTS.
"""
from __future__ import annotations

import re


_BOUNDARY_RE = re.compile(r"(?:[.!?。！？…]+\s+|\n+)")


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

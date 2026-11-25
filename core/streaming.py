"""Lightweight text segmentation and bilingual streaming primitives for HUD speech."""
from __future__ import annotations

import queue
import re
import threading

_BOUNDARY_RE = re.compile(r"""(?:[.!?。！？…]+["'’”)]]*\s+|\n+)""")
_MARKER_RE = re.compile(r"\[(PT|JA)\]", re.IGNORECASE)


class SentenceSegmenter:
    """Groups text into moderate-length speech segments."""
    def __init__(self, *, max_chars: int = 140) -> None:
        if max_chars < 20:
            raise ValueError("max_chars must be at least 20")
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


class BilingualStreamParser:
    """Incrementally parses [PT] caption and [JA] spoken sections.

    Markers may be split across provider chunks. Caption text is emitted as
    received; Japanese is segmented into speech-ready sentences. Plain-text
    legacy replies are retained for the caller to route through fallback TTS.
    """
    def __init__(self, *, max_chars: int = 140) -> None:
        self._buffer = ""
        self._mode: str | None = None
        self._pt_ever = False
        self._ja_ever = False
        self._legacy = ""
        self._ja_segmenter = SentenceSegmenter(max_chars=max_chars)

    @property
    def has_bilingual_markers(self) -> bool:
        return self._pt_ever or self._ja_ever

    @property
    def legacy_text(self) -> str:
        return self._legacy.strip()

    def feed(self, fragment: str) -> tuple[str, list[str]]:
        self._buffer += str(fragment or "")
        caption = []
        spoken: list[str] = []
        while self._buffer:
            marker = _MARKER_RE.search(self._buffer)
            if marker:
                text = self._buffer[:marker.start()]
                self._buffer = self._buffer[marker.end():]
                if self._mode == "PT":
                    caption.append(text)
                elif self._mode == "JA":
                    spoken.extend(self._ja_segmenter.feed(text))
                elif self._mode is None:
                    self._legacy += text
                self._mode = marker.group(1).upper()
                if self._mode == "PT":
                    self._pt_ever = True
                else:
                    self._ja_ever = True
                continue
            # Keep a possible split marker suffix from being emitted as content.
            keep = min(3, len(self._buffer))
            safe = self._buffer[:-keep] if keep else self._buffer
            self._buffer = self._buffer[-keep:] if keep else ""
            if self._mode == "PT":
                caption.append(safe)
            elif self._mode == "JA":
                spoken.extend(self._ja_segmenter.feed(safe))
            elif self._mode is None:
                self._legacy += safe
            break
        return "".join(caption), spoken

    def flush(self) -> tuple[str, list[str], str]:
        tail = self._buffer
        self._buffer = ""
        caption_tail = ""
        spoken: list[str] = []
        if self._mode == "PT":
            caption_tail = tail
        elif self._mode == "JA":
            spoken.extend(self._ja_segmenter.feed(tail))
        elif self._mode is None:
            self._legacy += tail
        spoken.extend(self._ja_segmenter.flush())
        return caption_tail, spoken, self.legacy_text


class StreamingSpeechSession:
    """Thread-safe state for a response whose speech is prepared incrementally."""
    def __init__(self, token: int) -> None:
        self.token = token
        self.segmenter = SentenceSegmenter()
        self.bilingual_parser = BilingualStreamParser()
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
            self.bilingual_parser._ja_segmenter.reset()
            self.bilingual_parser._buffer = ""
        self.segments.put(None)

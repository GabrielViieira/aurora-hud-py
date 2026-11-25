"""Cheap local observers for proactive conversational nudges (no actions/tools)."""
from __future__ import annotations

import ctypes
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

_GAME_TITLE_RE = re.compile(
    r"(?i)(unreal|unity|valorant|minecraft|elden ring|unturned|apex|"
    r"league of legends|dota|counter.?strike|cs2|overwatch|fortnite|genshin|honkai|"
    r"roblox|final fantasy|warframe|cyberpunk|witcher|skyrim|red dead|gta|resident evil)"
)
_PARTIAL_DOWNLOAD_EXTS = {".crdownload", ".part", ".partial", ".download", ".tmp"}


def get_active_window_title() -> str:
    """Best-effort foreground window title on Windows."""
    if os.name != "nt":
        return ""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value.strip()
    except Exception:
        return ""


@dataclass
class ObserverSnapshot:
    active_window: str
    is_game: bool
    game_seconds: float
    local_time: str
    download_completed: str = ""


class CheapObservers:
    """Poll window metadata and download-folder file sizes without AI inference."""
    def __init__(self, *, downloads_dir: Path | None = None, window_reader=get_active_window_title):
        self.downloads_dir = downloads_dir or (Path.home() / "Downloads")
        self.window_reader = window_reader
        self._last_tick = time.monotonic()
        self._active_window = ""
        self._game_seconds = 0.0
        self._files: dict[str, tuple[int, float, int]] = {}
        self._recent_completion_at = 0.0

    @staticmethod
    def _is_game(title: str) -> bool:
        return bool(_GAME_TITLE_RE.search(title or ""))

    def _scan_downloads(self, now: float) -> str:
        try:
            candidates = list(self.downloads_dir.iterdir())
        except OSError:
            return ""
        current: dict[str, tuple[int, float, int]] = {}
        completed = ""
        for path in candidates[:300]:
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
                if path.suffix.casefold() in _PARTIAL_DOWNLOAD_EXTS:
                    current[str(path)] = (size, now, 0)
                    continue
                previous = self._files.get(str(path))
                if previous is None:
                    current[str(path)] = (size, now, 0)
                    continue
                old_size, stable_since, stable_count = previous
                if size != old_size:
                    current[str(path)] = (size, now, 0)
                else:
                    stable_count += 1
                    current[str(path)] = (size, stable_since, stable_count)
                    if stable_count == 2 and old_size > 1024 and now - self._recent_completion_at > 180:
                        completed = path.name
                        self._recent_completion_at = now
            except OSError:
                continue
        # Keep entries from disappearing/reappearing download jobs only briefly.
        self._files = current
        return completed

    def snapshot(self, *, now: float | None = None) -> ObserverSnapshot:
        moment = time.monotonic() if now is None else float(now)
        delta = max(0.0, min(90.0, moment - self._last_tick))
        self._last_tick = moment
        title = self.window_reader() or ""
        is_game = self._is_game(title)
        if is_game:
            self._game_seconds += delta
        else:
            # Preserve cumulative session, but a non-game foreground gap is not game time.
            pass
        download = self._scan_downloads(moment)
        return ObserverSnapshot(
            active_window=title[:240],
            is_game=is_game,
            game_seconds=self._game_seconds,
            local_time=time.strftime("%H:%M"),
            download_completed=download,
        )

    def reset_game_session(self) -> None:
        self._game_seconds = 0.0

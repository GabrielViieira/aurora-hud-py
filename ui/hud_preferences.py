"""Preferências locais da HUD, separadas das credenciais e do arquivo .env."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Mapping, Any


def default_hud_preferences(streaming_enabled: bool = True) -> dict[str, Any]:
    return {
        "speech_enabled": True,
        "streaming_enabled": bool(streaming_enabled),
        "long_responses_in_panel": True,
        "speech_volume_percent": 100,
        "speech_rate": 165,
        "response_panel_threshold": 260,
    }


def normalize_hud_preferences(
    values: Mapping[str, Any] | None,
    *,
    streaming_default: bool = True,
) -> dict[str, Any]:
    preferences = default_hud_preferences(streaming_default)
    if values:
        for key in ("speech_enabled", "streaming_enabled", "long_responses_in_panel"):
            value = values.get(key, preferences[key])
            if isinstance(value, bool):
                preferences[key] = value
            elif isinstance(value, str):
                preferences[key] = value.strip().casefold() in {"1", "true", "yes", "on", "sim"}
        bounds = {
            "speech_volume_percent": (25, 125),
            "speech_rate": (100, 220),
            "response_panel_threshold": (160, 600),
        }
        for key, (minimum, maximum) in bounds.items():
            try:
                preferences[key] = max(minimum, min(maximum, int(values.get(key, preferences[key]))))
            except (TypeError, ValueError, OverflowError):
                pass
    return preferences


def load_hud_preferences(
    path: Path,
    *,
    streaming_default: bool = True,
) -> dict[str, Any]:
    defaults = default_hud_preferences(streaming_default)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return defaults
    if not isinstance(raw, dict):
        return defaults
    return normalize_hud_preferences(raw, streaming_default=streaming_default)


def save_hud_preferences(path: Path, values: Mapping[str, Any]) -> dict[str, Any]:
    destination = Path(path)
    normalized = normalize_hud_preferences(values)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=destination.name + ".",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as stream:
            json.dump(normalized, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            temp_path = Path(stream.name)
        os.replace(temp_path, destination)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    return normalized

"""Logs estruturados e redação de segredos.

Não usamos `structlog` na Fase 1 para manter o núcleo sem dependências.
O formato é JSON por linha, legível por máquina e por humano.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|token|secret|senha|password|authorization|bearer)"
)
_SECRET_VALUE_RES = (
    re.compile(r"freellmapi-[A-Za-z0-9._-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9._-]{8,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{8,}", re.IGNORECASE),
)
_REDACTED = "***REDACTED***"


def redact_text(text: str) -> str:
    """Remove padrões óbvios de credencial de um texto."""
    if not text:
        return text
    for pattern in _SECRET_VALUE_RES:
        text = pattern.sub(_REDACTED, text)
    return text


def redact(value: Any, _depth: int = 0) -> Any:
    """Redige recursivamente valores com chaves sensíveis."""
    if _depth > 6:
        return "<...>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key)) and not str(key).lower().endswith("_env"):
                out[key] = _REDACTED
            else:
                out[key] = redact(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
        }
        extra = getattr(record, "aurora", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_dir: Path, level: str = "INFO", console: bool = True) -> Path:
    """Configura root logger com arquivo rotativo + console simples."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "aurora.log"

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler(stream=sys.stderr)
        console_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        console_handler.setLevel(logging.WARNING)
        root.addHandler(console_handler)

    return log_file


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Log com campos estruturados no canal `aurora`."""
    logger.log(level, message, extra={"aurora": fields})

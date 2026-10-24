"""Contrato de ferramentas: esquema, contexto, validação e resultado."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from aurora.config.permissions import PermissionPolicy, Risk
from aurora.config.settings import Settings
from aurora.security.audit import AuditLog


@dataclass
class ToolContext:
    settings: Settings
    policy: PermissionPolicy
    audit: AuditLog
    task_id: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def workspace(self) -> Path:
        return self.settings.paths.workspace_dir


@dataclass
class ToolResult:
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_content(self, max_chars: int = 8000) -> str:
        payload: dict[str, Any] = {"ok": self.ok, "summary": self.summary}
        if self.data:
            payload["data"] = self.data
        if self.error:
            payload["error"] = self.error
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) > max_chars:
            text = text[:max_chars] + "...[resultado truncado]"
        return text

    @classmethod
    def fail(cls, message: str, **data: Any) -> "ToolResult":
        return cls(ok=False, summary=message, data=data, error=message)


Handler = Callable[[ToolContext, dict[str, Any]], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    risk: Risk
    handler: Handler
    category: str = "geral"

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _json_type_name(schema_type: Any) -> str:
    if isinstance(schema_type, list):
        return "|".join(str(item) for item in schema_type)
    return str(schema_type or "")


def validate_arguments(schema: Mapping[str, Any], args: Mapping[str, Any]) -> list[str]:
    """Validação de um subconjunto de JSON Schema, suficiente para as tools."""
    errors: list[str] = []
    if not isinstance(args, Mapping):
        return ["argumentos devem ser um objeto JSON"]

    expected = schema.get("type", "object")
    if expected == "object":
        properties: dict[str, Any] = schema.get("properties") or {}
        required: list[str] = list(schema.get("required") or [])
        for key in required:
            if key not in args:
                errors.append(f"argumento obrigatório ausente: {key}")
        if schema.get("additionalProperties") is False:
            for key in args:
                if key not in properties:
                    errors.append(f"argumento desconhecido: {key}")

        for key, value in args.items():
            prop = properties.get(key)
            if not isinstance(prop, dict):
                continue
            errors.extend(_validate_value(key, value, prop))
    return errors


def _validate_value(name: str, value: Any, schema: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    expected = _json_type_name(schema.get("type"))

    type_ok = True
    if expected in {"string", "str"}:
        type_ok = isinstance(value, str)
    elif expected in {"integer", "int"}:
        type_ok = isinstance(value, int) and not isinstance(value, bool)
    elif expected in {"number", "float"}:
        type_ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif expected in {"boolean", "bool"}:
        type_ok = isinstance(value, bool)
    elif expected in {"array", "list"}:
        type_ok = isinstance(value, list)
    elif expected in {"object", "dict"}:
        type_ok = isinstance(value, dict)
    elif "|" in expected:
        type_ok = any(
            not _validate_value(name, value, {**schema, "type": part}) for part in expected.split("|")
        )

    if not type_ok:
        errors.append(f"{name}: esperado {expected}, recebido {type(value).__name__}")
        return errors

    if isinstance(value, str):
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(f"{name}: excede {schema['maxLength']} caracteres")
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append(f"{name}: mínimo de {schema['minLength']} caracteres")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{name}: abaixo do mínimo {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{name}: acima do máximo {schema['maximum']}")
    if isinstance(value, list) and "maxItems" in schema and len(value) > int(schema["maxItems"]):
        errors.append(f"{name}: máximo de {schema['maxItems']} itens")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{name}: deve ser um de {schema['enum']}")
    return errors

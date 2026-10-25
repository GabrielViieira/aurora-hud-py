"""Configuração autenticada e compartilhada para tradução de voz.

Usa a mesma URL base e a mesma chave de API que o runtime da Aurora.
Nunca escreve a chave em logs nem devolve segredos em mensagens de erro.
"""
from __future__ import annotations

import os

from aurora.config.settings import Settings


def translation_request_config() -> tuple[str, dict[str, str]]:
    """Devolve endpoint OpenAI-compatible e cabeçalhos autenticados do gateway."""
    settings = Settings.load()
    explicit_url = os.environ.get("AURORA_FREELLM_URL", "").strip()
    base_url = explicit_url or settings.llm.base_url
    base_url = base_url.rstrip("/")

    # Aceita host raiz, /v1 ou o endpoint completo configurado pelo usuário.
    if base_url.endswith("/chat/completions"):
        endpoint = base_url
    elif base_url.endswith("/v1"):
        endpoint = base_url + "/chat/completions"
    else:
        endpoint = base_url + "/v1/chat/completions"

    api_key = settings.api_key().strip()
    if not api_key:
        raise RuntimeError("a chave de API da Aurora está vazia")

    return endpoint, {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

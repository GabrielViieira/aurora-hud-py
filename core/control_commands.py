"""Comandos locais curtos da HUD, utilizáveis por texto ou voz."""
from __future__ import annotations

import re
import unicodedata


def classify_hud_control_command(text: str) -> str:
    """Retorna 'cancel', 'exit' ou string vazia; não envia comandos locais ao modelo."""
    value = unicodedata.normalize("NFKD", str(text or "").casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()
    value = re.sub(r"^(?:(?:ei|hey) )?aurora(?:\s+|$)", "", value).strip()

    cancel_patterns = (
        r"(?:para|pare|parar|cancele|cancelar|cancel|interrompa|interromper|stop|chega)(?: (?:a|o|the|de))?(?: (?:tarefa|instrucao|comando|execucao|processo|isso|task|instruction|command))?(?: (?:atual|current))?(?: agora| now)?",
        r"(?:cancel|stop) (?:the )?(?:current )?(?:task|instruction|command)",
        r"(?:para|pare|parar|interrompa|interromper|cancele|cancelar|stop|cancel|chega)(?: (?:de|a|o|the))? (?:falar|fala|voz|audio|speaking|talking|voice|speech)(?: (?:agora|now))?",
    )
    if any(re.fullmatch(pattern, value) for pattern in cancel_patterns):
        return "cancel"

    close_patterns = (
        r"(?:feche|fecha|fechar|encerre|encerra|encerrar|desligue|desliga|desligar|close|quit|exit) (?:a |o )?aurora",
        r"(?:saia|sair|exit|quit) (?:da|de) aurora",
        r"(?:feche|fecha|fechar|encerre|encerrar|close|quit|exit) (?:o |a )?(?:app|aplicativo) aurora",
        r"(?:close|quit|exit) aurora",
    )
    if any(re.fullmatch(pattern, value) for pattern in close_patterns):
        return "exit"
    return ""

"""Classificação heurística e independente de aplicativo para controles de pesquisa.

Usa metadados de acessibilidade e geometria, sem assumir coordenadas ou nomes
específicos de um aplicativo. A pontuação é evidência para o agente, não uma
garantia de que o controle seja de pesquisa.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

_SEARCH_TERMS = (
    "search", "searchbox", "searchbar", "searchfield", "searchinput",
    "pesquisar", "pesquisa", "busca", "buscar", "procurar",
    "find", "lookup", "query", "rechercher", "recherche", "suchen",
    "検索", "搜索",
)
_FILTER_TERMS = ("filter", "filtro", "filtrar", "filtre", "筛选")
_NAVIGATION_TERMS = (
    "browse", "recommendation", "recommended", "categories", "category",
    "community", "library", "wishlist", "downloads", "home", "settings",
    "discover", "news", "menu", "sort", "navigation", "navegacao",
    "recomendacoes", "categorias", "biblioteca", "inicio", "configuracoes",
)
# Chromium/Electron expõem o campo de busca como ComboBox, não como Edit.
_INPUT_TYPES = {"Edit", "ComboBox"}


def _normalize(value: object) -> str:
    raw = unicodedata.normalize("NFKD", str(value or "")).casefold()
    raw = "".join(char for char in raw if not unicodedata.combining(char))
    return re.sub(r"[^\w]+", " ", raw).strip()


def _has_term(value: object, terms: tuple[str, ...]) -> bool:
    normalized = _normalize(value)
    if not normalized:
        return False
    compact = re.sub(r"[\W_]+", "", normalized)
    words = set(normalized.split())
    return any(term in words or term in compact for term in terms)


def _score_candidate(candidate: dict[str, Any], window_width: int) -> tuple[int, list[str]]:
    control_type = str(candidate.get("tipo") or "")
    name = str(candidate.get("nome") or "")
    help_text = str(candidate.get("ajuda") or "")
    automation_id = str(candidate.get("id_automacao") or "")
    class_name = str(candidate.get("classe") or "")
    score = 0
    evidence: list[str] = []

    if _has_term(name, _SEARCH_TERMS):
        # Rótulo em um campo editável é evidência mais forte que o mesmo texto
        # em um Text estático (que pode apenas rotular um campo próximo).
        if control_type in _INPUT_TYPES:
            score += 52
        elif control_type == "Button":
            score += 35
        elif control_type in {"Text", "Hyperlink"}:
            score += 24
        else:
            score += 32
        evidence.append("rótulo contém termo de pesquisa")
    if _has_term(help_text, _SEARCH_TERMS):
        score += 30
        evidence.append("texto de ajuda/placeholder indica pesquisa")
    if _has_term(automation_id, _SEARCH_TERMS):
        score += 34
        evidence.append("id de automação indica pesquisa")
    if _has_term(class_name, _SEARCH_TERMS):
        score += 12
        evidence.append("classe contém termo de pesquisa")

    filter_fields = (name, help_text, automation_id)
    if any(_has_term(value, _FILTER_TERMS) for value in filter_fields):
        score += 15
        evidence.append("rótulo indica filtro")

    left = int(candidate.get("esquerda", 0) or 0)
    top = int(candidate.get("topo", 0) or 0)
    right = int(candidate.get("direita", left) or left)
    bottom = int(candidate.get("base", top) or top)
    width = max(0, right - left)
    height = max(0, bottom - top)
    aspect_ratio = width / max(1, height)
    width_ratio = width / max(1, window_width)

    if control_type in _INPUT_TYPES:
        score += 10
        evidence.append("controle é um campo editável")
        if width >= 180:
            score += 6
            evidence.append("campo tem largura significativa")
        if width_ratio >= 0.20:
            score += 8
            evidence.append("campo ocupa parte relevante da largura da janela")
        if width_ratio >= 0.35:
            score += 4
        if aspect_ratio >= 3.5:
            score += 7
            evidence.append("formato horizontal comprido")
        if aspect_ratio >= 6:
            score += 3
        if 16 <= height <= 70:
            score += 3
            evidence.append("altura compatível com campo de entrada de uma linha")
        elif height > 100:
            score -= 12
            evidence.append("campo alto demais para uma busca típica")

    if control_type == "Button":
        score += 3

    # Rótulos de navegação são alternativas comuns, mas não são por si só busca.
    has_search_label = any(
        _has_term(value, _SEARCH_TERMS) for value in (name, help_text, automation_id)
    )
    if not has_search_label and _has_term(name, _NAVIGATION_TERMS):
        score -= 28
        evidence.append("rótulo parece item de navegação, não campo de pesquisa")
    if not has_search_label and control_type in {"MenuItem", "TabItem"}:
        score -= 8

    return max(0, min(100, score)), evidence


def _nearby(button: dict[str, Any], field: dict[str, Any]) -> bool:
    """Retorna se um botão pequeno está encostado a um campo editável comprido."""
    left = int(button.get("esquerda", 0) or 0)
    top = int(button.get("topo", 0) or 0)
    right = int(button.get("direita", left) or left)
    bottom = int(button.get("base", top) or top)
    f_left = int(field.get("esquerda", 0) or 0)
    f_top = int(field.get("topo", 0) or 0)
    f_right = int(field.get("direita", f_left) or f_left)
    f_bottom = int(field.get("base", f_top) or f_top)
    f_width = f_right - f_left
    b_width = right - left
    b_height = bottom - top
    f_height = f_bottom - f_top
    if f_width < 180 or b_width <= 0 or b_width > 100 or b_height <= 0:
        return False
    overlap_y = max(0, min(bottom, f_bottom) - max(top, f_top))
    vertical_alignment = overlap_y / max(1, min(b_height, f_height))
    vertical_close = abs(((top + bottom) / 2) - ((f_top + f_bottom) / 2)) <= max(18, f_height)
    horizontal_gap = max(0, f_left - right, left - f_right)
    near_horizontally = horizontal_gap <= max(24, int(b_height * 1.5))
    return (vertical_alignment >= 0.35 or vertical_close) and near_horizontally


def rank_search_candidates(
    candidates: list[dict[str, Any]], window_width: int
) -> list[dict[str, Any]]:
    """Anota e ordena controles por probabilidade heurística de relação com busca."""
    ranked: list[dict[str, Any]] = []
    for original in candidates:
        item = dict(original)
        score, evidence = _score_candidate(item, max(1, int(window_width)))
        item["pontuacao_busca"] = score
        item["evidencias_busca"] = evidence
        ranked.append(item)

    # Ícones de lupa podem ser botões sem nome. A proximidade com um Edit
    # horizontal ajuda a reconhecê-los sem depender do aplicativo.
    for button in ranked:
        if button.get("tipo") not in {"Button", "Image"}:
            continue
        if not str(button.get("nome") or "").startswith(("[Botão sem nome]", "[Imagem sem nome]")):
            continue
        if str(button.get("ajuda") or "").strip() or str(button.get("id_automacao") or "").strip():
            continue
        for field in ranked:
            if field.get("tipo") != "Edit" or field is button:
                continue
            if field["pontuacao_busca"] < 25 or not _nearby(button, field):
                continue
            field["pontuacao_busca"] = min(100, field["pontuacao_busca"] + 8)
            if "há um botão próximo que pode ser a ação/ícone de busca" not in field["evidencias_busca"]:
                field["evidencias_busca"].append("há um botão próximo que pode ser a ação/ícone de busca")
            button["pontuacao_busca"] = min(100, max(20, button["pontuacao_busca"]) + 18)
            button["evidencias_busca"].append(
                "controle visual sem nome alinhado a um campo editável comprido; pode ser uma lupa"
            )
            break

    for item in ranked:
        score = int(item["pontuacao_busca"])
        if score >= 70:
            confidence = "alta"
        elif score >= 48:
            confidence = "média"
        elif score >= 30:
            confidence = "baixa"
        else:
            confidence = "muito baixa"
        item["confianca_busca"] = confidence
        if score >= 30 and item.get("tipo") in _INPUT_TYPES:
            item["papel_sugerido"] = "possível campo de busca"
        elif score >= 30 and item.get("tipo") == "Button":
            item["papel_sugerido"] = "possível botão de busca"
        elif score >= 30 and item.get("tipo") == "Image":
            item["papel_sugerido"] = "possível ícone de busca"
        else:
            item["papel_sugerido"] = ""

    ranked.sort(
        key=lambda item: (
            -int(item.get("pontuacao_busca", 0)),
            int(item.get("_priority", 9)),
            int(item.get("topo", 0)),
            int(item.get("esquerda", 0)),
        )
    )
    return ranked

"""Controle visual da área de trabalho para a Aurora.

As ações de entrada são ferramentas L1: a política de segurança padrão da
Aurora pode pedir confirmação antes de mover/clicar/digitar.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

from aurora.config.permissions import Risk
from aurora.tools.base import ToolContext, ToolResult, ToolSpec
from aurora.tools.search_perception import rank_search_candidates

_TOOL_DESCRIPTION_CAPTURE = (
    "Captura um monitor e retorna a imagem, dimensões, origem da captura e geometria da área de trabalho virtual. "
    "Controles em interface_acessivel usam coordenadas absolutas do desktop; centro_x/centro_y podem ser usados "
    "diretamente quando o nome do controle corresponde ao alvo. Para estimar um ponto pela imagem reduzida, "
    "converta o ponto (u, v) da imagem capturada para desktop: "
    "x = origem_x + u * largura_original / largura_capturada; "
    "y = origem_y + v * altura_original / altura_capturada. "
    "Não trate pixels da imagem reduzida como coordenadas absolutas. Coordenadas negativas só são válidas "
    "quando estiverem dentro dos limites da área de trabalho virtual, por exemplo em um monitor à esquerda/acima. "
    "Escolha um monitor pelo índice (1 é o primeiro listado). "
    "Para encontrar campos, botões ou controles de texto em tarefas interativas, prefira "
    "somente_acessibilidade=true na primeira tentativa; se não houver controles úteis, a ferramenta "
    "captura pixels automaticamente. Para pedidos puramente visuais (descrever imagem/ícone/layout), "
    "use somente_imagem=true para omitir UIA. Use captura normal quando precisar de imagem e controles. "
    "Como reconhecer uma barra de pesquisa em qualquer aplicativo, sem decorar nomes: "
    "(1) é um campo de entrada de uma linha, largo e horizontal, quase sempre no topo ou no cabeçalho; "
    "(2) o rótulo, o placeholder, o texto de ajuda ou o id costuma conter search/pesquisar/busca/buscar/find/filtro; "
    "(3) há uma lupa — ícone ou botão — dentro do campo ou encostada nele. "
    "O campo pode aparecer como Edit (campo de texto) ou ComboBox (o Chromium/Electron expõe assim). "
    "resumo_pesquisa traz pontuacao_busca, confianca_busca e evidencias_busca desses indícios: "
    "decida pelos indícios (rótulo + lupa + geometria), nunca pelo nome do aplicativo."
)
_TOOL_DESCRIPTION_MOUSE = (
    "Controla o mouse na área de trabalho virtual. Faça capturar_tela antes. Se houver um controle acessível "
    "com nome EXATO ou id_automacao único, prefira enviar esse valor em 'controle' para clicá-lo diretamente "
    "via UI Automation (para clicar, duplo-clique ou clique direito); isso evita estimar pixels pela imagem. "
    "Caso contrário, use centro_x/centro_y do controle acessível quando houver correspondência clara; esses "
    "valores já são coordenadas absolutas e não devem ser redimensionados. Se estimar o ponto pela imagem, "
    "transforme-o usando origem_x/origem_y e as dimensões original/capturada retornadas pela captura. Use x/y "
    "absolutos, não coordenadas relativas à janela ou à imagem. Coordenadas negativas só valem dentro dos limites "
    "reais da área de trabalho virtual. Execute uma ação por chamada e verifique o resultado."
)
_TOOL_DESCRIPTION_SEARCH = (
    "Pesquisa em qualquer aplicativo numa única chamada: encontra a barra de pesquisa pela anatomia "
    "(campo de uma linha largo + rótulo/placeholder com search/pesquisar/busca/find + lupa dentro ou ao lado), "
    "foca o campo, digita o texto, confirma o que realmente entrou e pressiona Enter. "
    "Não depende do nome do aplicativo. Se a evidência for fraca ou houver empate entre campos, "
    "recusa em vez de chutar e devolve os candidatos com pontuação para você escolher com controlar_mouse. "
    "Prefira esta ferramenta a clicar e digitar manualmente sempre que o pedido for 'pesquise/procure X'."
)

_TOOL_DESCRIPTION_KEYBOARD = (
    "Digita texto, pressiona uma tecla ou envia um atalho ao aplicativo em primeiro plano. "
    "Use capturar_tela para confirmar qual janela está ativa e verificar o resultado. "
    "Depois de digitar, o retorno traz 'confirmado' e o valor lido no campo em foco: "
    "se confirmado=false, o texto não entrou — clique no campo (de preferência pelo controle acessível) "
    "e digite de novo antes de anunciar sucesso. confirmado=null significa que o campo não pôde ser lido. "
    "Atalhos de encerramento/segurança são bloqueados por proteção."
)


def _pyautogui():
    try:
        import pyautogui
    except ImportError as exc:
        raise RuntimeError(
            "Controle de mouse/teclado não instalado. Rode: py -m pip install -r requirements-desktop.txt"
        ) from exc
    pyautogui.FAILSAFE = True
    # Pausa pequena para reduzir a latência entre operações; ainda pode ser ajustada
    # para aplicativos que precisam de mais tempo para processar entrada.
    try:
        action_pause = float(os.environ.get("AURORA_DESKTOP_ACTION_PAUSE", "0.01"))
    except ValueError:
        action_pause = 0.01
    pyautogui.PAUSE = max(0.0, min(action_pause, 0.5))
    return pyautogui


def _virtual_bounds(pyautogui) -> tuple[int, int, int, int]:
    """Retorna left, top, width, height; respeita coordenadas negativas em multi-monitor."""
    if os.name == "nt":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            left = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
            top = int(user32.GetSystemMetrics(77))   # SM_YVIRTUALSCREEN
            width = int(user32.GetSystemMetrics(78)) # SM_CXVIRTUALSCREEN
            height = int(user32.GetSystemMetrics(79))# SM_CYVIRTUALSCREEN
            if width > 0 and height > 0:
                return left, top, width, height
        except Exception:
            pass
    width, height = pyautogui.size()
    return 0, 0, int(width), int(height)


def _check_point(pyautogui, x: Any, y: Any, label: str = "posição") -> tuple[int, int]:
    if isinstance(x, bool) or not isinstance(x, int) or isinstance(y, bool) or not isinstance(y, int):
        raise ValueError(f"{label}: x e y precisam ser inteiros")
    left, top, width, height = _virtual_bounds(pyautogui)
    if not (left <= x < left + width and top <= y < top + height):
        raise ValueError(
            f"{label} ({x}, {y}) fora da área de trabalho virtual: "
            f"x entre {left} e {left + width - 1}, y entre {top} e {top + height - 1}. "
            "Use coordenadas absolutas do desktop; se o ponto veio da imagem reduzida, "
            "converta usando origem_x/origem_y e as dimensões original/capturada retornadas pela captura."
        )
    # PyAutoGUI usa o canto superior esquerdo da tela principal como fail-safe.
    if x == 0 and y == 0:
        raise ValueError("coordenada (0, 0) reservada para a proteção de parada do mouse")
    return x, y


_INPUT_CONTROL_TYPES = ("Edit", "ComboBox")


def _collect_scan_controls(window: Any, scan_mode: str) -> list[Any]:
    """Reúne os controles candidatos de uma janela para a leitura de acessibilidade.

    No modo rápido combina duas leituras: uma varredura em largura rasa (dá
    contexto: textos, botões, links) e um FindAll dirigido aos tipos de entrada.
    Interfaces Chromium/Electron — Steam, Discord, VS Code — aninham o campo de
    busca além do limite de profundidade, e ele desaparecia do resultado.
    """
    if scan_mode == "full":
        # Modo completo preserva a leitura ampla para apps complexos.
        try:
            return list(window.descendants()[:500])
        except Exception:
            return []

    # Varredura rápida: limita profundidade, número de nós e tempo total.
    # Evita percorrer toda a árvore de acessibilidade de navegadores e IDEs.
    from collections import deque

    scan_started = time.perf_counter()
    scan_budget_s = 0.35
    max_scan_nodes = 160
    max_scan_depth = 5
    controls: list[Any] = []
    queue = deque([(window, 0)])
    while queue and len(controls) < max_scan_nodes:
        if time.perf_counter() - scan_started >= scan_budget_s:
            break
        parent, depth = queue.popleft()
        if depth >= max_scan_depth:
            continue
        try:
            children = parent.children()
        except Exception:
            continue
        for child in children:
            controls.append(child)
            if depth + 1 < max_scan_depth and len(controls) < max_scan_nodes:
                queue.append((child, depth + 1))
            if len(controls) >= max_scan_nodes:
                break

    # FindAll dirigido: ~150ms por tipo, enxerga qualquer profundidade. Só roda
    # nos tipos de entrada, que são onde a busca costuma se esconder.
    directed: list[Any] = []
    for control_type in _INPUT_CONTROL_TYPES:
        try:
            directed.extend(window.descendants(control_type=control_type))
        except Exception:
            continue
    # Campos de entrada vêm PRIMEIRO: são poucos, são o alvo principal e, no fim
    # da lista, eram cortados pelo orçamento de processamento antes de serem lidos
    # (o ComboBox do Steam ficava invisível para o classificador de busca).
    return directed + controls


def _active_window_snapshot(limit: int = 35) -> dict[str, Any]:
    """Lê nomes e retângulos de controles acessíveis da janela em primeiro plano.

    É uma observação local sem OCR nem chamada de modelo. Falhas da árvore UIA
    não impedem a captura da imagem.
    """
    if os.name != "nt":
        return {"disponivel": False, "motivo": "UI Automation só está disponível no Windows."}

    scan_mode = os.environ.get("AURORA_DESKTOP_UIA_MODE", "fast").strip().lower()
    if scan_mode == "off":
        return {"disponivel": False, "motivo": "Leitura UI Automation desativada por configuração; use a imagem."}
    if scan_mode not in {"fast", "full"}:
        scan_mode = "fast"

    try:
        import ctypes
        from pywinauto import Desktop

        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        if not hwnd:
            return {"disponivel": False, "motivo": "O Windows não informou uma janela em primeiro plano."}
        window = Desktop(backend="uia").window(handle=hwnd)
        if not window.exists(timeout=1):
            return {"disponivel": False, "motivo": "A janela ativa não está acessível pela UI Automation."}

        title = str(window.window_text() or "").strip()[:200]
        rect = window.rectangle()
        snapshot: dict[str, Any] = {
            "disponivel": True,
            "janela_ativa": title,
            "retangulo_janela": {
                "esquerda": int(rect.left), "topo": int(rect.top),
                "direita": int(rect.right), "base": int(rect.bottom),
            },
            "resumo_pesquisa": [],
            "elementos": [],
            "total_elementos_lidos": 0,
        }
        useful_types = {
            "Button", "Text", "Edit", "Hyperlink", "MenuItem", "TabItem",
            "CheckBox", "RadioButton", "ComboBox", "ListItem", "TreeItem",
            "DataItem", "Document", "Group", "Image",
        }
        priority_types = {
            # Campos editáveis são especialmente importantes para buscas e formulários.
            "Edit": -1, "Button": 0, "MenuItem": 0, "Hyperlink": 0,
            "ComboBox": 1, "CheckBox": 1, "RadioButton": 1, "TabItem": 1,
            "ListItem": 2, "TreeItem": 2, "DataItem": 2, "Text": 3,
            "Image": 2, "Document": 4, "Group": 5,
        }
        candidates: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()

        controls = _collect_scan_controls(window, scan_mode)

        max_controls = 160 if scan_mode == "fast" else 500
        # Limite também o processamento dos elementos: consultas COM individuais
        # podem demorar além do orçamento usado apenas para percorrer a árvore.
        processing_deadline = time.perf_counter() + 1.2 if scan_mode == "fast" else None
        for control in controls[:max_controls]:
            if processing_deadline is not None and time.perf_counter() >= processing_deadline:
                break
            try:
                info = getattr(control, "element_info", None)
                control_type = str(getattr(info, "control_type", "") or "")
                if control_type not in useful_types:
                    continue
                name = str(control.window_text() or getattr(info, "name", "") or "").strip()
                # Campos Edit sem nome acessível são comuns em interfaces Chromium/Electron.
                # Ainda assim podem ser o campo de pesquisa; mantenha o tipo e a posição.
                details = _control_details(control)
                automation_id = details["auto_id"]
                help_text = details["ajuda"]
                class_name = details["classe"]
                if not name:
                    # Botões sem rótulo podem ser ícones (por exemplo, uma lupa).
                    # Mantê-los permite usar geometria e proximidade para classificá-los.
                    if control_type not in {"Edit", "Button", "Image"}:
                        continue
                    placeholder = {
                        "Edit": "[Campo de texto sem nome]",
                        "Button": "[Botão sem nome]",
                        "Image": "[Imagem sem nome]",
                    }[control_type]
                    name = help_text or placeholder
                name = re.sub(r"\s+", " ", name)[:180]
                # No modo rápido, evita uma chamada COM de visibilidade por controle;
                # o retângulo ainda precisa cruzar a janela ativa. O modo full preserva
                # a checagem de visibilidade mais detalhada.
                if scan_mode == "full":
                    try:
                        if not control.is_visible():
                            continue
                    except Exception:
                        pass
                bounds = control.rectangle()
                left, top, right, bottom = (
                    int(bounds.left), int(bounds.top), int(bounds.right), int(bounds.bottom)
                )
                if right <= left or bottom <= top:
                    continue
                # Mantém apenas controles cujo retângulo cruza a janela ativa.
                if right <= int(rect.left) or left >= int(rect.right) or bottom <= int(rect.top) or top >= int(rect.bottom):
                    continue
                key = (control_type, name.casefold(), left, top, right, bottom)
                if key in seen:
                    continue
                seen.add(key)
                candidate = {
                    "_priority": priority_types.get(control_type, 9),
                    "tipo": control_type,
                    "nome": name,
                    "esquerda": left, "topo": top, "direita": right, "base": bottom,
                    "centro_x": (left + right) // 2,
                    "centro_y": (top + bottom) // 2,
                }
                if automation_id:
                    candidate["id_automacao"] = automation_id
                if help_text:
                    candidate["ajuda"] = help_text
                if class_name:
                    candidate["classe"] = class_name
                candidates.append(candidate)
            except Exception:
                continue
        # Coloca os candidatos mais promissores no topo para que o modelo não perca
        # um campo de pesquisa entre dezenas de botões e itens de navegação.
        candidates = rank_search_candidates(candidates, int(rect.right) - int(rect.left))
        snapshot["total_elementos_lidos"] = len(candidates)
        snapshot["elementos"] = [
            {key: value for key, value in item.items() if key != "_priority"}
            for item in candidates[:max(1, min(int(limit), 80))]
        ]
        snapshot["resumo_pesquisa"] = [
            {
                "tipo": item["tipo"],
                "nome": item["nome"],
                "pontuacao_busca": item["pontuacao_busca"],
                "confianca_busca": item["confianca_busca"],
                "evidencias_busca": item["evidencias_busca"],
                "centro_x": item["centro_x"],
                "centro_y": item["centro_y"],
            }
            for item in candidates if item["pontuacao_busca"] >= 25
        ][:8]
        snapshot["nota_coordenadas"] = (
            "Coordenadas dos controles estão em pixels absolutos da área de trabalho. "
            "centro_x/centro_y indicam o centro do controle; use apenas se o nome corresponder ao alvo."
        )
        return snapshot
    except ImportError:
        return {"disponivel": False, "motivo": "pywinauto não está instalado; use a captura visual."}
    except Exception as exc:
        return {"disponivel": False, "motivo": f"Falha ao ler controles da janela ativa: {type(exc).__name__}: {exc}"}


def capture_screen(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Captura pixels para visão ou faz uma verificação rápida apenas via UI Automation."""
    try:
        from aurora.tools.screen_capture import capture_monitors

        requested_max_width = int(args.get("largura_maxima", 640))
        max_width = requested_max_width
        monitor_index = int(args.get("monitor", 1))
        accessibility_only = args.get("somente_acessibilidade", False) is True
        image_only = args.get("somente_imagem", False) is True
        if accessibility_only and image_only:
            return ToolResult.fail("somente_acessibilidade e somente_imagem não podem ser usados juntos")
        if requested_max_width < 320 or requested_max_width > 2200:
            return ToolResult.fail("largura_maxima deve ficar entre 320 e 2200")
        llm_settings = getattr(getattr(ctx, "settings", None), "llm", None)
        local_vision_configured = bool(getattr(llm_settings, "vision_base_url", ""))
        if local_vision_configured:
            # A requisição visual local é reduzida novamente antes do envio; não capture
            # uma imagem maior do que o modelo receberá, para evitar trabalho duplicado.
            max_width = min(max_width, 640)
        width_was_capped = max_width != requested_max_width
        if monitor_index < 1 or monitor_index > 16:
            return ToolResult.fail("monitor deve ficar entre 1 e 16")

        total_started = time.perf_counter()
        screens: list[dict[str, Any]] = []
        capture_ms = 0
        selected: dict[str, Any] | None = None
        data_url = ""

        # Caminho rápido: primeiro tenta obter somente a árvore acessível, sem
        # capturar/encodar imagem. Se UIA não trouxer controles, cai para a captura visual.
        if not accessibility_only:
            capture_started = time.perf_counter()
            screens = capture_monitors(max_width=max_width, monitor_index=monitor_index)
            capture_ms = round((time.perf_counter() - capture_started) * 1000)
            if not screens:
                return ToolResult.fail(f"monitor {monitor_index} não encontrado ou indisponível")
            selected = screens[0]
            data_url = str(selected.get("data_url", ""))
            if not data_url.startswith("data:image/"):
                return ToolResult.fail("captura não produziu uma imagem válida")

        if image_only:
            # Em verificações pós-ação cujo próximo passo depende da visão, a árvore UIA
            # acrescenta latência sem substituir a inspeção da imagem.
            ui_snapshot = {
                "disponivel": False,
                "motivo": "UI Automation omitida nesta captura visual rápida.",
                "elementos": [],
                "resumo_pesquisa": [],
            }
            uia_ms = 0
        else:
            uia_started = time.perf_counter()
            ui_snapshot = _active_window_snapshot()
            uia_ms = round((time.perf_counter() - uia_started) * 1000)
        ui_elements = ui_snapshot.get("elementos", [])
        uia_useful = bool(ui_snapshot.get("disponivel") and isinstance(ui_elements, list) and ui_elements)

        if accessibility_only and not uia_useful:
            # UIA não conseguiu descrever a tela; a verificação adaptativa volta
            # para imagem em vez de deixar o agente decidir às cegas.
            capture_started = time.perf_counter()
            screens = capture_monitors(max_width=max_width, monitor_index=monitor_index)
            capture_ms = round((time.perf_counter() - capture_started) * 1000)
            if not screens:
                return ToolResult.fail(
                    "UI Automation não retornou controles úteis e a captura visual também falhou."
                )
            selected = screens[0]
            data_url = str(selected.get("data_url", ""))
            if not data_url.startswith("data:image/"):
                return ToolResult.fail("captura não produziu uma imagem válida")

        total_ms = round((time.perf_counter() - total_started) * 1000)
        try:
            virtual_left, virtual_top, virtual_width, virtual_height = _virtual_bounds(_pyautogui())
            virtual_bounds = {
                "left": virtual_left,
                "top": virtual_top,
                "width": virtual_width,
                "height": virtual_height,
            }
        except Exception:
            virtual_bounds = None

        metadata: dict[str, Any] = {
            "tempo_captura_ms": capture_ms,
            "tempo_uia_ms": uia_ms,
            "tempo_total_ms": total_ms,
            "largura_solicitada": requested_max_width,
            "limite_visao_local_aplicado": width_was_capped,
            "modo_uia": (
                "omitida_captura_rapida" if image_only else
                os.environ.get("AURORA_DESKTOP_UIA_MODE", "fast").strip().lower() or "fast"
            ),
            "area_trabalho_virtual": virtual_bounds,
            "interface_acessivel": ui_snapshot,
        }
        if selected is not None:
            metadata.update({
                "monitor": selected["monitor"],
                "origem_x": selected["left"],
                "origem_y": selected["top"],
                "largura_original": selected["width"],
                "altura_original": selected["height"],
                "largura_capturada": selected["captured_size"][0],
                "altura_capturada": selected["captured_size"][1],
                "mime_type": selected.get("mime_type", "image/jpeg"),
            })
        else:
            metadata["somente_acessibilidade"] = True

        ui_count = len(ui_elements) if isinstance(ui_elements, list) else 0
        search_count = len(ui_snapshot.get("resumo_pesquisa", []))
        active_title = str(ui_snapshot.get("janela_ativa") or "janela não identificada")
        if selected is None:
            summary = (
                f"Verificação rápida via UI Automation, sem imagem: janela={active_title}; "
                f"controles={ui_count}; UIA={uia_ms} ms; total={total_ms} ms. "
                "Use esses dados para controles acessíveis; se o estado visual não estiver claro, "
                "solicite capturar_tela para análise visual."
            )
        else:
            summary = (
                f"Tela capturada: monitor {monitor_index}, "
                f"{metadata['largura_capturada']}x{metadata['altura_capturada']} "
                f"(captura {capture_ms} ms, UIA {uia_ms} ms, total {total_ms} ms). "
                + (f"Origem desktop da captura=({selected['left']}, {selected['top']}); "
                   f"tamanho original={selected['width']}x{selected['height']}; "
                   f"área de trabalho virtual={virtual_bounds}. " if virtual_bounds else "")
                + (f"Janela em primeiro plano: {active_title}. " if not image_only else "")
                + (
                    "UI Automation omitida para reduzir a latência; confira o estado pela imagem."
                    if image_only else
                    (f"Leitura local encontrou {ui_count} controles acessíveis." if ui_snapshot.get("disponivel") else
                     "Leitura local de controles indisponível; use a imagem e não invente nomes de elementos.")
                )
                + (f" Classificador heurístico destacou {search_count} possível(is) candidato(s) a busca; confira a pontuação, as evidências e a imagem." if ui_snapshot.get("disponivel") else "")
                + " A imagem será enviada ao modelo para inspeção visual."
            )
        result_data = dict(metadata)
        if selected is not None:
            result_data["data_url"] = data_url
        return ToolResult(ok=True, summary=summary, data=result_data)
    except Exception as exc:
        return ToolResult.fail(f"Falha ao capturar a tela: {type(exc).__name__}: {exc}")


def _control_details(control: Any) -> dict[str, str]:
    """Lê nome, tipo, id de automação, ajuda e classe com os nomes reais do pywinauto.

    O pywinauto 1.0 expõe `auto_id` e `legacy_help` no `element_info`. Ler
    `automation_id`/`help_text` devolvia sempre vazio, o que cegava o classificador
    de busca e impedia distinguir alvos ambíguos pelo id.
    """
    info = getattr(control, "element_info", None)

    def _field(*attributes: str, limit: int = 180) -> str:
        for attribute in attributes:
            try:
                value = str(getattr(info, attribute, "") or "").strip()
            except Exception:
                continue
            if value:
                return value[:limit]
        return ""

    try:
        name = str(control.window_text() or "").strip()
    except Exception:
        name = ""
    return {
        "nome": (name or _field("name"))[:180],
        "tipo": _field("control_type", limit=40),
        "auto_id": _field("auto_id", "automation_id", limit=120),
        "ajuda": _field("legacy_help", "help_text", limit=180),
        "classe": _field("class_name", limit=120),
    }


def _active_uia_window() -> Any:
    """Janela em primeiro plano como wrapper UIA."""
    if os.name != "nt":
        raise RuntimeError("Ação por controle acessível só está disponível no Windows.")
    import ctypes
    from pywinauto import Desktop

    hwnd = int(ctypes.windll.user32.GetForegroundWindow())
    if not hwnd:
        raise RuntimeError("O Windows não informou uma janela em primeiro plano.")
    window = Desktop(backend="uia").window(handle=hwnd)
    if not window.exists(timeout=0.5):
        raise RuntimeError("A janela ativa não está acessível pela UI Automation.")
    return window


def _match_accessible_controls(window: Any, target: str) -> dict[tuple[Any, ...], Any]:
    """Controles cujo nome ou auto_id coincida exatamente com o alvo.

    O backend UIA do pywinauto rejeita `title=` em `descendants()` com TypeError.
    Como esse erro era engolido, a ação por nome nunca encontrava nada e o agente
    caía sempre em coordenadas estimadas.
    """
    normalized_target = re.sub(r"\s+", " ", str(target or "")).strip().casefold()
    found: dict[tuple[Any, ...], Any] = {}
    if not normalized_target:
        return found
    for criterion in ({"auto_id": target}, {"name": target}):
        try:
            candidates = window.descendants(**criterion)
        except Exception:
            continue
        for control in candidates:
            try:
                details = _control_details(control)
                matched_values = {
                    re.sub(r"\s+", " ", value).strip().casefold()
                    for value in (details["nome"], details["auto_id"]) if value
                }
                if normalized_target not in matched_values:
                    continue
                bounds = control.rectangle()
                key = (
                    details["tipo"], details["nome"].casefold(), details["auto_id"],
                    int(bounds.left), int(bounds.top), int(bounds.right), int(bounds.bottom),
                )
                found[key] = control
            except Exception:
                continue
    return found


def _click_control(
    control: Any, action: str, window: Any | None = None, target: str = ""
) -> dict[str, Any]:
    """Executa a ação de mouse num controle já resolvido, focando antes de clicar."""
    label = target or _control_details(control)["nome"] or "controle"
    try:
        if not control.is_visible():
            raise ValueError(f"controle {label!r} não está visível.")
        if not control.is_enabled():
            raise ValueError(f"controle {label!r} está desabilitado.")
    except AttributeError:
        pass

    # Ativar a janela antes de clicar: numa janela inativa o primeiro clique costuma
    # apenas trazê-la para frente, sem focar o campo, e o texto digitado se perde.
    if window is not None:
        try:
            window.set_focus()
        except Exception:
            pass
    try:
        control.set_focus()
    except Exception:
        pass

    method = "click_input"
    if action == "clicar":
        control_type = _control_details(control)["tipo"]
        # Em campos de texto, InvokePattern não coloca o cursor: o texto digitado
        # depois se perde. Para Edit/ComboBox o clique real é obrigatório.
        invoke = None if control_type in _INPUT_CONTROL_TYPES else getattr(control, "invoke", None)
        if callable(invoke):
            try:
                invoke()
                method = "uia_invoke"
            except (AttributeError, NotImplementedError):
                control.click_input()
            except Exception as exc:
                # Só faz fallback quando o método não está disponível; um erro genérico
                # poderia ter ocorrido depois de a aplicação receber a ação.
                if exc.__class__.__name__ in {"NoPatternInterfaceError", "NoPatternInterface"}:
                    control.click_input()
                else:
                    raise
        else:
            control.click_input()
    elif action == "duplo_clique":
        control.double_click_input()
        method = "double_click_input"
    elif action == "clique_direito":
        control.right_click_input()
        method = "right_click_input"
    else:
        raise ValueError("controle nomeado só aceita clicar, duplo_clique ou clique_direito.")

    bounds = control.rectangle()
    details = _control_details(control)
    return {
        "controle": details["nome"][:180] or label,
        "id_automacao": details["auto_id"][:120],
        "tipo": details["tipo"],
        "metodo": method,
        "centro_x": (int(bounds.left) + int(bounds.right)) // 2,
        "centro_y": (int(bounds.top) + int(bounds.bottom)) // 2,
    }


def _perform_accessible_mouse_action(
    target: str, action: str, window: Any | None = None
) -> dict[str, Any]:
    """Atinge um controle UIA por nome/AutomationId, sem inferir coordenadas da imagem."""
    if not str(target or "").strip():
        raise ValueError("controle precisa conter um nome ou id de automação exato.")
    if window is None:
        window = _active_uia_window()

    found = _match_accessible_controls(window, target)

    if not found:
        raise ValueError(
            f"nenhum controle visível corresponde exatamente a {target!r}; "
            "use o nome/id retornado por capturar_tela ou use coordenadas verificadas."
        )
    if len(found) != 1:
        raise ValueError(
            f"o controle {target!r} corresponde a {len(found)} elementos; "
            "use o id de automação para distinguir o alvo."
        )

    return _click_control(next(iter(found.values())), action, window=window, target=target)


def control_mouse(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del ctx
    try:
        action = str(args.get("acao", "")).strip().lower()
        duration = float(args.get("duracao", 0.08))
        if not 0 <= duration <= 2.0:
            return ToolResult.fail("duracao precisa estar entre 0 e 2 segundos")

        pyautogui = _pyautogui()
        accessible_target = str(args.get("controle", "") or "").strip()
        if accessible_target:
            # Preserve the emergency stop even though UIA can invoke a control
            # without moving the physical cursor.
            if getattr(pyautogui, "FAILSAFE", True):
                position = pyautogui.position()
                point = (int(position[0]), int(position[1]))
                fail_points = getattr(pyautogui, "FAILSAFE_POINTS", [(0, 0)])
                if point in {tuple(item) for item in fail_points}:
                    raise RuntimeError(
                        "Proteção de parada do mouse ativada: mova o cursor para fora dos cantos da tela."
                    )
            if action not in {"clicar", "duplo_clique", "clique_direito"}:
                return ToolResult.fail(
                    "controle nomeado aceita apenas clicar, duplo_clique ou clique_direito"
                )
            performed = _perform_accessible_mouse_action(accessible_target, action)
            return ToolResult(
                ok=True,
                summary=(
                    f"Mouse via UI Automation: {action} no controle "
                    f"{performed['controle']!r} pelo método {performed['metodo']}."
                ),
                data={"acao": action, "origem": "ui_automation", **performed},
            )

        if action == "scroll":
            amount = int(args.get("quantidade_scroll", 0))
            if not -20 <= amount <= 20 or amount == 0:
                return ToolResult.fail("quantidade_scroll precisa estar entre -20 e 20 e não pode ser zero")
            if "x" in args and "y" in args:
                x, y = _check_point(pyautogui, args["x"], args["y"])
                pyautogui.moveTo(x, y, duration=duration)
            pyautogui.scroll(amount)
            return ToolResult(ok=True, summary=f"Rolagem executada ({amount}).", data={"acao": action, "quantidade_scroll": amount})

        x, y = _check_point(pyautogui, args.get("x"), args.get("y"))
        if action == "mover":
            pyautogui.moveTo(x, y, duration=duration)
        elif action in {"clicar", "duplo_clique", "clique_direito"}:
            clicks = 2 if action == "duplo_clique" else 1
            button = "right" if action == "clique_direito" else "left"
            pyautogui.click(x=x, y=y, clicks=clicks, interval=0.12, button=button)
        elif action == "arrastar":
            dest_x, dest_y = _check_point(pyautogui, args.get("destino_x"), args.get("destino_y"), "destino")
            pyautogui.moveTo(x, y, duration=duration)
            pyautogui.dragTo(dest_x, dest_y, duration=max(0.15, duration), button="left")
        else:
            return ToolResult.fail("ação de mouse inválida")
        return ToolResult(
            ok=True,
            summary=f"Mouse: {action} em ({x}, {y}).",
            data={"acao": action, "x": x, "y": y},
        )
    except Exception as exc:
        return ToolResult.fail(f"Ação de mouse não executada: {type(exc).__name__}: {exc}")


def _focused_field_state() -> dict[str, str] | None:
    """Nome, tipo e valor do campo que está com o foco de teclado."""
    if os.name != "nt":
        return None
    try:
        from pywinauto.windows.uia_defines import IUIA
        from pywinauto.windows.uia_element_info import UIAElementInfo

        info = UIAElementInfo(IUIA().iuia.GetFocusedElement())
        name = str(getattr(info, "name", "") or "").strip()
        value = str(getattr(info, "value", "") or "").strip()
        control_type = str(getattr(info, "control_type", "") or "").strip()
        if not (name or value or control_type):
            return None
        return {"nome": name[:180], "tipo": control_type, "valor": value[:180]}
    except Exception:
        return None


def _typing_confirmation(typed: str, state: dict[str, str] | None) -> tuple[bool | None, str]:
    """Compara o que foi digitado com o valor lido no campo em foco.

    Retorna None quando não há como ler o campo (não afirma nem nega), True quando
    o texto chegou e False quando o campo em foco mostra outra coisa.
    """
    if state is None:
        return None, "Não foi possível ler o campo em foco para confirmar o texto."
    name = state.get("nome") or "campo em foco"
    control_type = str(state.get("tipo") or "")
    value = str(state.get("valor") or "").strip()
    wanted = re.sub(r"\s+", " ", str(typed or "")).strip().casefold()
    if not wanted:
        return None, f"{name!r}: nada a confirmar."
    if control_type not in _INPUT_CONTROL_TYPES:
        # Chromium às vezes devolve o documento inteiro como elemento focado mesmo
        # com um campo ativo; afirmar "não entrou" aqui geraria falso negativo.
        return None, (
            f"O foco está em {control_type or 'um elemento'} {name!r}, não em um campo de entrada; "
            "o texto pode não ter entrado. Clique no campo antes de digitar."
        )
    normalized_value = re.sub(r"\s+", " ", value).strip().casefold()
    if wanted in normalized_value:
        return True, f"Confirmei {value!r} em {name!r}."
    if not value:
        return False, (
            f"{name!r} continua vazio: o texto não entrou. "
            "Clique no campo (ou use o controle acessível) antes de digitar de novo."
        )
    return False, f"{name!r} contém {value!r}, não {typed!r}; confira onde o foco estava."


_BLOCKED_HOTKEYS = {
    ("alt", "f4"),
    ("ctrl", "alt", "delete"),
    ("ctrl", "shift", "esc"),
    ("ctrl", "w"),
    ("ctrl", "q"),
    ("ctrl", "shift", "w"),
    ("win", "r"),
    ("win", "x"),
}
_KEY_RE = re.compile(r"^[a-z0-9]$|^f(?:[1-9]|1[0-2])$|^(?:enter|return|esc|escape|tab|space|backspace|delete|insert|home|end|pageup|pagedown|up|down|left|right|capslock|numlock|scrolllock|printscreen|pause|ctrl|control|alt|shift|win|winleft|winright|command)$", re.I)
_KEY_ALIASES = {"control": "ctrl", "return": "enter", "escape": "esc", "winleft": "win", "winright": "win", "command": "win"}


def _normalize_key(value: object) -> str:
    key = str(value).strip().lower()
    return _KEY_ALIASES.get(key, key)


def _send_text(pyautogui: Any, text: str, interval: float = 0.008) -> None:
    """Digita texto via clipboard (mais confiável para Unicode no Windows).

    `write()` não lida com todos os caracteres; o clipboard preserva acentos e
    símbolos. O clipboard anterior é restaurado ao final.
    """
    clipboard_module = None
    old_clipboard = None
    copied = False
    try:
        import pyperclip as clipboard_module

        old_clipboard = clipboard_module.paste()
        clipboard_module.copy(text)
        copied = True
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.15)  # dá tempo para o aplicativo ler o clipboard
    except Exception:
        pyautogui.write(text, interval=interval)
    finally:
        if clipboard_module is not None and copied:
            try:
                clipboard_module.copy(old_clipboard or "")
            except Exception:
                pass


def control_keyboard(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del ctx
    try:
        pyautogui = _pyautogui()
        action = str(args.get("acao", "")).strip().lower()
        if action == "digitar":
            text = str(args.get("texto", ""))
            if not text:
                return ToolResult.fail("texto vazio")
            if len(text) > 1500:
                return ToolResult.fail("texto excede o limite de 1500 caracteres por chamada")
            interval = float(args.get("intervalo", 0.008))
            if not 0 <= interval <= 0.1:
                return ToolResult.fail("intervalo precisa ficar entre 0 e 0.1 segundo")
            _send_text(pyautogui, text, interval=interval)
            # Lê o campo em foco depois de digitar: sem isso o agente anunciava
            # sucesso mesmo quando o texto não entrava em lugar nenhum.
            state = _focused_field_state()
            confirmed, message = _typing_confirmation(text, state)
            return ToolResult(
                ok=confirmed is not False,
                summary=f"Texto enviado ao aplicativo em foco ({len(text)} caracteres). {message}",
                data={
                    "acao": action,
                    "caracteres": len(text),
                    "confirmado": confirmed,
                    "campo_em_foco": state,
                },
            )

        if action == "pressionar":
            raw_key = str(args.get("tecla", "")).strip().lower()
            key = _normalize_key(raw_key)
            if not _KEY_RE.fullmatch(raw_key) or key == "win":
                return ToolResult.fail("tecla não permitida ou inválida")
            pyautogui.press(key)
            return ToolResult(ok=True, summary=f"Tecla pressionada: {key}.", data={"acao": action, "tecla": key})

        if action == "atalho":
            raw_keys = args.get("teclas", [])
            if not isinstance(raw_keys, list) or not 2 <= len(raw_keys) <= 4:
                return ToolResult.fail("atalho precisa conter de 2 a 4 teclas")
            raw_keys = tuple(str(key).strip().lower() for key in raw_keys)
            if any(not _KEY_RE.fullmatch(key) for key in raw_keys):
                return ToolResult.fail("atalho contém tecla inválida")
            keys = tuple(_normalize_key(key) for key in raw_keys)
            canonical = tuple(sorted(set(keys)))
            for blocked in _BLOCKED_HOTKEYS:
                if tuple(sorted(blocked)) == canonical:
                    return ToolResult.fail("atalho bloqueado por proteção; execute essa ação manualmente")
            if "win" in keys:
                return ToolResult.fail("atalhos com tecla Windows estão bloqueados")
            pyautogui.hotkey(*keys)
            return ToolResult(ok=True, summary=f"Atalho enviado: {'+'.join(keys)}.", data={"acao": action, "teclas": list(keys)})

        return ToolResult.fail("ação de teclado inválida")
    except Exception as exc:
        return ToolResult.fail(f"Ação de teclado não executada: {type(exc).__name__}: {exc}")


_MIN_SEARCH_SCORE = 50
_MIN_SEARCH_SCORE_WITH_MAGNIFIER = 42
_SEARCH_AMBIGUITY_MARGIN = 20


def _has_magnifier_evidence(candidate: dict[str, Any]) -> bool:
    return any("lupa" in str(clue).casefold() for clue in candidate.get("evidencias_busca") or [])


def _choose_search_field(candidates: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """Escolhe o campo de busca pela evidência anatômica, não pelo nome do app.

    Age sozinho quando o melhor campo é claro; em empate ou evidência fraca,
    recusa e explica o motivo — melhor devolver candidatos do que digitar no
    campo errado.
    """
    inputs = [
        candidate for candidate in candidates
        if str(candidate.get("tipo") or "") in _INPUT_CONTROL_TYPES
    ]
    inputs.sort(key=lambda item: -int(item.get("pontuacao_busca", 0) or 0))
    if not inputs:
        return None, "não há campo de entrada (Edit/ComboBox) na janela ativa"

    best = inputs[0]
    score = int(best.get("pontuacao_busca", 0) or 0)
    has_magnifier = _has_magnifier_evidence(best)
    if score < _MIN_SEARCH_SCORE and not (has_magnifier and score >= _MIN_SEARCH_SCORE_WITH_MAGNIFIER):
        return None, (
            f"o melhor campo de entrada pontuou {score}, abaixo do mínimo para agir sem confirmação"
            + ("" if has_magnifier else " e sem lupa ao lado")
        )

    if len(inputs) > 1:
        # Só conta como ambiguidade outro campo que também tenha evidência suficiente
        # para ser barra de pesquisa; um campo de texto comum não deve bloquear a ação.
        rivals = [
            candidate for candidate in inputs[1:]
            if int(candidate.get("pontuacao_busca", 0) or 0) >= _MIN_SEARCH_SCORE
            or (
                _has_magnifier_evidence(candidate)
                and int(candidate.get("pontuacao_busca", 0) or 0) >= _MIN_SEARCH_SCORE_WITH_MAGNIFIER
            )
        ]
        if rivals:
            second_score = int(rivals[0].get("pontuacao_busca", 0) or 0)
            if score - second_score < _SEARCH_AMBIGUITY_MARGIN:
                return None, (
                    f"há {len(inputs)} campos com pontuação próxima ({score} e {second_score}); "
                    "não escolho sozinho"
                )

    return best, f"melhor campo com pontuação {score} ({best.get('confianca_busca') or 'sem confiança'})"


def _search_candidates_brief(candidates: list[dict[str, Any]], limit: int = 5) -> str:
    if not candidates:
        return "nenhum candidato"
    parts = []
    for candidate in candidates[:limit]:
        parts.append(
            f"{candidate.get('nome')!r} [{candidate.get('tipo')}] "
            f"pontuação {candidate.get('pontuacao_busca')}"
        )
    return "; ".join(parts)


def _control_for_candidate(window: Any, candidate: dict[str, Any]) -> Any | None:
    """Wrapper UIA do candidato, casado pelo tipo e pelo retângulo exato.

    `from_point` pode devolver um ícone interno (a lupa) no lugar do campo, e
    InvokePattern num ícone não coloca o cursor no texto. Casar o retângulo
    garante que o clique vá no próprio campo de entrada.
    """
    control_type = str(candidate.get("tipo") or "")
    if not control_type:
        return None
    try:
        controls = window.descendants(control_type=control_type)
    except Exception:
        return None
    target = (
        int(candidate.get("esquerda", -1)), int(candidate.get("topo", -1)),
        int(candidate.get("direita", -1)), int(candidate.get("base", -1)),
    )
    for control in controls:
        try:
            rect = control.rectangle()
            bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
            if bounds == target:
                return control
        except Exception:
            continue
    return None


def pesquisar(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Localiza a barra de pesquisa pela anatomia, digita e confirma numa só chamada."""
    del ctx
    try:
        text = str(args.get("texto", "")).strip()
        if not text:
            return ToolResult.fail("texto de busca vazio")
        if len(text) > 1500:
            return ToolResult.fail("texto excede o limite de 1500 caracteres")
        # tecla="" desliga o Enter; omitir usa "enter".
        raw_key = str(args.get("tecla", "enter") or "").strip().lower() if "tecla" in args else "enter"
        submit_key = _normalize_key(raw_key) if raw_key else ""
        if submit_key and (not _KEY_RE.fullmatch(submit_key) or submit_key == "win"):
            return ToolResult.fail("tecla de confirmação inválida ou não permitida")

        snapshot = _active_window_snapshot(limit=80)
        if not snapshot.get("disponivel"):
            return ToolResult.fail(
                "não consegui ler a interface por acessibilidade ("
                + str(snapshot.get("motivo") or "motivo desconhecido")
                + "). Use capturar_tela com imagem e controle os campos manualmente."
            )

        # "elementos" tem os retângulos (necessários para achar o controle exato);
        # "resumo_pesquisa" é o resumo curto e serve de reserva.
        ranked = list(snapshot.get("elementos") or snapshot.get("resumo_pesquisa") or [])
        chosen, reason = _choose_search_field(ranked)
        if chosen is None:
            hint = ""
            if int(snapshot.get("total_elementos_lidos") or 0) == 0:
                hint = (
                    " A janela ativa não expôs nenhum controle: verifique se ela está "
                    "minimizada, atrás de outra janela ou se o aplicativo não oferece acessibilidade."
                )
            return ToolResult.fail(
                f"não identifiquei a barra de pesquisa com segurança: {reason}. "
                f"Candidatos: {_search_candidates_brief(ranked)}. "
                "Se algum for o campo certo, use controlar_mouse com o nome dele." + hint
            )

        window = _active_uia_window()
        pyautogui = _pyautogui()
        control = _control_for_candidate(window, chosen)
        if control is not None:
            performed = _click_control(control, "clicar", window=window, target=str(chosen.get("nome") or ""))
        else:
            # Sem wrapper confiável, o clique usa o centro do retângulo lido do UIA
            # (geometria medida, não estimada da imagem).
            try:
                window.set_focus()
            except Exception:
                pass
            pyautogui.click(int(chosen.get("centro_x", 0)), int(chosen.get("centro_y", 0)))
            time.sleep(0.08)
            performed = {
                "controle": str(chosen.get("nome") or ""),
                "id_automacao": str(chosen.get("id_automacao") or ""),
                "tipo": str(chosen.get("tipo") or ""),
                "metodo": "clique_coordenada_uia",
                "centro_x": int(chosen.get("centro_x", 0)),
                "centro_y": int(chosen.get("centro_y", 0)),
            }

        _send_text(pyautogui, text)
        state = _focused_field_state()
        confirmed, message = _typing_confirmation(text, state)
        focused_in_input = state is not None and str(state.get("tipo") or "") in _INPUT_CONTROL_TYPES
        # Foco fora de um campo (ex.: o documento do Chromium) significa que o
        # clique não colocou o cursor no texto: nada foi digitado de fato.
        succeeded = confirmed is True or (confirmed is None and (state is None or focused_in_input))
        if state is not None and not focused_in_input:
            message += (
                f" O foco ficou em {state.get('tipo')} {state.get('nome')!r}, não no campo de pesquisa; "
                "o texto não entrou. Clique no campo com controlar_mouse e digite de novo."
            )
        # Só confirma no aplicativo se o texto realmente entrou; Enter num campo vazio
        # poderia submeter um formulário ou acionar outro botão.
        submitted = bool(submit_key) and succeeded
        if submitted:
            pyautogui.press(submit_key)
        return ToolResult(
            ok=succeeded,
            summary=(
                f"Pesquisei {text!r} no campo {performed['controle']!r} "
                f"({performed['tipo']}, pontuação {chosen.get('pontuacao_busca')}). {message}"
                + (f" Tecla {submit_key} enviada." if submitted
                   else f" Tecla {submit_key} NÃO enviada porque o texto não foi confirmado."
                   if submit_key else "")
            ),
            data={
                "texto": text,
                "campo": performed,
                "pontuacao_busca": chosen.get("pontuacao_busca"),
                "confianca_busca": chosen.get("confianca_busca"),
                "evidencias_busca": chosen.get("evidencias_busca"),
                "confirmado": confirmed,
                "campo_em_foco": state,
                "tecla_enviada": submit_key if submitted else None,
            },
        )
    except Exception as exc:
        return ToolResult.fail(f"Pesquisa não executada: {type(exc).__name__}: {exc}")


def build_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="capturar_tela",
            description=_TOOL_DESCRIPTION_CAPTURE,
            parameters={
                "type": "object",
                "properties": {
                    "monitor": {"type": "integer", "minimum": 1, "maximum": 16, "description": "Índice de monitor (padrão 1)."},
                    "largura_maxima": {"type": "integer", "minimum": 320, "maximum": 2200, "description": "Largura máxima pedida; padrão 640. Quando a visão local Ollama estiver configurada, valores acima de 640 são limitados a 640 para evitar codificar pixels que serão descartados antes da inferência."},
                    "somente_acessibilidade": {"type": "boolean", "description": "Preferir true para localizar campos/botões e executar tarefas com controles convencionais. Evita pixels e inferência visual se UI Automation retornar controles úteis; sem controles úteis, captura imagem como fallback."},
                    "somente_imagem": {"type": "boolean", "description": "Use true para tarefas puramente visuais (ícones, layout, conteúdo gráfico) ou quando UIA já foi considerada insuficiente; omite a varredura UIA e reduz latência."},
                },
                "additionalProperties": False,
            },
            risk=Risk.L0,
            handler=capture_screen,
            category="desktop",
        ),
        ToolSpec(
            name="controlar_mouse",
            description=_TOOL_DESCRIPTION_MOUSE,
            parameters={
                "type": "object",
                "properties": {
                    "acao": {"type": "string", "enum": ["mover", "clicar", "duplo_clique", "clique_direito", "scroll", "arrastar"]},
                    "controle": {
                        "type": "string", "maxLength": 180,
                        "description": "Opcional: nome EXATO ou AutomationId de um controle listado por capturar_tela. Para clicar/duplo-clique/clique direito, prefira isso quando houver correspondência única; não use rótulos ambíguos.",
                    },
                    "x": {
                        "type": "integer", "minimum": -10000, "maximum": 10000,
                        "description": "Coordenada X absoluta do desktop em pixels. Prefira centro_x da UI Automation; para imagem reduzida, aplique origem e fator de escala da captura. Pode ser negativa somente se estiver dentro dos limites virtuais.",
                    },
                    "y": {
                        "type": "integer", "minimum": -10000, "maximum": 10000,
                        "description": "Coordenada Y absoluta do desktop em pixels. Prefira centro_y da UI Automation; para imagem reduzida, aplique origem e fator de escala da captura. Pode ser negativa somente se estiver dentro dos limites virtuais.",
                    },
                    "destino_x": {
                        "type": "integer", "minimum": -10000, "maximum": 10000,
                        "description": "Coordenada X absoluta do destino no desktop; use a mesma conversão e validação de x.",
                    },
                    "destino_y": {
                        "type": "integer", "minimum": -10000, "maximum": 10000,
                        "description": "Coordenada Y absoluta do destino no desktop; use a mesma conversão e validação de y.",
                    },
                    "quantidade_scroll": {"type": "integer", "minimum": -20, "maximum": 20},
                    "duracao": {
                        "type": "number", "minimum": 0, "maximum": 2,
                        "description": "Duração do movimento do cursor em segundos; padrão 0.08 para interação rápida. Aumente apenas se a interface exigir movimento mais lento.",
                    },
                },
                "required": ["acao"],
                "additionalProperties": False,
            },
            risk=Risk.L1,
            handler=control_mouse,
            category="desktop",
        ),
        ToolSpec(
            name="controlar_teclado",
            description=_TOOL_DESCRIPTION_KEYBOARD,
            parameters={
                "type": "object",
                "properties": {
                    "acao": {"type": "string", "enum": ["digitar", "pressionar", "atalho"]},
                    "texto": {"type": "string", "maxLength": 1500},
                    "tecla": {"type": "string", "maxLength": 24},
                    "teclas": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4},
                    "intervalo": {"type": "number", "minimum": 0, "maximum": 0.1},
                },
                "required": ["acao"],
                "additionalProperties": False,
            },
            risk=Risk.L1,
            handler=control_keyboard,
            category="desktop",
        ),
        ToolSpec(
            name="pesquisar",
            description=_TOOL_DESCRIPTION_SEARCH,
            parameters={
                "type": "object",
                "properties": {
                    "texto": {"type": "string", "minLength": 1, "maxLength": 1500},
                    "tecla": {"type": "string", "maxLength": 24, "default": "enter"},
                },
                "required": ["texto"],
                "additionalProperties": False,
            },
            risk=Risk.L1,
            handler=pesquisar,
            category="desktop",
        ),
    ]

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

_TOOL_DESCRIPTION_CAPTURE = (
    "Captura a tela atual e envia a imagem ao modelo visual, junto com uma leitura local opcional "
    "da janela ativa e dos controles acessíveis do Windows. Use os nomes/retângulos dos controles "
    "quando disponíveis, confirme visualmente e faça nova captura após cada ação importante. "
    "Escolha um monitor pelo índice (1 é o monitor principal/primeiro listado)."
)
_TOOL_DESCRIPTION_MOUSE = (
    "Controla o mouse na área de trabalho atual. Use capturar_tela antes para saber a posição dos elementos; "
    "depois faça uma ação de mouse por chamada e confira o resultado com nova captura. "
    "Coordenadas são pixels absolutos da área de trabalho virtual."
)
_TOOL_DESCRIPTION_KEYBOARD = (
    "Digita texto, pressiona uma tecla ou envia um atalho ao aplicativo em primeiro plano. "
    "Use capturar_tela para confirmar qual janela está ativa e verificar o resultado. "
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
    # O padrão anterior de 120 ms era somado a cada chamada de PyAutoGUI.
    # Mantém uma pausa curta e configurável para apps que precisam de mais tempo.
    try:
        action_pause = float(os.environ.get("AURORA_DESKTOP_ACTION_PAUSE", "0.03"))
    except ValueError:
        action_pause = 0.03
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
            f"{label} ({x}, {y}) fora da área de trabalho ({left}, {top}, {width}, {height})"
        )
    # PyAutoGUI usa o canto superior esquerdo da tela principal como fail-safe.
    if x == 0 and y == 0:
        raise ValueError("coordenada (0, 0) reservada para a proteção de parada do mouse")
    return x, y


def _active_window_snapshot(limit: int = 50) -> dict[str, Any]:
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
            "elementos": [],
            "total_elementos_lidos": 0,
        }
        useful_types = {
            "Button", "Text", "Edit", "Hyperlink", "MenuItem", "TabItem",
            "CheckBox", "RadioButton", "ComboBox", "ListItem", "TreeItem",
            "DataItem", "Document", "Group",
        }
        priority_types = {
            "Button": 0, "MenuItem": 0, "Hyperlink": 0, "Edit": 0,
            "ComboBox": 1, "CheckBox": 1, "RadioButton": 1, "TabItem": 1,
            "ListItem": 2, "TreeItem": 2, "DataItem": 2, "Text": 3,
            "Document": 4, "Group": 5,
        }
        candidates: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()

        if scan_mode == "full":
            # Modo completo preserva a leitura ampla para apps complexos.
            try:
                controls = window.descendants()[:500]
            except Exception:
                controls = []
        else:
            # Varredura rápida: limita profundidade, número de nós e tempo total.
            # Evita percorrer toda a árvore de acessibilidade de navegadores e IDEs.
            from collections import deque

            scan_started = time.perf_counter()
            scan_budget_s = 0.35
            max_scan_nodes = 240
            max_scan_depth = 5
            controls = []
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

        for control in controls[:500]:
            try:
                info = getattr(control, "element_info", None)
                control_type = str(getattr(info, "control_type", "") or "")
                if control_type not in useful_types:
                    continue
                name = str(control.window_text() or getattr(info, "name", "") or "").strip()
                if not name:
                    continue
                name = re.sub(r"\s+", " ", name)[:180]
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
                key = (control_type, name.casefold(), left, top, right, bottom)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append({
                    "_priority": priority_types.get(control_type, 9),
                    "tipo": control_type,
                    "nome": name,
                    "esquerda": left, "topo": top, "direita": right, "base": bottom,
                    "centro_x": (left + right) // 2,
                    "centro_y": (top + bottom) // 2,
                })
            except Exception:
                continue
        candidates.sort(key=lambda item: (item["_priority"], item["topo"], item["esquerda"]))
        snapshot["total_elementos_lidos"] = len(candidates)
        snapshot["elementos"] = [
            {key: value for key, value in item.items() if key != "_priority"}
            for item in candidates[:max(1, min(int(limit), 80))]
        ]
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
    """Captura um monitor e fornece data_url para o agente enviar como imagem ao LLM."""
    del ctx
    try:
        from aurora.tools.screen_capture import capture_monitors
        max_width = int(args.get("largura_maxima", 1920))
        monitor_index = int(args.get("monitor", 1))
        if max_width < 320 or max_width > 2200:
            return ToolResult.fail("largura_maxima deve ficar entre 320 e 2200")
        if monitor_index < 1 or monitor_index > 16:
            return ToolResult.fail("monitor deve ficar entre 1 e 16")

        total_started = time.perf_counter()
        capture_started = time.perf_counter()
        # Captura apenas o monitor solicitado, evitando codificar telas que serão descartadas.
        screens = capture_monitors(max_width=max_width, monitor_index=monitor_index)
        capture_ms = round((time.perf_counter() - capture_started) * 1000)
        if not screens:
            return ToolResult.fail(f"monitor {monitor_index} não encontrado ou indisponível")
        selected = screens[0]
        data_url = str(selected.get("data_url", ""))
        if not data_url.startswith("data:image/"):
            return ToolResult.fail("captura não produziu uma imagem válida")
        uia_started = time.perf_counter()
        ui_snapshot = _active_window_snapshot()
        uia_ms = round((time.perf_counter() - uia_started) * 1000)
        total_ms = round((time.perf_counter() - total_started) * 1000)
        metadata = {
            "tempo_captura_ms": capture_ms,
            "tempo_uia_ms": uia_ms,
            "tempo_total_ms": total_ms,
            "modo_uia": os.environ.get("AURORA_DESKTOP_UIA_MODE", "fast").strip().lower() or "fast",
            "monitor": selected["monitor"],
            "origem_x": selected["left"],
            "origem_y": selected["top"],
            "largura_original": selected["width"],
            "altura_original": selected["height"],
            "largura_capturada": selected["captured_size"][0],
            "altura_capturada": selected["captured_size"][1],
            "mime_type": selected.get("mime_type", "image/jpeg"),
            "interface_acessivel": ui_snapshot,
        }
        ui_count = len(ui_snapshot.get("elementos", []))
        active_title = str(ui_snapshot.get("janela_ativa") or "janela não identificada")
        return ToolResult(
            ok=True,
            summary=(f"Tela capturada: monitor {monitor_index}, "
                     f"{metadata['largura_capturada']}x{metadata['altura_capturada']} "
                     f"(captura {capture_ms} ms, UIA {uia_ms} ms). "
                     f"Janela em primeiro plano: {active_title}. "
                     + (f"Leitura local encontrou {ui_count} controles acessíveis." if ui_snapshot.get("disponivel") else
                        "Leitura local de controles indisponível; use a imagem e não invente nomes de elementos.")
                     + " A imagem será enviada ao modelo para inspeção visual."),
            data={**metadata, "data_url": data_url},
        )
    except Exception as exc:
        return ToolResult.fail(f"Falha ao capturar a tela: {type(exc).__name__}: {exc}")


def control_mouse(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del ctx
    try:
        pyautogui = _pyautogui()
        action = str(args.get("acao", "")).strip().lower()
        duration = float(args.get("duracao", 0.15))
        if not 0 <= duration <= 2.0:
            return ToolResult.fail("duracao precisa estar entre 0 e 2 segundos")
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
            # write() não lida com todos os caracteres Unicode no Windows; clipboard paste é mais confiável.
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
            return ToolResult(ok=True, summary=f"Texto enviado ao aplicativo em foco ({len(text)} caracteres).", data={"acao": action, "caracteres": len(text)})

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


def build_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="capturar_tela",
            description=_TOOL_DESCRIPTION_CAPTURE,
            parameters={
                "type": "object",
                "properties": {
                    "monitor": {"type": "integer", "minimum": 1, "maximum": 16, "description": "Índice de monitor (padrão 1)."},
                    "largura_maxima": {"type": "integer", "minimum": 320, "maximum": 2200, "description": "Largura máxima da imagem enviada ao modelo; padrão 1920 para ler texto pequeno."},
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
                    "x": {"type": "integer", "minimum": -10000, "maximum": 10000},
                    "y": {"type": "integer", "minimum": -10000, "maximum": 10000},
                    "destino_x": {"type": "integer", "minimum": -10000, "maximum": 10000},
                    "destino_y": {"type": "integer", "minimum": -10000, "maximum": 10000},
                    "quantidade_scroll": {"type": "integer", "minimum": -20, "maximum": 20},
                    "duracao": {"type": "number", "minimum": 0, "maximum": 2},
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
    ]

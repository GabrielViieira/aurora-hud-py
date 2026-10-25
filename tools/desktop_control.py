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
    "Captura a tela atual sem salvar a imagem no disco e entrega a imagem ao modelo visual. "
    "Use antes de decidir onde clicar; após uma ação importante, capture novamente para verificar. "
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
    pyautogui.PAUSE = 0.12
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


def capture_screen(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Captura um monitor e fornece data_url para o agente enviar como imagem ao LLM."""
    del ctx
    try:
        from aurora.tools.screen_capture import capture_monitors
        max_width = int(args.get("largura_maxima", 1440))
        monitor_index = int(args.get("monitor", 1))
        if max_width < 320 or max_width > 2200:
            return ToolResult.fail("largura_maxima deve ficar entre 320 e 2200")
        screens = capture_monitors(max_width=max_width)
        if not screens:
            return ToolResult.fail("nenhum monitor disponível para captura")
        selected = next((s for s in screens if int(s.get("monitor", -1)) == monitor_index), None)
        if selected is None:
            return ToolResult.fail(
                f"monitor {monitor_index} não encontrado; monitores disponíveis: "
                + ", ".join(str(s.get("monitor")) for s in screens)
            )
        data_url = str(selected.get("data_url", ""))
        if not data_url.startswith("data:image/"):
            return ToolResult.fail("captura não produziu uma imagem válida")
        metadata = {
            "monitor": selected["monitor"],
            "origem_x": selected["left"],
            "origem_y": selected["top"],
            "largura_original": selected["width"],
            "altura_original": selected["height"],
            "largura_capturada": selected["captured_size"][0],
            "altura_capturada": selected["captured_size"][1],
            "mime_type": selected.get("mime_type", "image/jpeg"),
        }
        return ToolResult(
            ok=True,
            summary=(f"Tela capturada: monitor {monitor_index}, "
                     f"{metadata['largura_capturada']}x{metadata['altura_capturada']}. "
                     "A imagem será enviada ao modelo para inspeção visual."),
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
                    "largura_maxima": {"type": "integer", "minimum": 320, "maximum": 2200, "description": "Largura máxima da imagem enviada ao modelo."},
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

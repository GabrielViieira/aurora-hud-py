"""Gerenciador de aplicativos Windows com resolução conservadora e sem cliques cegos na bandeja.

A automação usa nomes/processos/atalhos conhecidos. Para Steam, usa steam://open/main
quando o processo já existe; nunca escolhe um ícone anônimo da bandeja por aproximação.
"""
from __future__ import annotations

import csv
import os
import re
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Any

from aurora.config.permissions import Risk
from aurora.tools.base import ToolContext, ToolResult, ToolSpec

_ALIASES: dict[str, tuple[str, ...]] = {
    "steam": ("steam", "steam.exe", "valve steam"),
    "chrome": ("chrome", "chrome.exe", "google chrome"),
    "google chrome": ("chrome", "chrome.exe", "google chrome"),
    "edge": ("msedge", "msedge.exe", "microsoft edge", "edge"),
    "microsoft edge": ("msedge", "msedge.exe", "microsoft edge", "edge"),
    "vscode": ("code", "code.exe", "visual studio code", "vs code"),
    "vs code": ("code", "code.exe", "visual studio code", "vs code"),
    "visual studio code": ("code", "code.exe", "visual studio code", "vs code"),
    "discord": ("discord", "discord.exe", "discordapp"),
    "spotify": ("spotify", "spotify.exe"),
    "obs": ("obs64", "obs64.exe", "obs studio", "obs"),
    "obs studio": ("obs64", "obs64.exe", "obs studio", "obs"),
    "telegram": ("telegram", "telegram.exe", "telegram desktop"),
    "whatsapp": ("whatsapp", "whatsapp.exe"),
    "bloco de notas": ("notepad", "notepad.exe", "bloco de notas"),
    "notepad": ("notepad", "notepad.exe", "bloco de notas"),
    "calculadora": ("calc", "calc.exe", "calculator", "calculadora"),
    "calculator": ("calc", "calc.exe", "calculator", "calculadora"),
    "paint": ("mspaint", "mspaint.exe", "paint"),
    "explorador de arquivos": ("explorer", "explorer.exe", "file explorer"),
    "file explorer": ("explorer", "explorer.exe", "file explorer"),
}


def _norm(value: object) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch)).casefold()
    raw = re.sub(r"\.(exe|lnk|url)$", "", raw)
    return re.sub(r"[^a-z0-9]+", "", raw)


def _aliases(query: str) -> set[str]:
    base = _norm(query)
    result = {base} if base else set()
    for key, values in _ALIASES.items():
        key_norm = _norm(key)
        value_norms = {_norm(v) for v in values}
        if base == key_norm or base in value_norms:
            result.update(value_norms)
            result.add(key_norm)
    return {v for v in result if v}


def _is_steam(query: str) -> bool:
    return _norm(query) in _aliases("steam")


def _start_menu_roots() -> list[Path]:
    candidates = [
        Path(os.environ.get("APPDATA", r"C:\Users\Default\AppData\Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("USERPROFILE", r"C:\Users\Default")) / "Desktop",
        Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop",
    ]
    return [p for p in candidates if p.is_dir()]


def _shortcuts() -> list[Path]:
    items: list[Path] = []
    seen: set[str] = set()
    for root in _start_menu_roots():
        try:
            for path in root.rglob("*.lnk"):
                try:
                    key = os.path.normcase(str(path.resolve()))
                    if path.is_file() and key not in seen:
                        seen.add(key)
                        items.append(path)
                    if len(items) >= 12000:
                        return items
                except OSError:
                    continue
        except OSError:
            continue
    return items


def _find_shortcuts(query: str, limit: int = 8) -> tuple[list[Path], list[str]]:
    names = _aliases(query)
    if not names:
        return [], []
    exact: list[Path] = []
    fuzzy: list[tuple[int, Path]] = []
    for path in _shortcuts():
        stem = _norm(path.stem)
        if not stem:
            continue
        if stem in names:
            exact.append(path)
        elif any(len(n) >= 4 and (n in stem or stem in n) for n in names):
            # Correspondências aproximadas só são sugestões, nunca executadas automaticamente.
            score = max(min(len(n), len(stem)) for n in names if len(n) >= 4 and (n in stem or stem in n))
            fuzzy.append((score, path))
    exact.sort(key=lambda p: (len(p.stem), str(p).casefold()))
    if exact:
        unique = {str(p.resolve()).casefold(): p for p in exact}
        matches = list(unique.values())
        if len(matches) == 1:
            return matches, []
        # Atalhos duplicados para o mesmo nome são equivalentes: usar o primeiro é seguro.
        same_names = {_norm(p.stem) for p in matches}
        if len(same_names) == 1:
            return [matches[0]], []
        return [], sorted({p.stem for p in matches})[:limit]
    if fuzzy:
        fuzzy.sort(key=lambda x: (-x[0], x[1].stem.casefold(), str(x[1]).casefold()))
        top_score = fuzzy[0][0]
        top = [p for score, p in fuzzy if score == top_score]
        return [], sorted({p.stem for p in top})[:limit]
    return [], []


def _steam_path() -> Path | None:
    candidates: list[Path] = []
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / ("Programs/Steam/steam.exe" if env == "LOCALAPPDATA" else "Steam/steam.exe"))
    candidates.extend([Path(r"C:\Program Files (x86)\Steam\steam.exe"), Path(r"C:\Program Files\Steam\steam.exe")])
    if os.name == "nt":
        try:
            import winreg
            keys = (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
            )
            for hive, key_name, value_name in keys:
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        value, _ = winreg.QueryValueEx(key, value_name)
                    if value:
                        candidates.append(Path(value) / "steam.exe")
                except OSError:
                    pass
        except ImportError:
            pass
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _processes() -> list[dict[str, str]]:
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=8, shell=False, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            return []
        import io
        text = result.stdout.decode("utf-8-sig", errors="replace")
        return [{"nome": row[0], "pid": row[1]} for row in csv.reader(io.StringIO(text)) if len(row) >= 2]
    except (OSError, subprocess.TimeoutExpired):
        return []


def _matching_processes(query: str) -> list[dict[str, str]]:
    names = _aliases(query)
    return [p for p in _processes() if _norm(p["nome"]) in names]


def _window_records() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    windows: list[dict[str, Any]] = []
    # OpenProcess/QueryFullProcessImageName identifies the owning executable rather than guessing from title text.
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def image_for_pid(pid: int) -> str:
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(2048)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
        except Exception:
            pass
        finally:
            kernel32.CloseHandle(handle)
        return ""

    def callback(hwnd, _lparam):
        try:
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if not title:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            exe_path = image_for_pid(int(pid.value))
            windows.append({
                "hwnd": int(hwnd), "pid": int(pid.value), "titulo": title,
                "executavel": Path(exe_path).name if exe_path else "",
                "caminho": exe_path,
                "visivel": bool(user32.IsWindowVisible(hwnd)),
                "minimizada": bool(user32.IsIconic(hwnd)),
            })
        except Exception:
            pass
        return True
    try:
        user32.EnumWindows(enum_proc(callback), 0)
    except Exception:
        return []
    return windows


def _restore_window(query: str) -> dict[str, Any] | None:
    names = _aliases(query)
    records = _window_records()
    # Match executable name first. Only use an exact title as a fallback; no substring title matching.
    matches = [r for r in records if _norm(r["executavel"]) in names]
    if not matches:
        matches = [r for r in records if _norm(r["titulo"]) in names]
    if not matches:
        return None
    matches.sort(key=lambda r: (bool(r["visivel"]), bool(r["minimizada"]), len(r["titulo"])), reverse=True)
    user32 = __import__("ctypes").windll.user32
    user32.ShowWindow(matches[0]["hwnd"], 9)  # SW_RESTORE
    user32.BringWindowToTop(matches[0]["hwnd"])
    user32.SetForegroundWindow(matches[0]["hwnd"])
    time.sleep(0.2)
    matches[0]["visivel"] = bool(user32.IsWindowVisible(matches[0]["hwnd"]))
    return matches[0] if matches[0]["visivel"] else None


def _launch_steam() -> ToolResult:
    if os.name != "nt":
        return ToolResult.fail("A abertura do Steam só é suportada no Windows.")
    running = _matching_processes("steam")
    if running:
        # Método oficial do Steam para pedir à instância existente a janela principal.
        try:
            os.startfile("steam://open/main")  # type: ignore[attr-defined]
        except OSError as exc:
            return ToolResult.fail(f"O Steam está em execução, mas não consegui enviar steam://open/main: {exc}")
        time.sleep(1.2)
        window = _restore_window("steam")
        if window:
            return ToolResult(ok=True, summary="Steam já estava em execução; solicitei a janela principal e confirmei uma janela Steam visível.", data={"metodo": "steam-uri", "janela": window["titulo"]})
        # Não alegar sucesso total e, principalmente, não clicar num ícone de bandeja adivinhado.
        return ToolResult.fail(
            "Enviei steam://open/main para a instância existente, mas o Windows não expôs uma janela principal Steam confirmável. "
            "Não cliquei em nenhum ícone da bandeja para evitar acionar outro aplicativo. "
            "Confira uma vez manualmente se o Steam está minimizado/ocupado e tente de novo.",
            metodo="steam-uri", processos=running[:8],
        )
    executable = _steam_path()
    if not executable:
        return ToolResult.fail("Não encontrei steam.exe nas pastas comuns nem no Registro do Windows. Abra o Steam manualmente uma vez para confirmar a instalação.")
    try:
        subprocess.Popen([str(executable)], cwd=str(executable.parent), shell=False,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return ToolResult(ok=True, summary="Iniciei o Steam pelo steam.exe instalado. Aguarde a inicialização e depois peça para confirmar a tela.", data={"metodo": "exe", "executavel": str(executable), "verificado": False})
    except OSError as exc:
        return ToolResult.fail(f"Não consegui iniciar o Steam por {executable}: {exc}")


def list_apps(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del ctx
    query = str(args.get("filtro") or "").strip()
    limit = min(max(int(args.get("max_resultados", 40)), 1), 100)
    names = _aliases(query) if query else set()
    shortcuts = _shortcuts()
    if query:
        shortcuts = [p for p in shortcuts if _norm(p.stem) in names or any(n in _norm(p.stem) for n in names if len(n) >= 4)]
    windows = _window_records()
    if query:
        windows = [w for w in windows if _norm(w["executavel"]) in names or _norm(w["titulo"]) in names]
    processes = _processes()
    if query:
        processes = [p for p in processes if _norm(p["nome"]) in names]
    data: dict[str, Any] = {
        "atalhos": [{"nome": p.stem, "caminho": str(p)} for p in shortcuts[:limit]],
        "janelas": [{k: w[k] for k in ("titulo", "executavel", "visivel", "minimizada", "pid")} for w in windows[:limit]],
        "processos": processes[:limit],
        "nota": "A bandeja não é clicada automaticamente: ícones sem nome acessível podem apontar para o aplicativo errado.",
    }
    steam = _steam_path() if not query or _is_steam(query) else None
    if steam:
        data["steam_exe"] = str(steam)
    return ToolResult(ok=True, summary=f"Encontrei {len(shortcuts)} atalho(s), {len(windows)} janela(s) e {len(processes)} processo(s) correspondente(s).", data=data)


def open_app(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del ctx
    query = str(args.get("nome") or "").strip()
    if not query or len(query) > 100:
        return ToolResult.fail("Informe o nome do aplicativo (máximo 100 caracteres).")
    if os.name != "nt":
        return ToolResult.fail("Esta ferramenta só é compatível com Windows.")
    if _is_steam(query):
        return _launch_steam()
    try:
        existing = _restore_window(query)
        if existing:
            return ToolResult(ok=True, summary=f"Restaurei a janela existente '{existing['titulo']}'.", data={"janela": existing["titulo"], "executavel": existing["executavel"]})
        running = _matching_processes(query)
        if running:
            return ToolResult.fail(
                f"O processo {', '.join(p['nome'] for p in running[:5])} está ativo, mas não há janela correspondente confirmável. "
                "Não cliquei na bandeja nem abri outra instância, para não acionar o programa errado."
            )
        shortcuts, suggestions = _find_shortcuts(query)
        if suggestions:
            return ToolResult.fail(f"Encontrei nomes parecidos com '{query}', mas nenhum atalho exato. Diga qual é o correto.", candidatos=suggestions)
        if not shortcuts:
            return ToolResult.fail(f"Não encontrei atalho exato para '{query}' no Menu Iniciar/área de trabalho. Use listar_aplicativos para conferir os nomes.")
        os.startfile(str(shortcuts[0]))  # type: ignore[attr-defined]
        return ToolResult(ok=True, summary=f"Solicitei ao Windows a abertura do atalho '{shortcuts[0].stem}'. A abertura ainda precisa ser confirmada visualmente.", data={"atalho": str(shortcuts[0]), "verificado": False})
    except Exception as exc:
        return ToolResult.fail(f"Falha ao abrir '{query}': {type(exc).__name__}: {exc}")


def restore_app(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del ctx
    query = str(args.get("nome") or "").strip()
    if not query or len(query) > 100:
        return ToolResult.fail("Informe o nome do aplicativo (máximo 100 caracteres).")
    if os.name != "nt":
        return ToolResult.fail("Esta ferramenta só é compatível com Windows.")
    if _is_steam(query):
        return _launch_steam()
    try:
        window = _restore_window(query)
        if window:
            return ToolResult(ok=True, summary=f"Restaurei a janela '{window['titulo']}' e trouxe-a para frente.", data={"janela": window["titulo"], "executavel": window["executavel"], "minimizada_antes": window["minimizada"]})
        running = _matching_processes(query)
        if running:
            return ToolResult.fail(f"O processo {', '.join(p['nome'] for p in running[:5])} está ativo, mas nenhuma janela compatível foi encontrada. Não cliquei em ícones de bandeja sem identificação.")
        return ToolResult.fail(f"Não encontrei janela nem processo correspondente a '{query}'. Use abrir_aplicativo para iniciá-lo.")
    except Exception as exc:
        return ToolResult.fail(f"Falha ao restaurar '{query}': {type(exc).__name__}: {exc}")


def build_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="listar_aplicativos",
            description="Lista atalhos, processos e janelas abertas do Windows. Use filtro para encontrar um nome exato. Não clica na bandeja.",
            parameters={"type": "object", "properties": {"filtro": {"type": "string", "maxLength": 100}, "max_resultados": {"type": "integer", "minimum": 1, "maximum": 100}}, "additionalProperties": False},
            risk=Risk.L0, handler=list_apps, category="desktop",
        ),
        ToolSpec(
            name="abrir_aplicativo",
            description="Abre um aplicativo pelo atalho exato do Menu Iniciar. Steam tem fluxo específico via steam.exe/steam://open/main. Não adivinha nem clica em ícones anônimos da bandeja; verifica processos/janelas e relata falhas.",
            parameters={"type": "object", "properties": {"nome": {"type": "string", "minLength": 1, "maxLength": 100}}, "required": ["nome"], "additionalProperties": False},
            risk=Risk.L1, handler=open_app, category="desktop",
        ),
        ToolSpec(
            name="restaurar_aplicativo",
            description="Restaura uma janela minimizada usando a identidade do processo/título exato. Para Steam usa steam://open/main. Não clica em ícones de bandeja sem identificação.",
            parameters={"type": "object", "properties": {"nome": {"type": "string", "minLength": 1, "maxLength": 100}}, "required": ["nome"], "additionalProperties": False},
            risk=Risk.L0, handler=restore_app, category="desktop",
        ),
    ]

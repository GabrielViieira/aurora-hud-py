"""Ajuda nativa para manter a HUD acima de janelas comuns no Windows."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from functools import lru_cache

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
TOPMOST_FLAGS = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW


@lru_cache(maxsize=1)
def _load_user32():
    if os.name != "nt":
        return None
    return ctypes.WinDLL("user32", use_last_error=True)


def set_window_topmost(hwnd: int, *, user32=None) -> bool:
    """Define HWND_TOPMOST com NOACTIVATE; aceita uma API fake nos testes."""
    if not hwnd:
        return False

    api = user32 if user32 is not None else _load_user32()
    if api is None:
        return False

    function = api.SetWindowPos
    try:
        function.argtypes = (
            wintypes.HWND,
            wintypes.HWND,
            wintypes.INT,
            wintypes.INT,
            wintypes.INT,
            wintypes.INT,
            wintypes.UINT,
        )
        function.restype = wintypes.BOOL
    except (AttributeError, TypeError):
        # Funções simuladas nos testes podem não expor argtypes/restype.
        pass

    try:
        return bool(
            function(
                wintypes.HWND(hwnd),
                wintypes.HWND(HWND_TOPMOST),
                0,
                0,
                0,
                0,
                TOPMOST_FLAGS,
            )
        )
    except (OSError, ValueError, TypeError, ctypes.ArgumentError):
        return False

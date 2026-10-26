"""Regression tests for low-latency desktop capture modes."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
if "aurora" in sys.modules:
    _package = sys.modules["aurora"]
    _paths = getattr(_package, "__path__", None)
    if _paths is not None and str(_REPO_ROOT) not in _paths:
        _paths.insert(0, str(_REPO_ROOT))
else:
    _package = types.ModuleType("aurora")
    _package.__path__ = [str(_REPO_ROOT)]
    sys.modules["aurora"] = _package

sys.path.insert(0, str(_REPO_ROOT.parent))

from aurora.core.agent import (
    _should_use_accessibility_only_verification,
    _verification_settle_seconds,
)
from aurora.tools import desktop_control
from aurora.tools.base import ToolResult


def _screen() -> dict:
    return {
        "monitor": 1,
        "left": 0,
        "top": 0,
        "width": 1920,
        "height": 1080,
        "captured_size": (960, 540),
        "mime_type": "image/jpeg",
        "data_url": "data:image/jpeg;base64,AAAA",
    }


class DesktopCaptureFastTests(unittest.TestCase):
    @patch("aurora.tools.desktop_control._virtual_bounds", return_value=(0, 0, 1920, 1080))
    @patch("aurora.tools.desktop_control._pyautogui", return_value=object())
    @patch("aurora.tools.desktop_control._active_window_snapshot")
    @patch("aurora.tools.screen_capture.capture_monitors", return_value=[_screen()])
    def test_image_only_skips_accessibility_scan(
        self, capture_monitors, active_snapshot, _pyautogui, _virtual_bounds
    ):
        result = desktop_control.capture_screen(None, {"somente_imagem": True})

        self.assertTrue(result.ok, result.error)
        active_snapshot.assert_not_called()
        capture_monitors.assert_called_once_with(max_width=640, monitor_index=1)
        self.assertEqual(result.data["tempo_uia_ms"], 0)
        self.assertEqual(result.data["modo_uia"], "omitida_captura_rapida")
        self.assertTrue(result.data["data_url"].startswith("data:image/"))

    @patch("aurora.tools.desktop_control._virtual_bounds", return_value=(0, 0, 1920, 1080))
    @patch("aurora.tools.desktop_control._pyautogui", return_value=object())
    @patch(
        "aurora.tools.desktop_control._active_window_snapshot",
        return_value={
            "disponivel": True,
            "janela_ativa": "Aplicativo de teste",
            "elementos": [{"tipo": "Button", "nome": "OK", "centro_x": 100, "centro_y": 50}],
            "resumo_pesquisa": [],
            "total_elementos_lidos": 1,
        },
    )
    @patch("aurora.tools.screen_capture.capture_monitors")
    def test_accessibility_only_avoids_pixels_when_controls_exist(
        self, capture_monitors, active_snapshot, _pyautogui, _virtual_bounds
    ):
        result = desktop_control.capture_screen(None, {"somente_acessibilidade": True})

        self.assertTrue(result.ok, result.error)
        active_snapshot.assert_called_once()
        capture_monitors.assert_not_called()
        self.assertTrue(result.data["somente_acessibilidade"])
        self.assertNotIn("data_url", result.data)

    @patch("aurora.tools.desktop_control._virtual_bounds", return_value=(0, 0, 1920, 1080))
    @patch("aurora.tools.desktop_control._pyautogui", return_value=object())
    @patch(
        "aurora.tools.desktop_control._active_window_snapshot",
        return_value={
            "disponivel": False,
            "motivo": "UIA indisponível",
            "elementos": [],
            "resumo_pesquisa": [],
        },
    )
    @patch("aurora.tools.screen_capture.capture_monitors", return_value=[_screen()])
    def test_accessibility_only_falls_back_to_image_when_uia_has_no_controls(
        self, capture_monitors, active_snapshot, _pyautogui, _virtual_bounds
    ):
        result = desktop_control.capture_screen(None, {"somente_acessibilidade": True})

        self.assertTrue(result.ok, result.error)
        active_snapshot.assert_called_once()
        capture_monitors.assert_called_once_with(max_width=640, monitor_index=1)
        self.assertTrue(result.data["data_url"].startswith("data:image/"))


    def test_tool_content_does_not_embed_raw_image_data_url(self):
        result = ToolResult(
            ok=True,
            summary="Captura de tela concluída.",
            data={
                "monitor": 1,
                "data_url": "data:image/jpeg;base64," + ("A" * 12000),
            },
        )

        content = result.to_content(max_chars=20000)

        self.assertIn('"monitor": 1', content)
        self.assertNotIn("data:image/jpeg;base64", content)
        self.assertNotIn("A" * 200, content)


class VerificationPolicyTests(unittest.TestCase):
    """A verificação pós-ação deve preferir acessibilidade antes de gastar uma inferência visual."""

    def test_key_press_is_verified_by_accessibility(self):
        self.assertTrue(
            _should_use_accessibility_only_verification(
                "controlar_teclado", {"acao": "pressionar", "tecla": "enter"}
            )
        )

    def test_typing_is_verified_by_accessibility(self):
        self.assertTrue(
            _should_use_accessibility_only_verification(
                "controlar_teclado", {"acao": "digitar", "texto": "Counter Strike"}
            )
        )

    def test_coordinate_click_is_verified_by_accessibility(self):
        self.assertTrue(
            _should_use_accessibility_only_verification(
                "controlar_mouse", {"acao": "clicar", "x": 1500, "y": 240}
            )
        )

    def test_mouse_move_is_not_verified_as_an_action(self):
        self.assertFalse(
            _should_use_accessibility_only_verification(
                "controlar_mouse", {"acao": "mover", "x": 10, "y": 20}
            )
        )

    def test_app_launch_still_uses_image_verification(self):
        self.assertFalse(
            _should_use_accessibility_only_verification("abrir_aplicativo", {"nome": "Steam"})
        )

    def test_click_settles_longer_than_typing_before_reading_state(self):
        click = _verification_settle_seconds("controlar_mouse", {"acao": "clicar", "x": 1, "y": 2})
        typing = _verification_settle_seconds("controlar_teclado", {"acao": "digitar"})
        launch = _verification_settle_seconds("abrir_aplicativo", {"nome": "Steam"})

        self.assertGreater(click, typing)
        self.assertGreaterEqual(click, 0.10)
        self.assertEqual(launch, 0.45)


if __name__ == "__main__":
    unittest.main()

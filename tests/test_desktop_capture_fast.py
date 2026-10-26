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


class _FakeRect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _FakeInfo:
    def __init__(self, control_type: str, name: str = "") -> None:
        self.control_type = control_type
        self.name = name


class _FakeControl:
    def __init__(self, control_type: str, name: str = "", children=()) -> None:
        self.element_info = _FakeInfo(control_type, name)
        self._name = name
        self._children = list(children)
        self._rect = _FakeRect(0, 0, 100, 30)

    def window_text(self) -> str:
        return self._name

    def rectangle(self):
        return self._rect

    def children(self):
        return list(self._children)


class _FakeWindow(_FakeControl):
    def __init__(self, children=(), deep: dict[str, list] | None = None) -> None:
        super().__init__("Window", "Steam", children)
        self.deep = deep or {}

    def descendants(self, control_type: str | None = None, **kwargs):
        return list(self.deep.get(control_type, []))


class FastScanCollectionTests(unittest.TestCase):
    """O BFS raso não alcança campos de busca do Chromium; o FindAll dirigido sim."""

    def test_fast_scan_reaches_deep_input_control(self):
        combo = _FakeControl("ComboBox", "Search the store")
        shallow = _FakeControl("Text", "Browse")
        window = _FakeWindow(children=[shallow], deep={"ComboBox": [combo]})

        controls = desktop_control._collect_scan_controls(window, "fast")

        self.assertIn(combo, controls)
        self.assertIn(shallow, controls)

    def test_fast_scan_keeps_working_when_targeted_search_fails(self):
        shallow = _FakeControl("Text", "Browse")
        window = _FakeWindow(children=[shallow])

        def boom(control_type=None, **kwargs):
            raise RuntimeError("UIA indisponível")

        window.descendants = boom
        controls = desktop_control._collect_scan_controls(window, "fast")

        self.assertIn(shallow, controls)


class _StrictWindow:
    """Simula o backend UIA real: `descendants()` só aceita os critérios suportados.

    O código antigo passava `title=`, que o pywinauto 1.0 rejeita com TypeError;
    o erro era engolido e o controle "não existia".
    """

    def __init__(self, controls=(), calls: list | None = None) -> None:
        self.controls = list(controls)
        self.calls = calls if calls is not None else []

    def window_text(self) -> str:
        return "Steam"

    def rectangle(self):
        return _FakeRect(0, 0, 1920, 1040)

    def set_focus(self):
        self.calls.append("window.set_focus")

    def descendants(self, auto_id: str | None = None, name: str | None = None):
        for control in self.controls:
            info = control.element_info
            if auto_id is not None and str(getattr(info, "auto_id", "")) == auto_id:
                yield_control = True
            elif name is not None and control.window_text() == name:
                yield_control = True
            else:
                yield_control = False
            if yield_control:
                yield_from = [control]
                break
        else:
            yield_from = []
        return yield_from


class _ClickableControl(_FakeControl):
    def __init__(
        self,
        control_type: str,
        name: str = "",
        auto_id: str = "",
        calls: list | None = None,
    ) -> None:
        super().__init__(control_type, name)
        self.element_info.auto_id = auto_id
        self.element_info.legacy_help = ""
        self.element_info.class_name = ""
        self.calls = calls if calls is not None else []
        self._clickable_rect = _FakeRect(954, 98, 1396, 132)

    def rectangle(self):
        return self._clickable_rect

    def is_visible(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    def set_focus(self):
        self.calls.append("control.set_focus")

    def click_input(self):
        self.calls.append("control.click_input")


class AccessibleControlLookupTests(unittest.TestCase):
    def test_finds_control_by_exact_name(self):
        combo = _ClickableControl("ComboBox", "Search the store")
        window = _StrictWindow([combo])

        found = desktop_control._match_accessible_controls(window, "Search the store")

        self.assertEqual(list(found.values()), [combo])

    def test_finds_control_by_auto_id(self):
        field = _ClickableControl("Edit", "", auto_id="search_input")
        window = _StrictWindow([field])

        found = desktop_control._match_accessible_controls(window, "search_input")

        self.assertEqual(list(found.values()), [field])

    def test_ignores_controls_with_other_names(self):
        other = _ClickableControl("Button", "Browse")
        window = _StrictWindow([other])

        self.assertEqual(desktop_control._match_accessible_controls(window, "Search the store"), {})

    def test_focuses_window_and_control_before_clicking(self):
        calls: list[str] = []
        combo = _ClickableControl("ComboBox", "Search the store", calls=calls)
        window = _StrictWindow([combo], calls=calls)

        desktop_control._perform_accessible_mouse_action("Search the store", "clicar", window=window)

        self.assertLess(calls.index("window.set_focus"), calls.index("control.set_focus"))
        self.assertLess(calls.index("control.set_focus"), calls.index("control.click_input"))

    def test_reads_real_pywinauto_attribute_names(self):
        combo = _ClickableControl("ComboBox", "Search the store", auto_id="search_box")
        combo.element_info.legacy_help = "Type to search"

        details = desktop_control._control_details(combo)

        self.assertEqual(details["auto_id"], "search_box")
        self.assertEqual(details["ajuda"], "Type to search")


class TypingConfirmationTests(unittest.TestCase):
    def test_confirms_text_that_reached_the_field(self):
        confirmed, message = desktop_control._typing_confirmation(
            "counter strike",
            {"nome": "Search the store", "tipo": "ComboBox", "valor": "counter strike"},
        )

        self.assertTrue(confirmed)
        self.assertIn("counter strike", message)

    def test_flags_empty_field_after_typing(self):
        confirmed, message = desktop_control._typing_confirmation(
            "counter strike", {"nome": "Search the store", "tipo": "ComboBox", "valor": ""}
        )

        self.assertFalse(confirmed)
        self.assertIn("vazio", message)

    def test_focus_outside_an_input_is_reported_as_uncertain(self):
        # O Steam devolve o documento inteiro quando nada está focado.
        confirmed, message = desktop_control._typing_confirmation(
            "counter strike",
            {"nome": "Steam", "tipo": "Document", "valor": "https://steamloopback.host/"},
        )

        self.assertIsNone(confirmed)
        self.assertIn("não em um campo de entrada", message)

    def test_is_neutral_when_the_field_value_is_unreadable(self):
        confirmed, _ = desktop_control._typing_confirmation("counter strike", None)

        self.assertIsNone(confirmed)


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


def _search_candidate(tipo: str, nome: str, score: int, *, magnifier: bool = False, **extra) -> dict:
    evidence = ["campo é um campo editável"] if tipo in {"Edit", "ComboBox"} else []
    if magnifier:
        evidence.append(
            "há uma lupa (ícone ou botão de busca) dentro ou ao lado do campo; "
            "é o padrão típico de barra de pesquisa"
        )
    return {
        "tipo": tipo,
        "nome": nome,
        "pontuacao_busca": score,
        "confianca_busca": "alta" if score >= 70 else "média" if score >= 50 else "baixa",
        "evidencias_busca": evidence,
        "centro_x": 100,
        "centro_y": 100,
        **extra,
    }


class SearchFieldChoiceTests(unittest.TestCase):
    def test_named_steam_field_wins(self):
        candidates = [
            _search_candidate("ComboBox", "Search the store", 97, magnifier=True),
            _search_candidate("Button", "Search", 56),
        ]

        chosen, reason = desktop_control._choose_search_field(candidates)

        self.assertEqual(chosen["nome"], "Search the store")
        self.assertIn("97", reason)

    def test_unnamed_field_with_magnifier_is_chosen_alone(self):
        candidates = [_search_candidate("ComboBox", "[Campo de texto sem nome]", 45, magnifier=True)]

        chosen, _ = desktop_control._choose_search_field(candidates)

        self.assertIsNotNone(chosen)

    def test_weak_unnamed_field_without_magnifier_is_refused(self):
        candidates = [_search_candidate("Edit", "[Campo de texto sem nome]", 41)]

        chosen, reason = desktop_control._choose_search_field(candidates)

        self.assertIsNone(chosen)
        self.assertIn("sem lupa", reason)

    def test_close_scores_are_refused_as_ambiguous(self):
        candidates = [
            _search_candidate("Edit", "Buscar produtos", 80, magnifier=True),
            _search_candidate("Edit", "Mensagem", 70, magnifier=True),
        ]

        chosen, reason = desktop_control._choose_search_field(candidates)

        self.assertIsNone(chosen)
        self.assertIn("não escolho sozinho", reason)

    def test_clear_winner_beats_a_distant_second(self):
        candidates = [
            _search_candidate("Edit", "Pesquisar no site", 88, magnifier=True),
            _search_candidate("Edit", "Comentário", 40),
        ]

        chosen, _ = desktop_control._choose_search_field(candidates)

        self.assertEqual(chosen["nome"], "Pesquisar no site")

    def test_weak_second_field_does_not_block_a_magnifier_candidate(self):
        # Um campo de texto comum (sem lupa, 41) não deve impedir a busca legítima (45 + lupa).
        candidates = [
            _search_candidate("ComboBox", "[Campo de texto sem nome]", 45, magnifier=True),
            _search_candidate("Edit", "Comentário", 41),
        ]

        chosen, _ = desktop_control._choose_search_field(candidates)

        self.assertEqual(chosen["pontuacao_busca"], 45)

    def test_no_input_field_is_refused(self):
        chosen, reason = desktop_control._choose_search_field(
            [_search_candidate("Button", "Search", 56)]
        )

        self.assertIsNone(chosen)
        self.assertIn("não há campo de entrada", reason)


class SearchToolTests(unittest.TestCase):
    def _ctx(self):
        return object()

    def test_empty_text_is_rejected(self):
        result = desktop_control.pesquisar(self._ctx(), {"texto": "   "})

        self.assertFalse(result.ok)

    def test_refuses_and_lists_candidates_when_unsure(self):
        snapshot = {
            "disponivel": True,
            "resumo_pesquisa": [
                _search_candidate("Edit", "Comentário", 40),
                _search_candidate("Button", "Search", 56),
            ],
        }
        with patch.object(desktop_control, "_active_window_snapshot", return_value=snapshot):
            result = desktop_control.pesquisar(self._ctx(), {"texto": "counter strike"})

        self.assertFalse(result.ok)
        self.assertIn("não identifiquei", result.summary)
        self.assertIn("Comentário", result.summary)

    def test_reports_missing_accessibility_without_guessing(self):
        snapshot = {"disponivel": False, "motivo": "UI Automation desligada"}
        with patch.object(desktop_control, "_active_window_snapshot", return_value=snapshot):
            result = desktop_control.pesquisar(self._ctx(), {"texto": "counter strike"})

        self.assertFalse(result.ok)
        self.assertIn("UI Automation desligada", result.summary)

    def test_mentions_minimized_window_when_no_control_is_read(self):
        snapshot = {"disponivel": True, "resumo_pesquisa": [], "elementos": [], "total_elementos_lidos": 0}
        with patch.object(desktop_control, "_active_window_snapshot", return_value=snapshot):
            result = desktop_control.pesquisar(self._ctx(), {"texto": "counter strike"})

        self.assertFalse(result.ok)
        self.assertIn("minimizada", result.summary)

    def test_does_not_submit_when_typing_was_not_confirmed(self):
        calls: list[str] = []

        class _FakePyAutoGui:
            def press(self, key):
                calls.append(f"press:{key}")

        snapshot = {
            "disponivel": True,
            "resumo_pesquisa": [_search_candidate("ComboBox", "Search the store", 97, magnifier=True)],
        }
        clicked = {"controle": "Search the store", "tipo": "ComboBox", "id_automacao": "",
                   "metodo": "uia_invoke", "centro_x": 1, "centro_y": 1}
        with (
            patch.object(desktop_control, "_active_window_snapshot", return_value=snapshot),
            patch.object(desktop_control, "_active_uia_window", return_value="janela"),
            patch.object(desktop_control, "_control_for_candidate", return_value="controle"),
            patch.object(desktop_control, "_click_control", return_value=clicked),
            patch.object(desktop_control, "_send_text"),
            patch.object(desktop_control, "_focused_field_state",
                         return_value={"nome": "Search the store", "tipo": "ComboBox", "valor": ""}),
            patch.object(desktop_control, "_pyautogui", return_value=_FakePyAutoGui()),
        ):
            result = desktop_control.pesquisar(self._ctx(), {"texto": "counter strike"})

        self.assertFalse(result.ok)
        self.assertEqual(calls, [])
        self.assertIn("NÃO enviada", result.summary)

    def test_focus_on_document_is_reported_as_failure_without_submitting(self):
        calls: list[str] = []

        class _FakePyAutoGui:
            def press(self, key):
                calls.append(f"press:{key}")

        snapshot = {
            "disponivel": True,
            "resumo_pesquisa": [_search_candidate("ComboBox", "Search the store", 97, magnifier=True)],
        }
        clicked = {"controle": "Search the store", "tipo": "ComboBox", "id_automacao": "",
                   "metodo": "click_input", "centro_x": 1, "centro_y": 1}
        with (
            patch.object(desktop_control, "_active_window_snapshot", return_value=snapshot),
            patch.object(desktop_control, "_active_uia_window", return_value="janela"),
            patch.object(desktop_control, "_control_for_candidate", return_value="controle"),
            patch.object(desktop_control, "_click_control", return_value=clicked),
            patch.object(desktop_control, "_send_text"),
            patch.object(desktop_control, "_focused_field_state",
                         return_value={"nome": "Welcome to Steam", "tipo": "Document", "valor": "https://store"}),
            patch.object(desktop_control, "_pyautogui", return_value=_FakePyAutoGui()),
        ):
            result = desktop_control.pesquisar(self._ctx(), {"texto": "counter strike"})

        self.assertFalse(result.ok)
        self.assertEqual(calls, [])
        self.assertIn("não no campo de pesquisa", result.summary)

    def test_empty_key_disables_submission(self):
        calls: list[str] = []

        class _FakePyAutoGui:
            def press(self, key):
                calls.append(f"press:{key}")

        snapshot = {
            "disponivel": True,
            "resumo_pesquisa": [_search_candidate("ComboBox", "Search the store", 97, magnifier=True)],
        }
        clicked = {"controle": "Search the store", "tipo": "ComboBox", "id_automacao": "",
                   "metodo": "click_input", "centro_x": 1, "centro_y": 1}
        with (
            patch.object(desktop_control, "_active_window_snapshot", return_value=snapshot),
            patch.object(desktop_control, "_active_uia_window", return_value="janela"),
            patch.object(desktop_control, "_control_for_candidate", return_value="controle"),
            patch.object(desktop_control, "_click_control", return_value=clicked),
            patch.object(desktop_control, "_send_text"),
            patch.object(desktop_control, "_focused_field_state",
                         return_value={"nome": "Search the store", "tipo": "ComboBox", "valor": "counter strike"}),
            patch.object(desktop_control, "_pyautogui", return_value=_FakePyAutoGui()),
        ):
            result = desktop_control.pesquisar(self._ctx(), {"texto": "counter strike", "tecla": ""})

        self.assertTrue(result.ok)
        self.assertEqual(calls, [])
        self.assertIsNone(result.data["tecla_enviada"])

    def test_types_confirms_and_submits_in_one_call(self):
        calls: list[str] = []

        class _FakePyAutoGui:
            def press(self, key):
                calls.append(f"press:{key}")

        snapshot = {
            "disponivel": True,
            "resumo_pesquisa": [_search_candidate("ComboBox", "Search the store", 97, magnifier=True)],
        }
        typed = {
            "controle": "Search the store",
            "id_automacao": "",
            "tipo": "ComboBox",
            "metodo": "click_input",
            "centro_x": 100,
            "centro_y": 100,
        }
        state = {"nome": "Search the store", "tipo": "ComboBox", "valor": "counter strike"}
        with (
            patch.object(desktop_control, "_active_window_snapshot", return_value=snapshot),
            patch.object(desktop_control, "_active_uia_window", return_value="janela"),
            patch.object(desktop_control, "_control_for_candidate", return_value="controle"),
            patch.object(desktop_control, "_click_control", return_value=typed) as click,
            patch.object(desktop_control, "_send_text", side_effect=lambda *a, **k: calls.append("send")),
            patch.object(desktop_control, "_focused_field_state", return_value=state),
            patch.object(desktop_control, "_pyautogui", return_value=_FakePyAutoGui()),
        ):
            result = desktop_control.pesquisar(self._ctx(), {"texto": "counter strike"})

        self.assertTrue(result.ok)
        self.assertTrue(result.data["confirmado"])
        self.assertEqual(calls, ["send", "press:enter"])
        self.assertTrue(click.called)


if __name__ == "__main__":
    unittest.main()

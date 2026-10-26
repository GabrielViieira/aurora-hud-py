"""Testes do classificador genérico de controles de pesquisa."""
import importlib.util
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "search_perception.py"
_SPEC = importlib.util.spec_from_file_location("search_perception_under_test", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
rank_search_candidates = _MODULE.rank_search_candidates


def element(
    tipo: str,
    nome: str,
    esquerda: int,
    topo: int,
    direita: int,
    base: int,
    **extra,
):
    return {
        "tipo": tipo,
        "nome": nome,
        "esquerda": esquerda,
        "topo": topo,
        "direita": direita,
        "base": base,
        "centro_x": (esquerda + direita) // 2,
        "centro_y": (topo + base) // 2,
        **extra,
    }


class SearchPerceptionTests(unittest.TestCase):
    def test_search_label_outranks_navigation(self):
        search = element("Edit", "Search the Store", 300, 50, 850, 84)
        browse = element("MenuItem", "Browse", 40, 50, 125, 84)
        result = rank_search_candidates([browse, search], window_width=1000)

        self.assertEqual(result[0]["nome"], "Search the Store")
        self.assertGreater(result[0]["pontuacao_busca"], result[1]["pontuacao_busca"])
        self.assertEqual(result[0]["confianca_busca"], "alta")

    def test_static_search_label_does_not_outrank_input_field(self):
        field = element("Edit", "[Campo de texto sem nome]", 150, 70, 700, 106)
        label = element("Text", "Search", 80, 72, 130, 100)
        result = rank_search_candidates([label, field], window_width=1000)

        self.assertEqual(result[0]["tipo"], "Edit")
        self.assertEqual(result[0]["papel_sugerido"], "possível campo de busca")

    def test_wide_unnamed_edit_is_a_candidate(self):
        field = element("Edit", "[Campo de texto sem nome]", 150, 70, 700, 106)
        result = rank_search_candidates([field], window_width=1000)

        self.assertGreaterEqual(result[0]["pontuacao_busca"], 30)
        self.assertEqual(result[0]["papel_sugerido"], "possível campo de busca")
        self.assertIn("formato horizontal comprido", result[0]["evidencias_busca"])

    def test_unnamed_button_next_to_edit_is_possible_magnifier(self):
        field = element("Edit", "[Campo de texto sem nome]", 150, 70, 700, 106)
        button = element("Button", "[Botão sem nome]", 701, 70, 737, 106)
        result = rank_search_candidates([button, field], window_width=1000)
        by_name = {item["nome"]: item for item in result}

        self.assertEqual(by_name[field["nome"]]["papel_sugerido"], "possível campo de busca")
        self.assertEqual(by_name[button["nome"]]["papel_sugerido"], "possível botão de busca")
        self.assertTrue(any("pode ser uma lupa" in clue for clue in by_name[button["nome"]]["evidencias_busca"]))

    def test_unnamed_image_next_to_edit_is_possible_magnifier(self):
        field = element("Edit", "[Campo de texto sem nome]", 150, 70, 700, 106)
        icon = element("Image", "[Imagem sem nome]", 701, 70, 737, 106)
        result = rank_search_candidates([field, icon], window_width=1000)
        by_name = {item["nome"]: item for item in result}

        self.assertEqual(by_name[icon["nome"]]["papel_sugerido"], "possível ícone de busca")
        self.assertTrue(any("pode ser uma lupa" in clue for clue in by_name[icon["nome"]]["evidencias_busca"]))

    def test_accented_portuguese_search_label_is_recognized(self):
        field = element("Edit", "Pesquisa", 200, 40, 650, 74)
        result = rank_search_candidates([field], window_width=1000)
        self.assertIn("rótulo contém termo de pesquisa", result[0]["evidencias_busca"])
        self.assertGreaterEqual(result[0]["pontuacao_busca"], 70)

    def test_steam_combo_box_named_search_the_store_is_high_confidence(self):
        # Geometria real da barra de busca do Steam (janela 1920x1040):
        # o Chromium expõe o campo como ComboBox, não como Edit.
        field = element("ComboBox", "Search the store", 954, 98, 1396, 132)
        result = rank_search_candidates([field], window_width=1920)

        self.assertGreaterEqual(result[0]["pontuacao_busca"], 70)
        self.assertEqual(result[0]["confianca_busca"], "alta")
        self.assertEqual(result[0]["papel_sugerido"], "possível campo de busca")

    def test_wide_unnamed_combo_box_is_a_candidate(self):
        field = element("ComboBox", "", 100, 20, 600, 54)
        result = rank_search_candidates([field], window_width=1000)

        self.assertGreaterEqual(result[0]["pontuacao_busca"], 30)
        self.assertEqual(result[0]["papel_sugerido"], "possível campo de busca")

    def test_categories_is_not_a_search_candidate(self):
        categories = element("TabItem", "Categories", 20, 40, 130, 72)
        result = rank_search_candidates([categories], window_width=1000)
        self.assertLess(result[0]["pontuacao_busca"], 30)
        self.assertEqual(result[0]["papel_sugerido"], "")


if __name__ == "__main__":
    unittest.main()

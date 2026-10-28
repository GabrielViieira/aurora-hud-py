"""Testes das primitivas de segmentação e cancelamento de fala incremental."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

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

from aurora.core.streaming import SentenceSegmenter, StreamingSpeechSession


class SentenceSegmenterTests(unittest.TestCase):
    def test_releases_sentence_after_punctuation_and_space_arrive(self):
        segmenter = SentenceSegmenter()
        self.assertEqual(segmenter.feed("A primeira frase."), [])
        self.assertEqual(segmenter.feed(" A segunda continua"), ["A primeira frase."])
        self.assertEqual(segmenter.flush(), ["A segunda continua"])

    def test_splits_newline_delimited_segments(self):
        segmenter = SentenceSegmenter()
        self.assertEqual(segmenter.feed("Primeira linha\nSegunda linha\n"), [
            "Primeira linha", "Segunda linha"
        ])

    def test_long_unpunctuated_text_is_released_without_waiting_for_end(self):
        segmenter = SentenceSegmenter(max_chars=40)
        text = "Esta é uma resposta longa sem pontuação " * 3
        parts = segmenter.feed(text)
        self.assertTrue(parts)
        self.assertEqual("".join(part + " " for part in parts).strip()[:20], text[:20])
        self.assertTrue(all(len(part) <= 72 for part in parts))

    def test_flush_preserves_final_unterminated_fragment(self):
        segmenter = SentenceSegmenter()
        segmenter.feed("A resposta final")
        self.assertEqual(segmenter.flush(), ["A resposta final"])
        self.assertEqual(segmenter.buffered_text, "")

    def test_reset_discards_buffered_partial_text(self):
        segmenter = SentenceSegmenter()
        segmenter.feed("não falar isso")
        segmenter.reset()
        self.assertEqual(segmenter.flush(), [])


class StreamingSpeechSessionTests(unittest.TestCase):
    def test_finish_closes_input_but_preserves_final_segments(self):
        session = StreamingSpeechSession(token=10)
        self.assertEqual(session.feed("Primeira frase. "), ["Primeira frase."])
        session.segments.put("Primeira frase.")
        tail = session.finish("completed")
        self.assertEqual(tail, [])
        self.assertEqual(session.final_state, "completed")
        self.assertEqual(session.feed("não deve entrar"), [])

    def test_cancel_marks_session_and_wakes_consumer(self):
        session = StreamingSpeechSession(token=20)
        session.feed("texto parcial")
        session.cancel()
        self.assertTrue(session.cancelled)
        self.assertTrue(session.stop_event.is_set())
        self.assertTrue(session.closed)
        self.assertIsNone(session.segments.get_nowait())
        self.assertEqual(session.feed("texto tardio"), [])

    def test_finish_flushes_remaining_text_once(self):
        session = StreamingSpeechSession(token=30)
        session.feed("Resposta sem ponto")
        self.assertEqual(session.finish("failed"), ["Resposta sem ponto"])
        self.assertEqual(session.finish("completed"), [])
        self.assertEqual(session.final_state, "failed")


if __name__ == "__main__":
    unittest.main()

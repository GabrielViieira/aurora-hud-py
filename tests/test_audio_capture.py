"""Testes offline da conversão de áudio do microfone para PCM do Vosk."""
from __future__ import annotations

import struct
import unittest

from aurora.ui.audio_capture import to_vosk_pcm16


class AudioCaptureTests(unittest.TestCase):
    def test_float_audio_becomes_little_endian_pcm16(self):
        pcm = to_vosk_pcm16([0.0, 0.5, -0.5, 1.0], source_rate=16000)
        samples = struct.unpack("<hhhh", pcm)
        self.assertEqual(samples[0], 0)
        self.assertTrue(16000 <= samples[1] <= 16400)
        self.assertTrue(-16400 <= samples[2] <= -16000)
        self.assertEqual(samples[3], 32767)

    def test_stereo_audio_is_downmixed_to_mono(self):
        pcm = to_vosk_pcm16([[0.5, -0.5], [0.25, 0.25]], source_rate=16000)
        samples = struct.unpack("<hh", pcm)
        self.assertEqual(samples[0], 0)
        self.assertTrue(8000 <= samples[1] <= 8300)

    def test_native_rate_is_resampled_to_vosk_rate(self):
        pcm = to_vosk_pcm16([0.25] * 480, source_rate=48000, target_rate=16000)
        self.assertEqual(len(pcm), 160 * 2)
        sample = struct.unpack("<h", pcm[:2])[0]
        self.assertTrue(8000 <= sample <= 8300)

    def test_gain_is_applied_and_clipped(self):
        pcm = to_vosk_pcm16([0.1, 0.8], source_rate=16000, gain_percent=200)
        samples = struct.unpack("<hh", pcm)
        self.assertTrue(6400 <= samples[0] <= 6800)
        self.assertEqual(samples[1], 32767)

    def test_empty_input_returns_empty_pcm(self):
        self.assertEqual(to_vosk_pcm16([], source_rate=48000), b"")

    def test_invalid_sample_rate_is_rejected(self):
        with self.assertRaises(ValueError):
            to_vosk_pcm16([0.0], source_rate=0)


if __name__ == "__main__":
    unittest.main()

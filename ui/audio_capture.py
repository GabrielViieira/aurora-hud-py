"""Conversão de áudio capturado para PCM mono de 16 kHz usado por Vosk e Whisper."""
from __future__ import annotations

import array
import math
import sys
from typing import Any


def to_vosk_pcm16(
    audio: Any,
    *,
    source_rate: int,
    target_rate: int = 16000,
    gain_percent: float = 100.0,
    numpy_module: Any = None,
) -> bytes:
    """Converte blocos float de 1 ou vários canais em PCM16 mono, com reamostragem."""
    try:
        source_rate = int(source_rate)
        target_rate = int(target_rate)
        gain = max(0.0, min(4.0, float(gain_percent) / 100.0))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Taxas de amostragem e ganho precisam ser numéricos.")
    if source_rate < 1 or target_rate < 1:
        raise ValueError("Taxas de amostragem precisam ser positivas.")

    if numpy_module is not None:
        np = numpy_module
        samples = np.asarray(audio, dtype=np.float32)
        if samples.size == 0:
            return b""
        if samples.ndim > 1:
            samples = samples.reshape(samples.shape[0], -1).mean(axis=1, dtype=np.float32)
        samples = samples.reshape(-1)
        if samples.size == 0:
            return b""
        if source_rate != target_rate:
            output_count = max(1, int(round(samples.size * target_rate / source_rate)))
            positions = np.arange(output_count, dtype=np.float64) * (source_rate / target_rate)
            left = np.minimum(positions.astype(np.int64), samples.size - 1)
            right = np.minimum(left + 1, samples.size - 1)
            fraction = (positions - left).astype(np.float32)
            samples = samples[left] * (1.0 - fraction) + samples[right] * fraction
        samples = np.nan_to_num(samples, nan=0.0, posinf=0.0, neginf=0.0)
        samples = np.clip(samples * gain, -1.0, 1.0)
        return (samples * 32767.0).astype("<i2").tobytes()

    if hasattr(audio, "tolist"):
        audio = audio.tolist()
    rows = list(audio or [])
    if not rows:
        return b""
    if isinstance(rows[0], (list, tuple)):
        mono = []
        for row in rows:
            channels = list(row) if isinstance(row, (list, tuple)) else [row]
            mono.append(sum(float(v) for v in channels) / max(1, len(channels)))
    else:
        mono = [float(value) for value in rows]
    if source_rate != target_rate:
        output_count = max(1, int(round(len(mono) * target_rate / source_rate)))
        resampled = []
        for index in range(output_count):
            position = index * source_rate / target_rate
            left = min(int(position), len(mono) - 1)
            right = min(left + 1, len(mono) - 1)
            fraction = position - left
            resampled.append(mono[left] * (1.0 - fraction) + mono[right] * fraction)
        mono = resampled

    pcm = array.array("h")
    for sample in mono:
        value = float(sample)
        if not math.isfinite(value):
            value = 0.0
        value = max(-1.0, min(1.0, value * gain))
        pcm.append(max(-32768, min(32767, int(round(value * 32767)))))
    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm.tobytes()

"""Voz: TTS modular e canal não bloqueante. STT entra na Fase 4."""

from aurora.voice.output import SpeechChannel, text_for_speech
from aurora.voice.tts_engine import (
    NullTts,
    SapiTts,
    SapiVoice,
    TtsEngine,
    TtsUnavailable,
    VoiceVoxTts,
    create_tts,
    describe_voices,
    list_sapi_voices,
)

__all__ = [
    "NullTts",
    "SapiTts",
    "SapiVoice",
    "SpeechChannel",
    "TtsEngine",
    "TtsUnavailable",
    "VoiceVoxTts",
    "create_tts",
    "describe_voices",
    "list_sapi_voices",
    "text_for_speech",
]

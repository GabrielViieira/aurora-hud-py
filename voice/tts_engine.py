"""Síntese de voz japonesa: SAPI do Windows (padrão) e VOICEVOX (opcional).

Ordem de preferência:
1. voz japonesa instalada no Windows (ex.: Microsoft Haruka);
2. VOICEVOX, se o usuário habilitar e o motor estiver rodando;
3. silêncio — o modo texto continua funcionando.

Este módulo não reconhece fala: STT é independente e entra na Fase 4.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aurora.config.settings import VoiceSettings
from aurora.observability import get_logger

logger = get_logger(__name__)

VOICE_DIR = Path(__file__).resolve().parent


class TtsUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SapiVoice:
    name: str
    culture: str
    gender: str = ""

    @property
    def is_japanese(self) -> bool:
        return self.culture.lower().startswith("ja")


def powershell_path() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def _run_powershell(args: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    powershell = powershell_path()
    if not powershell:
        raise TtsUnavailable("PowerShell não encontrado; TTS SAPI indisponível")
    command = [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        *args,
    ]
    return subprocess.run(
        command,
        capture_output=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def list_sapi_voices() -> list[SapiVoice]:
    try:
        completed = _run_powershell(["-File", str(VOICE_DIR / "sapi_list.ps1")])
    except (OSError, subprocess.TimeoutExpired, TtsUnavailable) as exc:
        logger.warning("não foi possível listar vozes SAPI: %s", exc)
        return []
    if completed.returncode != 0:
        logger.warning(
            "sapi_list.ps1 falhou: %s",
            completed.stderr.decode("utf-8", errors="replace")[:300],
        )
        return []
    text = completed.stdout.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("saída inválida ao listar vozes: %r", text[:200])
        return []
    if isinstance(payload, dict):
        payload = [payload]
    return [
        SapiVoice(
            name=str(item.get("Name", "")),
            culture=str(item.get("Culture", "")),
            gender=str(item.get("Gender", "")),
        )
        for item in payload
        if isinstance(item, dict) and item.get("Name")
    ]


class TtsEngine(Protocol):
    name: str

    def speak(self, text: str) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


class NullTts:
    name = "none"

    def __init__(self, reason: str = "voz desabilitada") -> None:
        self.reason = reason

    def speak(self, text: str) -> None:
        logger.debug("TTS nulo: %s", self.reason)

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None


class SapiTts:
    name = "sapi"

    def __init__(
        self,
        voice_name: str,
        *,
        rate: int = 0,
        volume: int = 100,
        work_dir: Path | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self.voice_name = voice_name
        self.rate = max(-10, min(10, rate))
        self.volume = max(0, min(100, volume))
        self.timeout_s = timeout_s
        self.work_dir = Path(work_dir or Path(tempfile.gettempdir()) / "aurora-voice")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.text_file = self.work_dir / "sapi_speech.txt"
        self._process: subprocess.Popen | None = None

    def speak(self, text: str) -> None:
        if not text.strip():
            return
        self.text_file.write_text(text, encoding="utf-8")
        powershell = powershell_path()
        if not powershell:
            raise TtsUnavailable("PowerShell não encontrado")
        command = [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(VOICE_DIR / "sapi_speak.ps1"),
            "-TextFile",
            str(self.text_file),
            "-Rate",
            str(self.rate),
            "-Volume",
            str(self.volume),
        ]
        if self.voice_name:
            command.extend(["-VoiceName", self.voice_name])
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            _, stderr = self._process.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            self.stop()
            raise TtsUnavailable("a síntese excedeu o tempo limite")
        if self._process.returncode != 0:
            detail = (stderr or b"").decode("utf-8", errors="replace")[:300]
            raise TtsUnavailable(f"falha na síntese SAPI: {detail}")
        self._process = None

    def stop(self) -> None:
        if self._process and self._process.poll() is None:
            try:
                self._process.kill()
            except OSError:
                pass
        self._process = None

    def close(self) -> None:
        self.stop()


class VoiceVoxTts:
    name = "voicevox"

    def __init__(
        self,
        base_url: str,
        speaker: int = 3,
        *,
        work_dir: Path | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.speaker = speaker
        self.timeout_s = timeout_s
        self.work_dir = Path(work_dir or Path(tempfile.gettempdir()) / "aurora-voice")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.wav_file = self.work_dir / "voicevox.wav"
        self._process: subprocess.Popen | None = None

    def is_available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/version", timeout=2) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def speakers(self) -> list[dict]:
        try:
            with urllib.request.urlopen(f"{self.base_url}/speakers", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, list) else []
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
            return []

    def _post(self, path: str, body: bytes | None = None) -> bytes:
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            return response.read()

    def speak(self, text: str) -> None:
        if not text.strip():
            return
        try:
            query_bytes = self._post(
                f"/audio_query?text={urllib.parse.quote(text)}&speaker={self.speaker}"
            )
            audio = self._post(f"/synthesis?speaker={self.speaker}", body=query_bytes)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise TtsUnavailable(f"VOICEVOX indisponível: {exc}") from exc
        if not audio:
            raise TtsUnavailable("VOICEVOX devolveu áudio vazio")
        self.wav_file.write_bytes(audio)
        self._play(self.wav_file)

    def _play(self, path: Path) -> None:
        if os.name != "nt":
            raise TtsUnavailable("reprodução VOICEVOX implementada apenas no Windows")
        import winsound

        winsound.PlaySound(str(path), winsound.SND_FILENAME)

    def stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.kill()
        self._process = None
        if os.name == "nt":
            try:
                import winsound

                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass

    def close(self) -> None:
        self.stop()


def create_tts(settings: VoiceSettings, work_dir: Path | None = None) -> TtsEngine:
    if not settings.enabled or settings.backend == "none":
        return NullTts("voz desabilitada na configuração")

    voices = list_sapi_voices()
    japanese = [voice for voice in voices if voice.is_japanese]
    selected = next((voice for voice in japanese if voice.name == settings.voice_name), None)
    if settings.voice_name and selected is None:
        selected = next((voice for voice in voices if voice.name == settings.voice_name), None)

    if settings.backend == "voicevox":
        client = VoiceVoxTts(
            settings.voicevox_url, settings.voicevox_speaker, work_dir=work_dir
        )
        if client.is_available():
            return client
        logger.warning("VOICEVOX não respondeu; tentando SAPI")

    if not selected and japanese:
        selected = japanese[0]

    if selected:
        return SapiTts(
            selected.name,
            rate=settings.rate,
            volume=settings.volume,
            work_dir=work_dir,
        )

    return NullTts("nenhuma voz japonesa instalada no Windows")


def describe_voices() -> str:
    voices = list_sapi_voices()
    if not voices:
        return "nenhuma voz SAPI encontrada"
    return "; ".join(
        f"{voice.name} [{voice.culture}]" + (" (ja)" if voice.is_japanese else "")
        for voice in voices
    )

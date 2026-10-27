# Launcher da interface holográfica nativa da Aurora (PySide6 + QPainter).

# Interface HUD oficial da Aurora. Integra com o runtime da Aurora.


from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Optional, Callable

# Adiciona caminho da app para importar aurora
APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

# Tamanho base da janela do HUD (frameless). Era 500x500; a composição é
# desenhada por QPainter em coordenadas normalizadas, então escala sozinha.
HUD_BASE_SIZE = (300, 300)

try:
    from aurora.config.settings import Settings
    from aurora.runtime import build_runtime
    AURORA_AVAILABLE = True
except Exception as exc:
    print(f"[Aurora UI] Aviso: não foi possível importar runtime da Aurora: {exc}")
    Settings = None
    build_runtime = None
    AURORA_AVAILABLE = False

try:
    import pyttsx3
    PYTTSX3_AVAILABLE = True
except Exception:
    pyttsx3 = None
    PYTTSX3_AVAILABLE = False

try:
    import httpx
    HTTPX_AVAILABLE = True
except Exception:
    httpx = None
    HTTPX_AVAILABLE = False

try:
    from vvclient import Client as VoicevoxClient
    import asyncio
    VOICEVOX_AVAILABLE = True
except Exception:
    VoicevoxClient = None
    asyncio = None
    VOICEVOX_AVAILABLE = False

from animation import D20Animation
from efeitos import HologramEffects
from PySide6.QtCore import (
    QPoint,
    QRectF,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QMouseEvent, QPainter, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QTextBrowser,
    QWidget,
)

_JA_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]")
# Para validar tradução, exija hiragana/katakana; kanji isolado também pode ser chinês.
_JA_KANA_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF]")
FREELLM_URL = os.environ.get("AURORA_FREELLM_URL", "http://localhost:3001")

def _classify_hud_control_command(text: str) -> str:
    """Reconhece comandos locais explícitos de cancelar tarefa ou encerrar a Aurora."""
    value = unicodedata.normalize("NFKD", str(text or "").casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()
    value = re.sub(r"^(?:(?:ei|hey) )?aurora(?:\s+|$)", "", value).strip()

    cancel_patterns = (
        r"(?:cancele|cancelar|cancel|pare|parar|interrompa|interromper|stop)(?: (?:a|o|the))?(?: (?:tarefa|instrucao|comando|execucao|processo|isso|task|instruction|command))?(?: (?:atual|current))?(?: agora| now)?",
        r"(?:cancel|stop) (?:the )?(?:current )?(?:task|instruction|command)",
        r"(?:pare|parar|interrompa|interromper|cancele|cancelar|stop|cancel)(?: (?:de|a|o|the))? (?:falar|fala|voz|audio|speaking|talking|voice|speech)(?: (?:agora|now))?",
    )
    if any(re.fullmatch(pattern, value) for pattern in cancel_patterns):
        return "cancel"

    close_patterns = (
        r"(?:feche|fecha|fechar|encerre|encerra|encerrar|desligue|desliga|desligar|close|quit|exit) (?:a |o )?aurora",
        r"(?:saia|sair|exit|quit) (?:da|de) aurora",
        r"(?:feche|fecha|fechar|encerre|encerrar|close|quit|exit) (?:o |a )?(?:app|aplicativo) aurora",
        r"(?:close|quit|exit) aurora",
    )
    if any(re.fullmatch(pattern, value) for pattern in close_patterns):
        return "exit"
    return ""


def _tts_gateway_config() -> tuple[str, dict[str, str]]:
    # Resolve endpoint e autenticação usando a mesma configuração do runtime.
    from aurora.voice.translation_client import translation_request_config
    return translation_request_config()

APPLIO_DIR = Path(r"C:\Users\Administrator\Downloads\Applio-main")
RAPHAEL_PTH = APPLIO_DIR / "logs" / "Raphael_Sensei.pth"
if not RAPHAEL_PTH.exists():
    RAPHAEL_PTH = APPLIO_DIR / "assets" / "weights" / "Raphael_Sensei" / "Raphael_Sensei.pth"
RAPHAEL_INDEX = ""
# Procura index caso exista
for cand in [
    APPLIO_DIR / "logs" / "Raphael_Sensei" / "added_IVF2165_Flat_nprobe_1_Raphael_Sensei_v2.index",
    APPLIO_DIR / "logs" / "Raphael_Sensei" / "added.index",
    APPLIO_DIR / "logs" / "Raphael_Sensei" / "trained" / "added.index",
    APPLIO_DIR / "assets" / "weights" / "Raphael_Sensei" / "added_IVF2165_Flat_nprobe_1_Raphael_Sensei_v2.index",
]:
    if cand.exists():
        RAPHAEL_INDEX = str(cand)
        break

# GPT-SoVITS treinado localmente: a API mantém GPT/SoVITS carregados entre falas.
GPT_SOVITS_DIR = Path(os.environ.get(
    "AURORA_GPT_SOVITS_DIR",
    r"C:\Users\Administrator\Downloads\GPT-SoVITS-v2pro-20250604",
)).expanduser()
GPT_SOVITS_API_PORT = int(os.environ.get("AURORA_GPT_SOVITS_API_PORT", "9880"))
GPT_SOVITS_API_URL = os.environ.get(
    "AURORA_GPT_SOVITS_API_URL", f"http://127.0.0.1:{GPT_SOVITS_API_PORT}"
).rstrip("/")
GPT_SOVITS_PYTHON = Path(os.environ.get(
    "AURORA_GPT_SOVITS_PYTHON", str(GPT_SOVITS_DIR / "runtime" / "python.exe")
)).expanduser()
GPT_SOVITS_GPT_MODEL = Path(os.environ.get(
    "AURORA_GPT_SOVITS_GPT_MODEL",
    str(GPT_SOVITS_DIR / "GPT_weights_v2Pro" / "xxx-e15.ckpt"),
)).expanduser()
GPT_SOVITS_SOVITS_MODEL = Path(os.environ.get(
    "AURORA_GPT_SOVITS_SOVITS_MODEL",
    str(GPT_SOVITS_DIR / "SoVITS_weights_v2Pro" / "xxx_e8_s816.pth"),
)).expanduser()
GPT_SOVITS_REFERENCE = Path(os.environ.get(
    "AURORA_GPT_SOVITS_REFERENCE",
    str(GPT_SOVITS_DIR / "logs" / "xxx" / "5-wav32k" / "0001_raphael.wav"),
)).expanduser()
GPT_SOVITS_PROMPT_TEXT = os.environ.get(
    "AURORA_GPT_SOVITS_PROMPT_TEXT", "こんにちは。ラファエルです。"
)
GPT_SOVITS_DEVICE = os.environ.get("AURORA_GPT_SOVITS_DEVICE", "cuda").strip() or "cuda"
GPT_SOVITS_ENABLED = os.environ.get("AURORA_GPT_SOVITS_ENABLED", "1").strip().lower() not in {
    "0", "false", "no", "off", "disabled"
}
GPT_SOVITS_API_LOG = Path(os.environ.get(
    "LOCALAPPDATA", str(Path.home() / "AppData" / "Local")
)) / "Aurora" / "logs" / "gpt_sovits_api.log"

# Motor de inferência GPT-SoVITS direto: sem servidor HTTP local.
GPT_SOVITS_DIRECT_ENABLED = os.environ.get(
    "AURORA_GPT_SOVITS_DIRECT_ENABLED", "1"
).strip().lower() not in {"0", "false", "no", "off", "disabled"}
GPT_SOVITS_API_FALLBACK_ENABLED = os.environ.get(
    "AURORA_GPT_SOVITS_API_FALLBACK", "0"
).strip().lower() in {"1", "true", "yes", "on", "enabled"}
GPT_SOVITS_DIRECT_WORKER = Path(__file__).resolve().with_name("gpt_sovits_worker.py")
GPT_SOVITS_INFERENCE = GPT_SOVITS_DIR / "api.py"
GPT_SOVITS_DIRECT_LOG = Path(os.environ.get(
    "LOCALAPPDATA", str(Path.home() / "AppData" / "Local")
)) / "Aurora" / "logs" / "gpt_sovits_direct.log"
GPT_SOVITS_DEBUG_WAV = Path(os.environ.get(
    "LOCALAPPDATA", str(Path.home() / "AppData" / "Local")
)) / "Aurora" / "debug" / "raphael_latest.wav"

VOICEVOX_URL = os.environ.get("AURORA_VOICEVOX_URL", "http://127.0.0.1:50021")
VOICEVOX_SPEAKER = int(os.environ.get("AURORA_VOICEVOX_SPEAKER", "3"))

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SVG = BASE_DIR / "aurora_full.svg"
DEFAULT_VOSK_MODEL = BASE_DIR / "models" / "vosk-model-small-pt-0.3"

# Usa a voz japonesa Nanami via SAPI/pyttsx3 por padrão, sem conversão RVC.
# Outros motores permanecem disponíveis por AURORA_TTS_BACKEND.
AURORA_TTS_BACKEND = os.environ.get("AURORA_TTS_BACKEND", "nanami").strip().casefold()
if AURORA_TTS_BACKEND not in {"piper", "gpt_sovits", "nanami", "nanami_rvc"}:
    print(f"[Aurora TTS] Backend desconhecido {AURORA_TTS_BACKEND!r}; usando Nanami sem RVC.")
    AURORA_TTS_BACKEND = "nanami"

PIPER_MODEL_PATH = Path(os.environ.get(
    "AURORA_PIPER_MODEL",
    str(BASE_DIR.parent / "voice" / "voice_JA" / "sample-voice-v2.onnx"),
)).expanduser()
PIPER_CONFIG_PATH = Path(os.environ.get(
    "AURORA_PIPER_CONFIG",
    str(BASE_DIR.parent / "voice" / "voice_JA" / "sample-voice-v2.onnx.json"),
)).expanduser()

# Atenua a saída do Piper para 30% da amplitude original (aprox. -10,5 dB).
PIPER_OUTPUT_GAIN = 0.3


class SvgWidget(QWidget):
    # Widget transparente com animações nativas de QPainter.

    drag_started = Signal(QPoint)
    drag_moved = Signal(QPoint)
    hologram_double_clicked = Signal()

    def __init__(self, svg_path: Path, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setStyleSheet("background: transparent;")

        # A composição visual é desenhada inteiramente por QPainter.
        # Não instanciamos QSvgRenderer aqui: o SVG de origem usa filtros
        # glow grandes que podem provocar o aviso "requested buffer size is too big".
        # O caminho continua guardado para diagnóstico, mas o desenho é nativo.
        self.svg_path = svg_path
        if not svg_path.is_file():
            raise FileNotFoundError(f"Arquivo SVG não encontrado: {svg_path}")

        # Corrige o erro original: os efeitos e o d20 precisam existir antes
        # do primeiro paintEvent, que pode ocorrer assim que o widget é exibido.
        self.effects = HologramEffects()
        self.d20 = D20Animation()
        self._last_frame = time.perf_counter()

    def advance_animation(self) -> None:
        # Avança as animações usando o tempo real entre quadros.
        now = time.perf_counter()
        dt = min(max(now - self._last_frame, 0.0), 0.1)
        self._last_frame = now
        self.effects.update(dt)
        self.d20.update(dt)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - método Qt
        del event
        painter = QPainter(self)
        if not painter.isActive():
            return
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            rect = QRectF(self.rect())

            # Durante listening/thinking/processing/speaking, a composição inteira
            # gira ao redor do centro. Os anéis e o icosaedro mantêm seus próprios
            # movimentos, criando uma rotação em camadas. O widget segue transparente.
            painter.save()
            painter.translate(rect.center())
            painter.rotate(self.effects.global_rotation_angle)
            painter.translate(-rect.center())
            try:
                # Camadas transparentes: feixes, partículas, borda japonesa e núcleo.
                self.effects.paint_background(painter, rect)
                self.effects.paint_particles(painter, rect)
                self.effects.paint_core(painter, rect)
                # O icosaedro fica por último, à frente dos halos e perfeitamente legível.
                self.d20.paint(painter, rect)
            finally:
                painter.restore()
        finally:
            # Garante que QPainter seja encerrado mesmo se uma camada falhar.
            painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_started.emit(event.globalPosition().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.drag_moved.emit(event.globalPosition().toPoint())
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.hologram_double_clicked.emit()
        super().mouseDoubleClickEvent(event)


class VoiceListener:
    # Escuta o microfone padrão e só envia fala após a palavra "aurora".

    # Usa o dispositivo de entrada padrão do Windows. Quando o dispositivo usa
    # WASAPI, solicita explicitamente modo compartilhado (exclusive=False).
    # O Vosk processa o áudio localmente; texto fora da janela de ativação não é
    # encaminhado ao agente.
    #

    def __init__(
        self,
        model_path: Path,
        text_callback: Callable[[str], None],
        status_callback: Callable[[str], None],
        wakeword: str = "aurora",
        command_timeout_s: float = 8.0,
        activation_callback: Optional[Callable[[], None]] = None,
        standby_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        self.model_path = model_path
        self.text_callback = text_callback
        self.status_callback = status_callback
        self.wakeword = (wakeword or "aurora").strip().casefold()
        self.command_timeout_s = max(2.0, float(command_timeout_s))
        self.activation_callback = activation_callback
        self.standby_callback = standby_callback
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=20)
        self._stream = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            self.status_callback("Já estou escutando a palavra de ativação 'Aurora'.")
            return
        if not self.model_path.is_dir():
            raise FileNotFoundError(
                f"Modelo Vosk não encontrado: {self.model_path}\n"
                "Extraia o modelo para essa pasta ou defina AURORA_VOSK_MODEL."
            )

        try:
            import sounddevice as sd
            import numpy as np
            from vosk import KaldiRecognizer, Model
        except ImportError as exc:
            raise RuntimeError(
                "A escuta por voz exige sounddevice, vosk e numpy.\n"
                "Instale com: py -m pip install sounddevice vosk numpy"
            ) from exc

        self._stop_event.clear()
        self.status_callback("Carregando modelo de reconhecimento de voz…")

        def worker() -> None:
            failed = False
            try:
                model = Model(str(self.model_path))
                recognizer = KaldiRecognizer(model, 16000)

                # O Vosk continua detectando a wakeword; Whisper transcreve comandos
                # em português, inglês ou com os dois idiomas na mesma frase.
                whisper_model = None
                stt_backend = os.environ.get("AURORA_STT_BACKEND", "whisper").strip().lower()
                if stt_backend in {"whisper", "faster-whisper"}:
                    try:
                        from faster_whisper import WhisperModel

                        whisper_size = os.environ.get("AURORA_WHISPER_MODEL", "small").strip() or "small"
                        whisper_device = os.environ.get("AURORA_WHISPER_DEVICE", "cpu").strip().lower()
                        default_compute = "int8" if whisper_device == "cpu" else "float16"
                        whisper_compute = os.environ.get("AURORA_WHISPER_COMPUTE_TYPE", default_compute).strip()
                        self.status_callback(
                            f"Carregando reconhecimento multilíngue Whisper ({whisper_size}); "
                            "a primeira inicialização pode baixar o modelo…"
                        )
                        whisper_model = WhisperModel(
                            whisper_size,
                            device=whisper_device,
                            compute_type=whisper_compute,
                        )
                        self.status_callback(
                            f"Whisper multilíngue ativo ({whisper_size}); aguardando 'Aurora'."
                        )
                    except Exception as exc:
                        whisper_model = None
                        self.status_callback(
                            "Whisper indisponível; usando reconhecimento Vosk em português. "
                            "Confira faster-whisper e o download do modelo."
                        )
                        print(f"[Aurora STT] Não foi possível iniciar Whisper: {exc}")

                def transcribe_utterance(pcm_audio: bytes) -> str:
                    # Transcreve um segmento de fala mantendo o idioma original.
                    if whisper_model is None or not pcm_audio:
                        return ""
                    try:
                        audio = np.frombuffer(pcm_audio, dtype=np.int16).astype(np.float32) / 32768.0
                        if audio.size < 4000:  # menos de 250 ms tende a ser ruído/segmento incompleto
                            return ""
                        segments, info = whisper_model.transcribe(
                            audio,
                            task="transcribe",
                            language=None,  # autodetecta: não forçar português nem inglês
                            beam_size=5,
                            condition_on_previous_text=False,
                            initial_prompt=(
                                "Fala em português brasileiro e English, podendo misturar os idiomas. "
                                "Preserve palavras inglesas e nomes técnicos exatamente quando falados, "
                                "por exemplo: browser, open, click, scroll, screenshot, search, "
                                "Windows, VS Code, Python, PowerShell, GitHub e mouse. "
                                "Transcreva o áudio, não traduza."
                            ),
                        )
                        parts = [segment.text.strip() for segment in segments if segment.text.strip()]
                        transcript = " ".join(parts).strip()
                        if transcript:
                            lang = getattr(info, "language", "?")
                            probability = getattr(info, "language_probability", 0.0)
                            print(f"[Aurora STT] Whisper ({lang}, {probability:.2f}): {transcript}")
                        return transcript
                    except Exception as exc:
                        print(f"[Aurora STT] Falha no Whisper; usando texto Vosk: {exc}")
                        return ""

                # Descarta áudio velho de uma execução anterior.
                while True:
                    try:
                        self._audio_queue.get_nowait()
                    except queue.Empty:
                        break

                # sounddevice usa o dispositivo padrão quando device=None.
                # WASAPI em modo compartilhado permite que outros aplicativos
                # também utilizem o microfone do Windows.
                extra_settings = None
                try:
                    default_input = sd.default.device[0]
                    if default_input is not None and int(default_input) >= 0:
                        device_info = sd.query_devices(int(default_input), "input")
                        host_info = sd.query_hostapis(int(device_info["hostapi"]))
                        if "WASAPI" in str(host_info.get("name", "")).upper():
                            extra_settings = sd.WasapiSettings(exclusive=False)
                except Exception as exc:
                    # O modo padrão do PortAudio também é compartilhado em
                    # backends não exclusivos; falha de detecção não deve parar.
                    print(f"[Aurora voz] Não consegui confirmar o backend de áudio: {exc}")

                active_until = 0.0
                wakeword_pattern = re.compile(
                    rf"(?<!\w){re.escape(self.wakeword)}(?!\w)",
                    flags=re.IGNORECASE,
                )

                def return_to_standby() -> None:
                    if self.standby_callback is not None:
                        try:
                            self.standby_callback()
                        except Exception as exc:
                            print(f"[Aurora voz] falha ao voltar para idle: {exc}")

                def dispatch_command(command: str) -> None:
                    command = command.strip(" \t\r\n,.;:!?-–—")
                    if command and self.text_callback:
                        self.text_callback(command)

                def handle_recognized(phrase: str, utterance_pcm: bytes) -> None:
                    nonlocal active_until
                    phrase = (phrase or "").strip()
                    if not phrase:
                        return

                    now = time.monotonic()
                    if active_until and now > active_until:
                        active_until = 0.0
                        self.status_callback("Ativação expirou; aguardando 'Aurora'.")
                        return_to_standby()

                    match = wakeword_pattern.search(phrase)
                    if match:
                        if self.activation_callback is not None:
                            try:
                                self.activation_callback()
                            except Exception as exc:
                                print(f"[Aurora voz] falha no callback de ativação: {exc}")

                        # Reouve o segmento inteiro. Isso permite entender, por
                        # exemplo, "Aurora, open the browser" mesmo que o Vosk
                        # em português erre as palavras inglesas.
                        whispered = transcribe_utterance(utterance_pcm)
                        if whispered:
                            whisper_match = wakeword_pattern.search(whispered)
                            if whisper_match:
                                command = whispered[whisper_match.end():].strip(" \t\r\n,.;:!?-–—")
                            else:
                                command = whispered.strip(" \t\r\n,.;:!?-–—")
                                if command.casefold() == self.wakeword:
                                    command = ""
                        else:
                            command = phrase[match.end():].strip(" \t\r\n,.;:!?-–—")

                        # Fallback para o texto Vosk caso Whisper não tenha reconhecido
                        # a parte do comando, mas Vosk tenha reconhecido alguma.
                        if not command:
                            command = phrase[match.end():].strip(" \t\r\n,.;:!?-–—")

                        if command:
                            active_until = 0.0
                            self.status_callback("Palavra de ativação detectada; processando comando bilíngue…")
                            dispatch_command(command)
                        else:
                            active_until = time.monotonic() + self.command_timeout_s
                            self.status_callback(
                                f"Aurora ativada. Diga seu pedido em até {int(self.command_timeout_s)} segundos."
                            )
                        return

                    if active_until and now <= active_until:
                        active_until = 0.0
                        self.status_callback("Pedido reconhecido; retornando à escuta por 'Aurora'.")
                        whispered = transcribe_utterance(utterance_pcm)
                        dispatch_command(whispered or phrase)

                def audio_callback(indata, frames, time_info, status) -> None:
                    del frames, time_info
                    if self._stop_event.is_set():
                        raise sd.CallbackStop()
                    # Não faça I/O nem reconhecimento dentro do callback de áudio.
                    pcm = (indata[:, 0] * 32767.0).clip(-32768, 32767).astype(np.int16).tobytes()
                    try:
                        self._audio_queue.put_nowait(pcm)
                    except queue.Full:
                        # Mantém áudio recente, evitando atraso acumulado.
                        try:
                            self._audio_queue.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            self._audio_queue.put_nowait(pcm)
                        except queue.Full:
                            pass

                stream_kwargs = {
                    "device": None,  # dispositivo de entrada padrão do Windows
                    "samplerate": 16000,
                    "channels": 1,
                    "dtype": "float32",
                    "callback": audio_callback,
                    "blocksize": 1600,  # ~100 ms a 16 kHz
                }
                if extra_settings is not None:
                    stream_kwargs["extra_settings"] = extra_settings

                with sd.InputStream(**stream_kwargs) as stream:
                    self._stream = stream
                    self.status_callback("Escutando por 'Aurora' (microfone padrão, modo compartilhado quando disponível)…")
                    utterance_audio = bytearray()
                    max_utterance_bytes = 25 * 16000 * 2  # limite de 25 s, mono PCM 16-bit / 16 kHz
                    while not self._stop_event.is_set():
                        if active_until and time.monotonic() > active_until:
                            active_until = 0.0
                            self.status_callback("Ativação expirou; aguardando 'Aurora'.")
                            return_to_standby()
                        try:
                            block = self._audio_queue.get(timeout=0.15)
                        except queue.Empty:
                            continue
                        utterance_audio.extend(block)
                        if len(utterance_audio) > max_utterance_bytes:
                            del utterance_audio[:-max_utterance_bytes]
                        if recognizer.AcceptWaveform(block):
                            captured_audio = bytes(utterance_audio)
                            utterance_audio.clear()
                            try:
                                result = json.loads(recognizer.Result())
                            except (json.JSONDecodeError, TypeError):
                                continue
                            handle_recognized(str(result.get("text") or ""), captured_audio)

            except Exception as exc:
                failed = True
                if not self._stop_event.is_set():
                    self.status_callback(f"Falha no reconhecimento de voz: {exc}")
                    print(f"[Aurora voz] Falha no listener: {exc}")
            finally:
                self._stream = None
                if self._stop_event.is_set():
                    self.status_callback("Escuta parada.")
                elif not failed:
                    self.status_callback("Escuta encerrada.")

        self._thread = threading.Thread(
            target=worker,
            name="AuroraWakeWordListener",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        if not thread or not thread.is_alive():
            self._thread = None


class _ApprovalTicket:
    # Compartilha pedido e resultado entre a thread do agente e a interface Qt.
    def __init__(self, request: object) -> None:
        self.request = request
        self.event = threading.Event()
        self.approved = False


class _GuiApprover:
    # O agente aguarda uma resposta da HUD, nunca input() oculto no terminal.
    def __init__(self, request_signal, timeout_s: float = 95.0) -> None:
        self._request_signal = request_signal
        self._timeout_s = max(5.0, timeout_s)

    def approve(self, request: object) -> bool:
        ticket = _ApprovalTicket(request)
        self._request_signal.emit(ticket)
        if not ticket.event.wait(timeout=self._timeout_s):
            print("[Aurora aprovação] Timeout esperando confirmação na interface; ação negada.", flush=True)
            return False
        return bool(ticket.approved)


class ResponseTextBrowser(QTextBrowser):
    user_scrolled = Signal()

    def wheelEvent(self, event) -> None:  # noqa: N802
        self.user_scrolled.emit()
        super().wheelEvent(event)


class ResponsePanel(QDialog):
    """Painel de leitura para respostas longas, com rolagem automática suave."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("Aurora — resposta completa")
        self.resize(440, 560)
        self.setMinimumSize(320, 260)
        self.setStyleSheet(
            "QDialog { background: #07131d; color: #c8faff; }"
            "QLabel { color: #9dffff; font-weight: bold; }"
            "QTextBrowser { background: #091c29; color: #d9faff; "
            "border: 1px solid #176078; border-radius: 6px; padding: 10px; "
            "font-family: 'Segoe UI', sans-serif; font-size: 10pt; }"
            "QPushButton { color: #9dffff; background: #103447; "
            "border: 1px solid #1685a5; border-radius: 5px; padding: 6px 10px; }"
            "QPushButton:hover { background: #15516a; }"
        )
        layout = QVBoxLayout(self)
        heading = QLabel("RESPOSTA DA AURORA", self)
        layout.addWidget(heading)
        self.text_view = ResponseTextBrowser(self)
        self.text_view.setOpenExternalLinks(True)
        self.text_view.user_scrolled.connect(self._pause_auto_scroll)
        layout.addWidget(self.text_view, 1)
        self.resume_button = QPushButton("Retomar rolagem automática", self)
        self.resume_button.clicked.connect(self.start_auto_scroll)
        layout.addWidget(self.resume_button)
        self._auto_scrolling = False
        self._auto_scroll_timer = QTimer(self)
        self._auto_scroll_timer.setInterval(30)
        self._auto_scroll_timer.timeout.connect(self._advance_auto_scroll)
        self.text_view.verticalScrollBar().valueChanged.connect(self._on_scroll_changed)
        self.text_view.verticalScrollBar().sliderPressed.connect(self._pause_auto_scroll)
        self.text_view.verticalScrollBar().actionTriggered.connect(self._pause_auto_scroll)

    def _pause_auto_scroll(self, *_args) -> None:
        self._auto_scrolling = False
        self._auto_scroll_timer.stop()

    def set_response(self, text: str) -> None:
        self._auto_scroll_timer.stop()
        self._auto_scrolling = False
        self.text_view.setPlainText(text)
        self.text_view.verticalScrollBar().setValue(0)
        QTimer.singleShot(2200, self.start_auto_scroll)

    def start_auto_scroll(self) -> None:
        bar = self.text_view.verticalScrollBar()
        if bar.value() >= bar.maximum():
            bar.setValue(0)
        self._auto_scrolling = True
        self._auto_scroll_timer.start()

    def _advance_auto_scroll(self) -> None:
        bar = self.text_view.verticalScrollBar()
        if bar.value() >= bar.maximum():
            self._auto_scroll_timer.stop()
            self._auto_scrolling = False
            return
        self._auto_scrolling = True
        bar.setValue(min(bar.maximum(), bar.value() + 1))

    def _on_scroll_changed(self, _value: int) -> None:
        if not self._auto_scrolling:
            self._auto_scroll_timer.stop()

class SvgHologram(QMainWindow):
    response_requested = Signal(str)
    recognized_text = Signal(str)
    voice_status = Signal(str)
    show_caption_requested = Signal(str)
    speak_requested = Signal(str)
    speech_ready = Signal(str, str, str, str, int, object)  # legenda, fala, WAV, estado, geração, evento
    speech_preparation_failed = Signal(str, str, str, int, object)
    speech_playback_finished = Signal(str, str, int, object)
    speech_queue_state_changed = Signal(int, bool)
    visual_state_requested = Signal(str)
    approval_requested = Signal(object)
    agent_task_finished = Signal()

    def __init__(self, svg_path: Path, listen_on_start: bool = False) -> None:
        super().__init__()
        self.setWindowTitle("Aurora")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setStyleSheet("QMainWindow, QWidget { background: transparent; }")
        self.resize(*HUD_BASE_SIZE)
        self.move(100, 100)
        self._drag_offset: Optional[QPoint] = None
        self._base_size = self.size()
        self.setMinimumSize(self._base_size)
        self.setMaximumSize(self._base_size)
        self.setFixedSize(self._base_size)
        try:
            handle = self.windowHandle()
            if handle is not None:
                handle.screenChanged.connect(self._enforce_fixed_size)
        except Exception:
            pass

        self._runtime = None
        self._agent = None
        self._speech = None
        self._task_lock = threading.RLock()
        self._task_active = False
        self._task_cancel_pending = False
        self._active_task_thread: Optional[threading.Thread] = None
        self._active_approval_dialog = None
        self._exit_after_task = False
        self._speech_generation_lock = threading.Lock()
        self._speech_generation = 0
        self._speech_pending = False
        self._speech_active = False
        self._active_speech_generation: Optional[int] = None
        self._active_speech_stop_event: Optional[threading.Event] = None
        self._active_speech_done = None
        self._allow_close = False
        self._close_prompt_active = False
        self._response_panel: Optional[ResponsePanel] = None
        self.approval_requested.connect(
            self._show_approval_dialog, Qt.ConnectionType.QueuedConnection
        )
        self.agent_task_finished.connect(
            self._on_agent_task_finished, Qt.ConnectionType.QueuedConnection
        )
        self._init_runtime()

        central = QWidget(self)
        central.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        central.setAutoFillBackground(False)
        central.setStyleSheet("background: transparent;")
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.svg_widget = SvgWidget(svg_path, central)
        layout.addWidget(self.svg_widget)
        self.svg_widget.drag_started.connect(self._start_drag)
        self.svg_widget.drag_moved.connect(self._move_drag)
        self.svg_widget.hologram_double_clicked.connect(self.open_inline_input)

        # Timer que estava faltando no código original. A animação fica na
        # thread gráfica; o processamento da voz ocorre em uma thread separada.
        self.animation_timer = QTimer(self)
        self.animation_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.animation_timer.timeout.connect(self.svg_widget.advance_animation)
        self.animation_timer.start(16)

        self.recognized_text.connect(self.show_recognized_text)
        self.voice_status.connect(self.show_voice_status)
        self.show_caption_requested.connect(self.show_caption, Qt.ConnectionType.QueuedConnection)
        self.response_requested.connect(self._show_response_panel, Qt.ConnectionType.QueuedConnection)
        self.speak_requested.connect(self._speak_text, Qt.ConnectionType.QueuedConnection)
        self.speech_ready.connect(self._play_prepared_speech, Qt.ConnectionType.QueuedConnection)
        self.speech_preparation_failed.connect(self._speech_preparation_failed, Qt.ConnectionType.QueuedConnection)
        self.speech_playback_finished.connect(self._finish_prepared_speech, Qt.ConnectionType.QueuedConnection)
        self.speech_queue_state_changed.connect(
            self._on_speech_queue_state_changed, Qt.ConnectionType.QueuedConnection
        )
        self.visual_state_requested.connect(self.set_visual_state, Qt.ConnectionType.QueuedConnection)
        self.set_visual_state("idle")

        self._tts_engine = None
        self._tts_lock = threading.Lock()
        self._piper_voice = None
        self._piper_load_lock = threading.Lock()
        self._piper_import_error_reported = False
        self._speech_sequence_lock = threading.Lock()
        self._tts_voice_ja = None
        self._tts_voice_pt = None
        self._init_tts()
        self._translation_cache = {}
        self._applio_available = (
            AURORA_TTS_BACKEND == "nanami_rvc"
            and APPLIO_DIR.exists()
            and RAPHAEL_PTH.exists()
        )
        self._voicevox_enabled = VOICEVOX_AVAILABLE
        # Mantém a API antiga apenas como fallback opcional.
        self._gpt_sovits_process: Optional[subprocess.Popen] = None
        self._gpt_sovits_start_lock = threading.Lock()
        self._gpt_sovits_start_failed = False
        self._gpt_sovits_model_selected = False
        self._gpt_sovits_api_fallback = GPT_SOVITS_API_FALLBACK_ENABLED

        # Motor direto persistente: stdin/stdout local, sem endpoint HTTP.
        self._gpt_sovits_direct_process: Optional[subprocess.Popen] = None
        self._gpt_sovits_direct_messages: Optional[queue.Queue] = None
        self._gpt_sovits_direct_ready = False
        self._gpt_sovits_direct_start_failed = False
        self._gpt_sovits_direct_start_lock = threading.Lock()

        self._gpt_sovits_enabled = (
            AURORA_TTS_BACKEND == "gpt_sovits"
            and GPT_SOVITS_ENABLED
            and GPT_SOVITS_DIRECT_ENABLED
            and GPT_SOVITS_DIRECT_WORKER.is_file()
            and GPT_SOVITS_INFERENCE.is_file()
            and GPT_SOVITS_PYTHON.is_file()
            and GPT_SOVITS_GPT_MODEL.is_file()
            and GPT_SOVITS_SOVITS_MODEL.is_file()
            and GPT_SOVITS_REFERENCE.is_file()
        )
        print(f"[Aurora TTS] Backend principal: {AURORA_TTS_BACKEND}.")
        if AURORA_TTS_BACKEND == "nanami":
            print("[Aurora TTS] Usando a voz japonesa SAPI/pyttsx3 diretamente, sem RVC.")
        if AURORA_TTS_BACKEND == "piper":
            print("[Aurora TTS] GPT-SoVITS e Nanami + RVC estão temporariamente desativados.")
        if self._gpt_sovits_enabled:
            print("[Aurora TTS] GPT-SoVITS Raphael configurado; a inferência direta será iniciada sob demanda.")
        elif (
            AURORA_TTS_BACKEND == "gpt_sovits"
            and GPT_SOVITS_ENABLED
            and GPT_SOVITS_DIRECT_ENABLED
        ):
            print(
                "[Aurora TTS] Motor GPT-SoVITS direto não configurado "
                "(worker, inference_webui.py, Python, modelos ou referência ausentes); "
                "serão usados os fallbacks."
            )
        if AURORA_TTS_BACKEND == "gpt_sovits" and self._gpt_sovits_api_fallback:
            print("[Aurora TTS] Fallback HTTP local GPT-SoVITS habilitado por AURORA_GPT_SOVITS_API_FALLBACK=1.")

        model_path = Path(
            os.environ.get("AURORA_VOSK_MODEL", str(DEFAULT_VOSK_MODEL))
        ).expanduser()
        self.voice_listener = VoiceListener(
            model_path=model_path,
            text_callback=self.recognized_text.emit,
            status_callback=self.voice_status.emit,
            wakeword=os.environ.get("AURORA_WAKE_WORD", "aurora"),
            command_timeout_s=float(os.environ.get("AURORA_WAKE_TIMEOUT", "8")),
            activation_callback=lambda: self.visual_state_requested.emit("listening"),
            standby_callback=lambda: self.visual_state_requested.emit("idle"),
        )

        # Legenda flutuante com efeito fade in/out (estável para janela frameless)
        self.caption_label = QLabel(self)
        self.caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption_label.setWordWrap(True)
        self.caption_label.setMaximumWidth(max(160, HUD_BASE_SIZE[0] - 40))
        self.caption_label.hide()

        self._caption_alpha = 0
        self._caption_fading_in = False
        self._caption_fading_out = False
        self._caption_visible_duration = 4000

        self.caption_anim_timer = QTimer(self)
        self.caption_anim_timer.setInterval(30)
        self.caption_anim_timer.timeout.connect(self._update_caption_fade)

        self.caption_hide_timer = QTimer(self)
        self.caption_hide_timer.setSingleShot(True)
        self.caption_hide_timer.timeout.connect(self._start_caption_fade_out)

        # Campo de entrada inline (sem QDialog modal)
        self.input_container = QWidget(self)
        self.input_container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.input_container.hide()
        input_layout = QHBoxLayout(self.input_container)
        input_layout.setContentsMargins(6, 6, 6, 6)
        input_layout.setSpacing(6)

        self.message_input = QLineEdit(self.input_container)
        self.message_input.setPlaceholderText("Digite sua mensagem...")
        self.message_input.setStyleSheet(
            "QLineEdit {"
            "color: #9dffff;"
            "background-color: rgba(10, 40, 60, 170);"
            "border: 1px solid rgba(0, 200, 255, 180);"
            "border-radius: 6px;"
            "padding: 4px 8px;"
            "font-family: 'Consolas', 'Courier New', monospace;"
            "font-size: 9pt;"
            "}"
        )
        self.message_input.returnPressed.connect(self._submit_inline_message)
        self.message_input.editingFinished.connect(self._on_input_editing_finished)

        self.send_inline_btn = QPushButton("Enviar", self.input_container)
        self.send_inline_btn.setStyleSheet(
            "QPushButton {"
            "color: #9dffff;"
            "background-color: rgba(10, 40, 60, 170);"
            "border: 1px solid rgba(0, 200, 255, 180);"
            "border-radius: 6px;"
            "padding: 4px 10px;"
            "font-family: 'Consolas', 'Courier New', monospace;"
            "font-size: 9pt;"
            "}"
            "QPushButton:hover { background-color: rgba(0, 120, 160, 200); }"
        )
        self.send_inline_btn.clicked.connect(self._submit_inline_message)

        input_layout.addWidget(self.message_input)
        input_layout.addWidget(self.send_inline_btn)
        self.input_container.adjustSize()

        # Sem texto sobre o holograma por padrão.
        self.status_label = QLabel(self)
        self.status_label.hide()

        self.listen_shortcut = QShortcut(QKeySequence("F8"), self)
        self.listen_shortcut.activated.connect(self.toggle_listening)

        # Escape cancela a tarefa em curso; Ctrl+Shift+Q pede para encerrar.
        self.cancel_shortcut = QShortcut(QKeySequence("Escape"), self)
        self.cancel_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.cancel_shortcut.activated.connect(self._on_escape_shortcut)
        self.exit_shortcut = QShortcut(QKeySequence("Ctrl+Shift+Q"), self)
        self.exit_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.exit_shortcut.activated.connect(self.close)

        # Controles sempre visíveis no HUD, independentes do campo de texto.
        self.control_strip = QWidget(self)
        self.control_strip.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        control_layout = QHBoxLayout(self.control_strip)
        control_layout.setContentsMargins(3, 3, 3, 3)
        control_layout.setSpacing(4)
        self.cancel_task_btn = QPushButton("■ Cancelar", self.control_strip)
        self.cancel_task_btn.setToolTip("Cancelar a instrução atual (Escape)")
        self.cancel_task_btn.setFixedSize(98, 28)
        self.cancel_task_btn.setEnabled(False)
        self.cancel_task_btn.clicked.connect(lambda: self.cancel_current_task())
        self.close_aurora_btn = QPushButton("× Sair", self.control_strip)
        self.close_aurora_btn.setToolTip("Encerrar a Aurora (Ctrl+Shift+Q)")
        self.close_aurora_btn.setFixedSize(62, 28)
        self.close_aurora_btn.clicked.connect(self.close)
        control_style = (
            "QPushButton { color: #9dffff; background-color: rgba(8, 28, 42, 225); "
            "border: 1px solid rgba(0, 200, 255, 190); border-radius: 6px; "
            "padding: 3px 6px; font-family: 'Consolas', 'Courier New', monospace; font-size: 8pt; }"
            "QPushButton:hover { background-color: rgba(0, 100, 135, 235); }"
            "QPushButton:disabled { color: rgba(157, 255, 255, 90); "
            "border-color: rgba(0, 200, 255, 65); background-color: rgba(8, 28, 42, 100); }"
        )
        self.cancel_task_btn.setStyleSheet(control_style)
        self.close_aurora_btn.setStyleSheet(control_style)
        control_layout.addWidget(self.cancel_task_btn)
        control_layout.addWidget(self.close_aurora_btn)
        self.control_strip.setFixedSize(172, 34)
        self.control_strip.move(self.width() - self.control_strip.width() - 8, 8)
        self.control_strip.raise_()

        if listen_on_start:
            QTimer.singleShot(600, self.start_listening)

    @Slot(str)
    def _show_response_panel(self, text: str) -> None:
        if not text or not self.isVisible():
            return
        if self._response_panel is None:
            self._response_panel = ResponsePanel(self)
        self._response_panel.set_response(text)
        target = self.frameGeometry().topRight() + QPoint(12, 0)
        self._response_panel.move(target)
        self._response_panel.show()
        self._response_panel.raise_()

    @Slot(str)
    def set_visual_state(self, state: str) -> None:
        # Aplica um preset visual ao núcleo e ao icosaedro.
        if not hasattr(self, "svg_widget"):
            return
        try:
            self.svg_widget.effects.set_state(state)
            self.svg_widget.d20.set_state(state)
            self.svg_widget.update()
            print(f"[Aurora visual] estado={state}")
        except Exception as exc:
            print(f"[Aurora visual] Não foi possível aplicar {state!r}: {exc}")

    @Slot(object)
    def _show_approval_dialog(self, ticket: object) -> None:
        # A caixa roda no thread Qt; o agente fica aguardando no worker.
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Aurora — autorização necessária")
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setText("A Aurora precisa da sua autorização para continuar.")
        request = getattr(ticket, "request", None)
        try:
            details = request.render() if request is not None else "Ação protegida solicitada."
        except Exception:
            details = "Ação protegida solicitada."
        dialog.setInformativeText(
            details + "\n\nEscolha Sim para autorizar esta ação ou Não para recusá-la. "
            "O pedido expira automaticamente em 90 segundos."
        )
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        cancel_task_button = dialog.addButton(
            "Cancelar tarefa", QMessageBox.ButtonRole.DestructiveRole
        )
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self._active_approval_dialog = dialog

        # Fecha a caixa automaticamente para que uma autorização pendente não bloqueie o agente.
        QTimer.singleShot(90_000, dialog.reject)
        try:
            clicked = dialog.exec()
            ticket.approved = clicked == QMessageBox.StandardButton.Yes
            if dialog.clickedButton() is cancel_task_button:
                ticket.approved = False
                self.cancel_current_task(show_feedback=True)
            print(
                f"[Aurora aprovação] ferramenta={getattr(request, 'tool', '?')} "
                f"decisão={'autorizada' if ticket.approved else 'negada'}.",
                flush=True,
            )
        except Exception as exc:
            ticket.approved = False
            print(f"[Aurora aprovação] Falha na caixa de confirmação: {exc}", flush=True)
        finally:
            self._active_approval_dialog = None
            ticket.event.set()

    def _init_runtime(self) -> None:
        if not AURORA_AVAILABLE or Settings is None or build_runtime is None:
            return
        try:
            settings = Settings.load()
            runtime = build_runtime(
                settings,
                voice=False,
                approver=_GuiApprover(self.approval_requested),
            )
            self._runtime = runtime
            self._agent = runtime.agent
            self._speech = runtime.speech
        except Exception as exc:
            print(f"[Aurora UI] Não foi possível conectar ao runtime: {exc}")

    def _init_tts(self) -> None:
        if not PYTTSX3_AVAILABLE:
            print("[Aurora TTS] pyttsx3 não disponível. Voz desativada.")
            return
        try:
            engine = pyttsx3.init()
            voices = engine.getProperty("voices")
            jp_voice = None
            candidates = []
            for v in voices:
                vid = str(getattr(v, "id", "") or "").casefold()
                vname = str(getattr(v, "name", "") or "").casefold()
                language_parts = []
                for raw_lang in getattr(v, "languages", []) or []:
                    if isinstance(raw_lang, bytes):
                        raw_lang = raw_lang.decode("utf-8", errors="ignore")
                    language_parts.append(re.sub(r"[^a-zA-Z-]", "", str(raw_lang)).casefold())
                lang_str = " ".join(language_parts)
                # Evita o antigo teste '"ja" in id', que podia casar com identificadores
                # não japoneses por coincidência. Preferimos cultura/nome explicitamente JA.
                explicit_ja = (
                    "jpn" in vid
                    or "jajp" in vid.replace("-", "").replace("_", "")
                    or "ja-jp" in lang_str
                    or "japanese" in lang_str
                    or "japanese" in vname
                    or "japan" in vname
                    or any(name in vname for name in ("haruka", "ichiro", "sayaka", "ayumi", "nanami"))
                    or bool(re.search(r"(^|[\\\\/_.-])ja([\\\\/_.-]|$)", vid))
                )
                if explicit_ja:
                    candidates.append((vname, str(getattr(v, "id", ""))))
            # Se Nanami estiver instalada, prioriza essa voz sobre outras vozes JA.
            candidates.sort(key=lambda candidate: 0 if "nanami" in candidate[0] else 1)
            jp_voice = candidates[0][1] if candidates else None
            self._tts_voice_ja = jp_voice
            if jp_voice:
                engine.setProperty("voice", jp_voice)
                print(f"[Aurora TTS] Voz japonesa selecionada: {candidates[0][0]} ({jp_voice})")
            else:
                available = [
                    f"{getattr(v, 'name', '?')} [{getattr(v, 'id', '?')}]"
                    for v in voices
                ]
                print("[Aurora TTS] AVISO: nenhuma voz japonesa foi identificada. "
                      "A Aurora não falará em português como fallback. Vozes detectadas: "
                      + "; ".join(available[:12]))
            engine.setProperty("rate", 165)
            engine.setProperty("volume", 0.5)
            self._tts_engine = engine
        except Exception as exc:
            print(f"[Aurora TTS] Falha ao iniciar TTS: {exc}")
            self._tts_engine = None

    def _translate_to_japanese(self, text: str) -> str:
        if not text:
            return ""
        # Texto que já tem hiragana/katakana provavelmente já está em japonês.
        if _JA_KANA_RE.search(text):
            return text
        key = text.strip()
        if key in self._translation_cache:
            cached = self._translation_cache[key]
            if _JA_KANA_RE.search(cached):
                return cached
        if not HTTPX_AVAILABLE:
            print("[Aurora TTS] Tradução japonesa indisponível: pacote httpx não instalado.")
            return ""

        def _do_translate(prompt: str) -> str:
            try:
                payload = {
                    "model": os.environ.get("AURORA_TTS_TRANSLATION_MODEL", "auto").strip() or "auto",
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": key},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 512,
                }
                gateway_url, headers = _tts_gateway_config()
                resp = httpx.post(
                    gateway_url, json=payload, headers=headers, timeout=15.0
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if content:
                        translated = content.strip().strip('"').strip("'").strip("「").strip("」")
                        return translated
                    print("[Aurora TTS] O modelo de tradução retornou conteúdo vazio.")
                elif resp.status_code in (401, 403):
                    self._tts_translation_auth_failed = True
                    print(
                        f"[Aurora TTS] Autenticação da tradução recusada (HTTP {resp.status_code}). "
                        "O TTS agora usa a mesma chave configurada para o runtime; confira a chave/URL "
                        "do gateway e reinicie a Aurora."
                    )
                else:
                    print(f"[Aurora TTS] Tradução recusada pelo gateway (HTTP {resp.status_code}).")
            except Exception as exc:
                print(f"[Aurora TTS] Falha na tradução: {type(exc).__name__}: {exc}")
            return ""

        self._tts_translation_auth_failed = False

        # Primeira tentativa
        translated = _do_translate(
            "Você é um tradutor especializado em japonês falado. Traduza TODO o texto para japonês natural, coloquial e adequado para TTS. "
            "NUNCA responda em português. NUNCA use romaji. NUNCA explique. Retorne APENAS o texto em japonês."
        )
        if translated and _JA_KANA_RE.search(translated):
            self._translation_cache[key] = translated
            return translated
        if self._tts_translation_auth_failed:
            return ""

        # Segunda tentativa mais agressiva
        translated = _do_translate(
            "Traduza para japonês falado. A resposta deve conter hiragana ou katakana, "
            "não pode estar em português nem em romaji. Retorne apenas o texto japonês."
        )
        if translated and _JA_KANA_RE.search(translated):
            self._translation_cache[key] = translated
            return translated

        print("[Aurora TTS] AVISO: não consegui validar uma tradução em japonês; "
              "áudio desta resposta será omitido para não falar português.")
        return ""

    def _speak_with_voicevox_to_file(self, japanese_text: str, out_wav: Path) -> bool:
        if not self._voicevox_enabled or VoicevoxClient is None or asyncio is None:
            return False
        try:
            async def _gen():
                client = VoicevoxClient(VOICEVOX_URL)
                try:
                    audio_query = await client.create_audio_query(japanese_text, speaker=VOICEVOX_SPEAKER)
                    audio = await client.synthesis(audio_query, speaker=VOICEVOX_SPEAKER)
                    return audio
                finally:
                    await client.close()
            audio = asyncio.run(_gen())
            if not audio:
                return False
            out_wav.parent.mkdir(parents=True, exist_ok=True)
            out_wav.write_bytes(audio)
            return out_wav.exists() and out_wav.stat().st_size > 0
        except Exception as exc:
            print(f"[Aurora Voicevox] Erro: {exc}")
            return False

    def _gpt_sovits_api_ready(self) -> bool:
        if not HTTPX_AVAILABLE:
            return False
        try:
            with httpx.Client(timeout=2.0, trust_env=False) as client:
                response = client.get(f"{GPT_SOVITS_API_URL}/docs")
            return response.status_code == 200
        except Exception:
            return False

    def _ensure_gpt_sovits_api(self) -> bool:
        # Inicia a API local uma vez e mantém GPT/SoVITS carregados para falas seguintes.
        if not self._gpt_sovits_enabled or not HTTPX_AVAILABLE:
            return False

        # Se a API já está ativa, configura nela os pesos treinados da Aurora.
        if self._gpt_sovits_api_ready():
            if self._gpt_sovits_model_selected or (
                self._gpt_sovits_process is not None and self._gpt_sovits_process.poll() is None
            ):
                self._gpt_sovits_model_selected = True
                return True
            try:
                with httpx.Client(timeout=180.0, trust_env=False) as client:
                    response = client.post(
                        f"{GPT_SOVITS_API_URL}/set_model",
                        json={
                            "gpt_model_path": str(GPT_SOVITS_GPT_MODEL),
                            "sovits_model_path": str(GPT_SOVITS_SOVITS_MODEL),
                        },
                    )
                response.raise_for_status()
                payload = response.json()
                if payload.get("code", 0) != 0:
                    raise RuntimeError(str(payload))
                self._gpt_sovits_model_selected = True
                print("[Aurora GPT-SoVITS] API existente configurada com os pesos Raphael.")
                return True
            except Exception as exc:
                print(f"[Aurora GPT-SoVITS] API existente não pôde carregar os pesos Raphael: {exc}")
                return False

        with self._gpt_sovits_start_lock:
            if self._gpt_sovits_api_ready():
                self._gpt_sovits_model_selected = True
                return True
            if self._gpt_sovits_start_failed:
                return False
            if not (GPT_SOVITS_DIR / "api.py").is_file():
                print(f"[Aurora GPT-SoVITS] api.py não existe em {GPT_SOVITS_DIR}")
                self._gpt_sovits_start_failed = True
                return False

            try:
                GPT_SOVITS_API_LOG.parent.mkdir(parents=True, exist_ok=True)
                command = [
                    str(GPT_SOVITS_PYTHON), "-s", "api.py",
                    "-s", str(GPT_SOVITS_SOVITS_MODEL),
                    "-g", str(GPT_SOVITS_GPT_MODEL),
                    "-dr", str(GPT_SOVITS_REFERENCE),
                    "-dt", GPT_SOVITS_PROMPT_TEXT,
                    "-dl", "ja",
                    "-d", GPT_SOVITS_DEVICE,
                    "-a", "127.0.0.1",
                    "-p", str(GPT_SOVITS_API_PORT),
                ]
                with GPT_SOVITS_API_LOG.open("a", encoding="utf-8", errors="replace") as log_file:
                    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    self._gpt_sovits_process = subprocess.Popen(
                        command,
                        cwd=str(GPT_SOVITS_DIR),
                        stdin=subprocess.DEVNULL,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        creationflags=creationflags,
                    )
                print(
                    "[Aurora GPT-SoVITS] Iniciando API local; o primeiro carregamento pode demorar. "
                    f"Log: {GPT_SOVITS_API_LOG}"
                )
            except Exception as exc:
                self._gpt_sovits_start_failed = True
                print(f"[Aurora GPT-SoVITS] Não consegui iniciar a API: {exc}")
                return False

            timeout_s = min(
                300.0,
                max(30.0, float(os.environ.get("AURORA_GPT_SOVITS_STARTUP_TIMEOUT", "180"))),
            )
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if self._gpt_sovits_api_ready():
                    self._gpt_sovits_model_selected = True
                    print("[Aurora GPT-SoVITS] API pronta; os pesos ficam carregados entre as falas.")
                    return True
                if self._gpt_sovits_process is not None and self._gpt_sovits_process.poll() is not None:
                    self._gpt_sovits_start_failed = True
                    try:
                        tail = GPT_SOVITS_API_LOG.read_text(encoding="utf-8", errors="replace")[-2500:]
                    except OSError:
                        tail = "(não foi possível ler o log)"
                    print(f"[Aurora GPT-SoVITS] API encerrou na inicialização. Log recente:\n{tail}")
                    return False
                time.sleep(0.7)

            # Não matamos o processo: pode estar terminando o carregamento dos pesos.
            # Próxima fala volta a verificar a disponibilidade antes de usar o fallback.
            print(
                f"[Aurora GPT-SoVITS] API ainda não respondeu após {timeout_s:.0f}s. "
                f"Confira {GPT_SOVITS_API_LOG}; usando fallback nesta fala."
            )
            return False

    def _read_gpt_sovits_direct_messages(self, process: subprocess.Popen, messages: queue.Queue) -> None:
        # Lê respostas JSON do worker sem bloquear a thread da interface.
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    messages.put(line.rstrip("\r\n"))
        except Exception as exc:
            messages.put(json.dumps({"ok": False, "error": str(exc)}))
        finally:
            messages.put(None)

    def _ensure_gpt_sovits_direct(self) -> bool:
        # Inicia uma única instância persistente de inferência direta do GPT-SoVITS.
        if not self._gpt_sovits_enabled:
            return False

        process = self._gpt_sovits_direct_process
        if process is not None and process.poll() is None and self._gpt_sovits_direct_ready:
            return True
        if self._gpt_sovits_direct_start_failed:
            return False

        with self._gpt_sovits_direct_start_lock:
            process = self._gpt_sovits_direct_process
            if process is not None and process.poll() is None and self._gpt_sovits_direct_ready:
                return True
            if self._gpt_sovits_direct_start_failed:
                return False
            if process is not None:
                self._shutdown_gpt_sovits_direct()

            messages: queue.Queue = queue.Queue()
            command = [
                str(GPT_SOVITS_PYTHON),
                "-u",
                str(GPT_SOVITS_DIRECT_WORKER),
                "--root", str(GPT_SOVITS_DIR),
                "--gpt-model", str(GPT_SOVITS_GPT_MODEL),
                "--sovits-model", str(GPT_SOVITS_SOVITS_MODEL),
                "--reference", str(GPT_SOVITS_REFERENCE),
                "--prompt-text", GPT_SOVITS_PROMPT_TEXT,
                "--device", GPT_SOVITS_DEVICE,
            ]
            try:
                GPT_SOVITS_DIRECT_LOG.parent.mkdir(parents=True, exist_ok=True)
                with GPT_SOVITS_DIRECT_LOG.open("a", encoding="utf-8", errors="replace") as log_file:
                    self._gpt_sovits_direct_process = subprocess.Popen(
                        command,
                        cwd=str(GPT_SOVITS_DIR),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=log_file,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
            except Exception as exc:
                self._gpt_sovits_direct_start_failed = True
                print(f"[Aurora GPT-SoVITS] Não consegui iniciar o worker direto: {exc}")
                return False

            process = self._gpt_sovits_direct_process
            self._gpt_sovits_direct_messages = messages
            threading.Thread(
                target=self._read_gpt_sovits_direct_messages,
                args=(process, messages),
                daemon=True,
                name="AuroraGPTSoVITSDirectReader",
            ).start()
            print(
                "[Aurora GPT-SoVITS] Inicializando inferência direta; o primeiro carregamento pode demorar. "
                f"Log: {GPT_SOVITS_DIRECT_LOG}"
            )

            try:
                startup_timeout = min(
                    300.0,
                    max(30.0, float(os.environ.get("AURORA_GPT_SOVITS_STARTUP_TIMEOUT", "240"))),
                )
            except ValueError:
                startup_timeout = 240.0
            deadline = time.monotonic() + startup_timeout

            while time.monotonic() < deadline:
                if process.poll() is not None and messages.empty():
                    self._gpt_sovits_direct_start_failed = True
                    print(
                        "[Aurora GPT-SoVITS] O worker terminou durante a inicialização. "
                        f"Consulte {GPT_SOVITS_DIRECT_LOG}"
                    )
                    self._shutdown_gpt_sovits_direct()
                    return False
                try:
                    line = messages.get(timeout=min(0.5, max(0.05, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                if line is None:
                    self._gpt_sovits_direct_start_failed = True
                    print(f"[Aurora GPT-SoVITS] O worker encerrou sem sinal de pronto. Log: {GPT_SOVITS_DIRECT_LOG}")
                    self._shutdown_gpt_sovits_direct()
                    return False
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    print(f"[Aurora GPT-SoVITS] Resposta inválida do worker: {line[:300]}")
                    continue
                if response.get("ready"):
                    self._gpt_sovits_direct_ready = True
                    self._gpt_sovits_direct_start_failed = False
                    print("[Aurora GPT-SoVITS] Motor direto pronto; os pesos ficam carregados entre as falas.")
                    return True
                if response.get("ok") is False:
                    self._gpt_sovits_direct_start_failed = True
                    print(
                        "[Aurora GPT-SoVITS] Falha ao carregar o motor direto: "
                        f"{response.get('error', 'erro sem detalhes')}. Log: {GPT_SOVITS_DIRECT_LOG}"
                    )
                    self._shutdown_gpt_sovits_direct()
                    return False

            self._gpt_sovits_direct_start_failed = True
            print(
                f"[Aurora GPT-SoVITS] Inicialização excedeu {startup_timeout:.0f}s. "
                f"Consulte {GPT_SOVITS_DIRECT_LOG}"
            )
            self._shutdown_gpt_sovits_direct()
            return False

    def _speak_with_gpt_sovits_direct(self, japanese_text: str) -> Optional[Path]:
        # Gera o WAV pelo GPT-SoVITS carregado localmente, sem servidor HTTP.
        if not self._gpt_sovits_enabled or not japanese_text.strip():
            return None

        work_dir = Path(tempfile.mkdtemp(prefix="aurora_gpt_sovits_direct_"))
        out_wav = work_dir / "aurora_raphael_tts.wav"
        started = time.perf_counter()
        try:
            if not self._ensure_gpt_sovits_direct():
                shutil.rmtree(work_dir, ignore_errors=True)
                return None

            process = self._gpt_sovits_direct_process
            messages = self._gpt_sovits_direct_messages
            if process is None or process.poll() is not None or process.stdin is None or messages is None:
                raise RuntimeError("O worker direto não está disponível.")

            # ASCII-only JSON transport avoids Windows console/code-page and
            # lone-surrogate problems when sending Japanese through a text pipe.
            process.stdin.write(json.dumps({
                "text": japanese_text,
                "output_path": str(out_wav),
            }, ensure_ascii=True) + "\n")
            process.stdin.flush()

            try:
                request_timeout = min(
                    300.0,
                    max(30.0, float(os.environ.get("AURORA_GPT_SOVITS_REQUEST_TIMEOUT", "180"))),
                )
            except ValueError:
                request_timeout = 180.0
            deadline = time.monotonic() + request_timeout

            while time.monotonic() < deadline:
                try:
                    line = messages.get(timeout=min(0.5, max(0.05, deadline - time.monotonic())))
                except queue.Empty:
                    if process.poll() is not None:
                        self._shutdown_gpt_sovits_direct()
                        raise RuntimeError("O worker direto encerrou durante a síntese.")
                    continue
                if line is None:
                    self._shutdown_gpt_sovits_direct()
                    raise RuntimeError("O worker direto fechou o canal de respostas.")
                try:
                    response = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"Resposta inválida do worker: {line[:300]}") from exc
                if response.get("ok"):
                    if not out_wav.is_file() or out_wav.stat().st_size < 44:
                        raise RuntimeError("O worker informou sucesso, mas não gerou um WAV válido.")
                    with out_wav.open("rb") as audio_file:
                        header = audio_file.read(12)
                    if header[:4] != b"RIFF" or b"WAVE" not in header:
                        raise RuntimeError("O worker gerou um arquivo que não é WAV.")
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    print(f"[Aurora GPT-SoVITS] Áudio gerado por inferência direta em {elapsed_ms:.0f} ms.")
                    return out_wav
                raise RuntimeError(response.get("error", "falha sem detalhes na inferência direta"))

            self._shutdown_gpt_sovits_direct()
            raise TimeoutError(f"A inferência direta excedeu {request_timeout:.0f}s.")
        except Exception as exc:
            print(f"[Aurora GPT-SoVITS] Inferência direta falhou: {exc}")
            shutil.rmtree(work_dir, ignore_errors=True)
            return None

    def _speak_with_piper_plus(self, japanese_text: str, out_wav: Path) -> Optional[Path]:
        # Gera WAV com a voz sample-voice-v2 via Piper Plus/ONNX.
        if not japanese_text.strip():
            return None
        if not PIPER_MODEL_PATH.is_file():
            print(f"[Aurora Piper] Modelo ONNX não encontrado: {PIPER_MODEL_PATH}")
            return None
        if not PIPER_CONFIG_PATH.is_file():
            print(f"[Aurora Piper] Configuração do modelo não encontrada: {PIPER_CONFIG_PATH}")
            return None

        try:
            from piper_plus import PiperVoice
        except Exception as exc:
            if not self._piper_import_error_reported:
                print(
                    "[Aurora Piper] Não foi possível importar Piper Plus. Instale no mesmo Python "
                    "que executa a Aurora com: python -m pip install piper-plus. "
                    f"Detalhe: {type(exc).__name__}: {exc}"
                )
                self._piper_import_error_reported = True
            return None

        import wave

        started = time.perf_counter()
        try:
            # Carrega o ONNX uma única vez e reutiliza a sessão para todas as falas.
            with self._piper_load_lock:
                if self._piper_voice is None:
                    self._piper_voice = PiperVoice.load(
                        str(PIPER_MODEL_PATH),
                        config_path=str(PIPER_CONFIG_PATH),
                    )
                    print(f"[Aurora Piper] Voz carregada: {PIPER_MODEL_PATH.name}")
                voice = self._piper_voice

            out_wav.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(out_wav), "wb") as wav_file:
                voice.synthesize(japanese_text, wav_file)

            if not out_wav.is_file() or out_wav.stat().st_size <= 44:
                raise RuntimeError("Piper Plus não produziu um WAV válido.")

            with wave.open(str(out_wav), "rb") as wav_file:
                duration = wav_file.getnframes() / max(1, wav_file.getframerate())
                sample_rate = wav_file.getframerate()

            elapsed = time.perf_counter() - started
            print(
                f"[Aurora Piper] Áudio gerado em {elapsed:.2f}s "
                f"({duration:.2f}s, {sample_rate} Hz)."
            )
            return out_wav
        except Exception as exc:
            try:
                out_wav.unlink(missing_ok=True)
            except OSError:
                pass
            print(f"[Aurora Piper] Falha na síntese: {type(exc).__name__}: {exc}")
            return None

    def _speak_with_gpt_sovits(self, japanese_text: str) -> Optional[Path]:
        # Prioriza o motor direto; a API HTTP só é usada se o fallback for ativado.
        audio = self._speak_with_gpt_sovits_direct(japanese_text)
        if audio is not None:
            return audio
        if self._gpt_sovits_api_fallback:
            print("[Aurora GPT-SoVITS] Tentando fallback explícito pela API HTTP local.")
            return self._speak_with_gpt_sovits_api(japanese_text)
        return None

    def _shutdown_gpt_sovits_direct(self) -> None:
        # Encerra o worker direto que pertence a esta instância da Aurora.
        process = self._gpt_sovits_direct_process
        self._gpt_sovits_direct_ready = False
        if process is not None:
            if process.poll() is None:
                try:
                    if process.stdin is not None:
                        process.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                        process.stdin.flush()
                    process.wait(timeout=4)
                except Exception:
                    try:
                        process.terminate()
                        process.wait(timeout=3)
                    except Exception:
                        try:
                            process.kill()
                        except Exception:
                            pass
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except Exception:
                pass
            try:
                if process.stdout is not None:
                    process.stdout.close()
            except Exception:
                pass
        self._gpt_sovits_direct_process = None
        self._gpt_sovits_direct_messages = None

    def _speak_with_gpt_sovits_api(self, japanese_text: str) -> Optional[Path]:
        # Fallback opcional por API HTTP local (desativado por padrão).
        if not self._gpt_sovits_enabled or not japanese_text.strip():
            return None
        work_dir = Path(tempfile.mkdtemp(prefix="aurora_gpt_sovits_"))
        out_wav = work_dir / "aurora_raphael_tts.wav"
        started = time.perf_counter()
        try:
            if not self._ensure_gpt_sovits_api():
                shutil.rmtree(work_dir, ignore_errors=True)
                return None

            payload = {
                "refer_wav_path": str(GPT_SOVITS_REFERENCE),
                "prompt_text": GPT_SOVITS_PROMPT_TEXT,
                "prompt_language": "ja",
                "text": japanese_text,
                "text_language": "ja",
                "top_k": 15,
                "top_p": 0.8,
                "temperature": 0.8,
                "speed": 1.0,
            }
            timeout_s = min(
                240.0,
                max(30.0, float(os.environ.get("AURORA_GPT_SOVITS_REQUEST_TIMEOUT", "120"))),
            )
            with httpx.Client(timeout=timeout_s, trust_env=False) as client:
                response = client.post(f"{GPT_SOVITS_API_URL}/", json=payload)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:700]}")
            audio = response.content
            if len(audio) < 44 or audio[:4] != b"RIFF" or b"WAVE" not in audio[:12]:
                preview = audio[:300].decode("utf-8", errors="replace")
                raise RuntimeError(f"API não retornou um WAV válido: {preview}")
            out_wav.write_bytes(audio)
            elapsed_ms = (time.perf_counter() - started) * 1000
            print(f"[Aurora GPT-SoVITS] WAV gerado em {elapsed_ms:.0f} ms.")
            return out_wav
        except Exception as exc:
            print(f"[Aurora GPT-SoVITS] Síntese falhou; tentando fallback: {exc}")
            shutil.rmtree(work_dir, ignore_errors=True)
            return None

    def _shutdown_gpt_sovits_api(self) -> None:
        # Encerra apenas a API que esta instância da Aurora iniciou.
        process = self._gpt_sovits_process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
            print("[Aurora GPT-SoVITS] API local encerrada.")
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _speak_with_applio(self, japanese_text: str) -> Optional[Path]:
        # Gera o WAV via edge-tts + RVC, sem iniciar a reprodução.
        if not self._applio_available:
            return None
        work_dir = Path(tempfile.mkdtemp(prefix="aurora_rvc_"))
        try:
            tts_wav = work_dir / "aurora_tts.wav"
            out_wav = work_dir / "aurora_rvc.wav"
            python_exe = str(APPLIO_DIR / "env" / "python.exe")
            if not os.path.exists(python_exe):
                python_exe = sys.executable

            script = (
                "import sys, os, asyncio;"
                f"sys.path.insert(0, r'{APPLIO_DIR}');"
                "from rvc.lib.tools import tts;"
                "asyncio.run(tts.generate_tts(%r, 'ja-JP-NanamiNeural', 0, %r))"
                % (japanese_text, str(tts_wav))
            )
            proc = subprocess.run(
                [python_exe, "-c", script], cwd=str(APPLIO_DIR),
                capture_output=True, timeout=60,
            )
            if proc.returncode != 0 or not tts_wav.exists() or tts_wav.stat().st_size == 0:
                print((proc.stderr or b"").decode("utf-8", errors="replace")[:300])
                shutil.rmtree(work_dir, ignore_errors=True)
                return None

            if RAPHAEL_INDEX:
                index_path = str(RAPHAEL_INDEX)
            else:
                index_path = ""
            cmd2 = [
                python_exe,
                "-c",
                (
                    "import sys;"
                    f"sys.path.insert(0, r'{APPLIO_DIR}');"
                    "from core import run_infer_script;"
                    f"run_infer_script(pitch=0,index_rate=0.75,volume_envelope=1.0,protect=0.33,f0_method='rmvpe',input_path=r'{tts_wav}',output_path=r'{out_wav}',pth_path=r'{RAPHAEL_PTH}',index_path=r'{index_path}',f0_autotune=False,f0_autotune_strength=1.0,proposed_pitch=False,proposed_pitch_threshold=155.0,export_format='WAV',embedder_model='contentvec',formant_shifting=False,formant_qfrency=1.0,formant_timbre=1.0,sid=0,cleanup_mode='off',cleanup_strength=12)"
                ),
            ]
            proc2 = subprocess.run(
                cmd2, cwd=str(APPLIO_DIR), capture_output=True, timeout=300,
            )
            if proc2.returncode != 0 or not out_wav.exists() or out_wav.stat().st_size == 0:
                print((proc2.stderr or b"").decode("utf-8", errors="replace")[:400])
                shutil.rmtree(work_dir, ignore_errors=True)
                return None

            print("[Aurora TTS] Áudio RVC preparado; aguardando início sincronizado.")
            return out_wav
        except Exception as exc:
            print(f"[Aurora RVC] Erro ao preparar áudio: {exc}")
            shutil.rmtree(work_dir, ignore_errors=True)
            return None

    def _prepare_sapi_audio(self, japanese_text: str, out_wav: Path) -> Optional[Path]:
        # O motor pyttsx3/SAPI deve executar no próprio processo/thread COM.
        # O engine criado pela HUD era reutilizado em uma thread de worker, onde
        # runAndWait() podia travar indefinidamente. Isolar a síntese em processo
        # filho permite impor timeout e não bloquear a fila de fala da HUD.
        if not self._tts_engine or not self._tts_voice_ja:
            return None
        text_path = out_wav.with_suffix(".txt")
        worker_path = Path(__file__).resolve().with_name("sapi_synthesize.py")
        timeout_s = max(5.0, float(os.environ.get("AURORA_SAPI_TIMEOUT_S", "35")))
        try:
            out_wav.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(japanese_text, encoding="utf-8")
            command = [
                sys.executable,
                str(worker_path),
                "--text-file", str(text_path),
                "--output", str(out_wav),
                "--voice-id", str(self._tts_voice_ja),
                "--rate", "165",
                "--volume", "0.5",
            ]
            sapi_started = time.monotonic()
            print(
                f"[Aurora TTS] SAPI: sintetizando em processo isolado "
                f"(timeout={timeout_s:.0f}s)."
            )
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()[:500]
                print(f"[Aurora TTS] Processo SAPI falhou (código={completed.returncode}): {detail}")
                return None
            print(
                f"[Aurora TTS] Processo SAPI terminou em "
                f"{time.monotonic() - sapi_started:.1f}s."
            )
            if out_wav.is_file() and out_wav.stat().st_size > 44:
                return out_wav
            print("[Aurora TTS] Processo SAPI terminou sem gerar um WAV válido.")
        except subprocess.TimeoutExpired:
            print(
                f"[Aurora TTS] SAPI excedeu {timeout_s:.0f}s; "
                "processo isolado interrompido para evitar travar a fila de fala."
            )
        except Exception as exc:
            print(f"[Aurora TTS] Não consegui pré-gerar WAV via SAPI: {type(exc).__name__}: {exc}")
        finally:
            try:
                text_path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            out_wav.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    def _queue_speech(self, caption: str, text: str, final_state: str) -> None:
        if not text or not text.strip():
            return
        with self._speech_generation_lock:
            self._speech_generation += 1
            generation = self._speech_generation
            self._speech_pending = True
        print(
            f"[Aurora TTS] Fala enfileirada: geração={generation}, "
            f"caracteres={len(text)}, estado_final={final_state}."
        )
        self.speech_queue_state_changed.emit(generation, True)
        worker = threading.Thread(
            target=self._prepare_speech_job,
            args=(caption, text, final_state, generation),
            daemon=True,
            name="AuroraSpeechPrepare",
        )
        try:
            worker.start()
        except Exception as exc:
            with self._speech_generation_lock:
                if generation == self._speech_generation:
                    self._speech_pending = False
            self.speech_queue_state_changed.emit(generation, False)
            print(f"[Aurora TTS] Não consegui iniciar preparação da fala: {exc}")

    def _is_speech_generation_current(self, generation: int) -> bool:
        with self._speech_generation_lock:
            return generation == self._speech_generation

    @Slot(int, bool)
    def _on_speech_queue_state_changed(self, generation: int, pending: bool) -> None:
        with self._speech_generation_lock:
            if generation != self._speech_generation:
                return
            self._speech_pending = pending
        self._update_cancel_button()

    def _prepare_speech_job(
        self, caption: str, text: str, final_state: str, generation: int,
    ) -> None:
        # Cada fala carrega uma geração. Cancelar invalida falas em fila e sínteses antigas.
        done = threading.Event()
        audio_path: Optional[Path] = None
        job_dir = Path(tempfile.mkdtemp(prefix="aurora_speech_"))
        spoken_text = text
        prep_started = time.monotonic()
        stage = "aguardando fila de fala"
        print(f"[Aurora TTS] Preparação iniciada: geração={generation}.")
        with self._speech_sequence_lock:
            print(f"[Aurora TTS] Geração={generation}: adquiriu a fila de fala.")
            try:
                if not self._is_speech_generation_current(generation):
                    return
                try:
                    stage = "aguardando bloqueio do mecanismo TTS"
                    print(f"[Aurora TTS] Geração={generation}: aguardando lock do TTS.")
                    with self._tts_lock:
                        if not self._is_speech_generation_current(generation):
                            return
                        stage = "traduzindo a resposta para japonês"
                        print(f"[Aurora TTS] Geração={generation}: início da tradução.")
                        translate_started = time.monotonic()
                        spoken_text = self._translate_to_japanese(text)
                        print(
                            f"[Aurora TTS] Geração={generation}: tradução terminou em "
                            f"{time.monotonic() - translate_started:.1f}s; "
                            f"caracteres={len(spoken_text or '')}."
                        )
                        if not self._is_speech_generation_current(generation):
                            return
                        if not spoken_text or not _JA_KANA_RE.search(spoken_text):
                            audio_path = None
                        else:
                            stage = f"sintetizando áudio (backend={AURORA_TTS_BACKEND})"
                            synth_started = time.monotonic()
                            print(f"[Aurora TTS] Geração={generation}: início da {stage}.")
                            if AURORA_TTS_BACKEND == "piper":
                                audio_path = self._speak_with_piper_plus(
                                    spoken_text, job_dir / "aurora_piper.wav"
                                )
                            elif AURORA_TTS_BACKEND == "gpt_sovits":
                                audio_path = self._speak_with_gpt_sovits(spoken_text)
                            elif AURORA_TTS_BACKEND == "nanami_rvc" and self._applio_available:
                                audio_path = self._speak_with_applio(spoken_text)
                            elif AURORA_TTS_BACKEND == "nanami":
                                audio_path = self._prepare_sapi_audio(
                                    spoken_text, job_dir / "aurora_nanami.wav"
                                )
                            if AURORA_TTS_BACKEND != "nanami":
                                if not audio_path and self._voicevox_enabled:
                                    candidate = job_dir / "aurora_voicevox.wav"
                                    if self._speak_with_voicevox_to_file(spoken_text, candidate):
                                        audio_path = candidate
                                if not audio_path:
                                    candidate = job_dir / "aurora_sapi.wav"
                                    audio_path = self._prepare_sapi_audio(spoken_text, candidate)
                            print(
                                f"[Aurora TTS] Geração={generation}: síntese terminou em "
                                f"{time.monotonic() - synth_started:.1f}s; "
                                f"arquivo={'sim' if audio_path and audio_path.is_file() else 'não'}."
                            )
                except Exception as exc:
                    print(f"[Aurora TTS] Geração={generation}: falha na etapa '{stage}': {type(exc).__name__}: {exc}")
                    audio_path = None

                if not self._is_speech_generation_current(generation):
                    return
                if audio_path and audio_path.is_file() and audio_path.stat().st_size > 44:
                    print(
                        f"[Aurora TTS] Geração={generation}: WAV pronto; "
                        f"preparação levou {time.monotonic() - prep_started:.1f}s. "
                        "Enviando áudio para reprodução."
                    )
                    self.speech_ready.emit(
                        caption, spoken_text, str(audio_path), final_state, generation, done
                    )
                else:
                    print(
                        f"[Aurora TTS] Geração={generation}: não foi possível gerar WAV; "
                        f"preparação levou {time.monotonic() - prep_started:.1f}s."
                    )
                    self.speech_preparation_failed.emit(
                        caption, spoken_text, final_state, generation, done
                    )
                print(f"[Aurora TTS] Geração={generation}: aguardando confirmação de fim da fala.")
                if not done.wait(timeout=900):
                    print(f"[Aurora TTS] Geração={generation}: timeout aguardando fim da reprodução.")
                else:
                    print(f"[Aurora TTS] Geração={generation}: a UI confirmou o fim da fala.")
            finally:
                try:
                    if audio_path:
                        shutil.rmtree(Path(audio_path).parent, ignore_errors=True)
                    shutil.rmtree(job_dir, ignore_errors=True)
                except Exception:
                    pass

    def _set_speech_visual_state(self, state: str) -> None:
        # Compatível com a UI que usa set_visual_state ou sinal Qt.
        try:
            setter = getattr(self, "set_visual_state", None)
            if callable(setter):
                setter(state)
            else:
                signal = getattr(self, "visual_state_requested", None)
                if signal is not None:
                    signal.emit(state)
        except Exception as exc:
            print(f"[Aurora visual] Não consegui aplicar {state}: {exc}")

    @Slot(str, str, str, str, int, object)
    def _play_prepared_speech(
        self, caption: str, spoken_text: str, audio_path: str,
        final_state: str, generation: int, done: object,
    ) -> None:
        if not self._is_speech_generation_current(generation):
            done.set()
            return
        self._speech_active = True
        self._active_speech_generation = generation
        stop_event = threading.Event()
        self._active_speech_stop_event = stop_event
        self._active_speech_done = done
        if caption:
            self.show_caption(caption)
        self._set_speech_visual_state("speaking")
        self._update_cancel_button()

        audio_name = Path(audio_path).name
        if audio_name == "aurora_piper.wav":
            if not _lower_wav_volume(audio_path, gain=PIPER_OUTPUT_GAIN):
                print("[Aurora TTS] Não foi possível reduzir o volume da voz Piper; WAV mantido.")
        elif audio_name == "aurora_nanami.wav":
            if not _add_short_echo_wav(audio_path, delay_ms=38, decay=0.14):
                print("[Aurora TTS] Não foi possível aplicar o eco da Nanami; WAV seco mantido.")
            if not _lower_wav_volume(audio_path, gain=0.4):
                print("[Aurora TTS] WAV Nanami mantido no volume original.")
        elif audio_name != "aurora_raphael_tts.wav" and not _lower_wav_volume(audio_path, gain=0.4):
            print("[Aurora TTS] WAV mantido no volume original.")

        import wave
        wav_frames = 0
        wav_duration = 0.0
        wav_rate = 0
        try:
            with wave.open(audio_path, "rb") as check_wav:
                wav_duration = check_wav.getnframes() / max(1, check_wav.getframerate())
                wav_frames = check_wav.getnframes()
                wav_rate = check_wav.getframerate()
            print(
                f"[Aurora TTS] Reproduzindo WAV: {wav_duration:.2f}s, "
                f"{wav_rate} Hz, {Path(audio_path).stat().st_size} bytes."
            )
        except Exception as exc:
            print(f"[Aurora TTS] Não consegui inspecionar WAV antes da reprodução: {exc}")

        def _play() -> None:
            try:
                import winsound
                if stop_event.is_set() or not self._is_speech_generation_current(generation):
                    self.speech_playback_finished.emit("cancelled", caption, generation, done)
                    return
                # A reprodução assíncrona pode ser interrompida sem esperar o fim da fala.
                winsound.PlaySound(audio_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                deadline = time.monotonic() + max(0.15, wav_duration + 0.12)
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    if stop_event.wait(min(0.05, max(0.0, remaining))):
                        break
                cancelled = stop_event.is_set() or not self._is_speech_generation_current(generation)
                if cancelled and not stop_event.is_set():
                    # Geração substituída sem cancelamento explícito: pare a fala antiga
                    # antes de liberar a fila. No cancelamento explícito, o chamador já
                    # parou o áudio; não emita um segundo stop que possa cortar a fala nova.
                    try:
                        winsound.PlaySound(None, 0)
                    except Exception:
                        pass
                elif not cancelled:
                    print(f"[Aurora TTS] Reprodução WAV concluída ({wav_frames} frames).")
                self.speech_playback_finished.emit(
                    "cancelled" if cancelled else final_state, caption, generation, done
                )
            except Exception as exc:
                if stop_event.is_set() or not self._is_speech_generation_current(generation):
                    self.speech_playback_finished.emit("cancelled", caption, generation, done)
                else:
                    print(f"[Aurora TTS] Falha ao reproduzir WAV pronto: {exc}")
                    self.speech_preparation_failed.emit(
                        caption, spoken_text, final_state, generation, done
                    )

        try:
            threading.Thread(target=_play, daemon=True, name="AuroraSpeechPlayback").start()
        except Exception as exc:
            print(f"[Aurora TTS] Não consegui iniciar a reprodução: {exc}")
            self.speech_preparation_failed.emit(caption, spoken_text, final_state, generation, done)

    @Slot(str, str, str, int, object)
    def _speech_preparation_failed(
        self, caption: str, spoken_text: str, final_state: str, generation: int, done: object,
    ) -> None:
        if not self._is_speech_generation_current(generation):
            done.set()
            return
        if self._active_speech_generation != generation or self._active_speech_stop_event is None:
            self._active_speech_stop_event = threading.Event()
        self._active_speech_generation = generation
        stop_event = self._active_speech_stop_event
        self._active_speech_done = done
        self._speech_active = True
        if caption:
            self.show_caption(caption)
        self._set_speech_visual_state("speaking")
        self._update_cancel_button()

        def _fallback() -> None:
            try:
                with self._tts_lock:
                    if (
                        not stop_event.is_set()
                        and self._is_speech_generation_current(generation)
                        and self._tts_engine and self._tts_voice_ja and spoken_text.strip()
                    ):
                        self._tts_engine.setProperty("voice", self._tts_voice_ja)
                        self._tts_engine.setProperty("rate", 165)
                        self._tts_engine.say(spoken_text)
                        self._tts_engine.runAndWait()
            except Exception as exc:
                print(f"[Aurora TTS] Fallback de voz falhou: {exc}")
            finally:
                cancelled = stop_event.is_set() or not self._is_speech_generation_current(generation)
                self.speech_playback_finished.emit(
                    "cancelled" if cancelled else final_state, caption, generation, done
                )

        threading.Thread(target=_fallback, daemon=True, name="AuroraSpeechFallback").start()

    @Slot(str, str, int, object)
    def _finish_prepared_speech(
        self, final_state: str, caption: str, generation: int, done: object,
    ) -> None:
        try:
            if not self._is_speech_generation_current(generation):
                return
            self._speech_active = False
            if self._active_speech_generation == generation:
                self._active_speech_generation = None
                self._active_speech_stop_event = None
                self._active_speech_done = None
            with self._speech_generation_lock:
                if generation == self._speech_generation:
                    self._speech_pending = False
            cancelled = final_state == "cancelled"
            if caption:
                if cancelled:
                    self.caption_hide_timer.stop()
                    if self.caption_label.isVisible():
                        self.caption_label.hide()
                elif self.caption_label.isVisible():
                    words = len(caption.split())
                    hold_ms = max(2500, min(5000, 1700 + words * 100))
                    self.caption_hide_timer.start(hold_ms)
            self._set_speech_visual_state("idle" if cancelled else final_state)
            self._update_cancel_button()
            self.speech_queue_state_changed.emit(generation, False)
        finally:
            done.set()

    def _cancel_speech_output(self) -> bool:
        with self._speech_generation_lock:
            was_pending = self._speech_pending
            self._speech_generation += 1
            generation = self._speech_generation
            self._speech_pending = False
        was_active = self._speech_active
        stop_event = self._active_speech_stop_event
        done = self._active_speech_done
        self._speech_active = False
        self._active_speech_generation = None
        self._active_speech_stop_event = None
        self._active_speech_done = None
        if stop_event is not None:
            stop_event.set()
        try:
            import winsound
            # PlaySound(None, 0) é a forma documentada de parar o som atual.
            winsound.PlaySound(None, 0)
        except Exception:
            pass
        try:
            if self._tts_engine is not None:
                self._tts_engine.stop()
        except Exception:
            pass
        # Libera o preparador da fala agora, inclusive durante o encerramento do HUD.
        if done is not None:
            try:
                done.set()
            except Exception:
                pass
        self.caption_hide_timer.stop()
        if self.caption_label.isVisible():
            self.caption_label.hide()
        self._set_speech_visual_state("idle")
        self.speech_queue_state_changed.emit(generation, False)
        self._update_cancel_button()
        return was_pending or was_active or stop_event is not None

    def speak_with_caption(
        self, caption: str, speech_text: str, *, final_state: Optional[str] = None,
    ) -> None:
        # Respostas longas usam painel rolável; a legenda sobre o HUD fica compacta.
        if len(speech_text) > 420 or speech_text.count("\n") >= 5:
            panel_text = caption
            if panel_text.casefold().startswith("aurora:"):
                panel_text = panel_text.split(":", 1)[1].strip()
            self.response_requested.emit(panel_text or speech_text)
            caption = "Aurora: resposta completa aberta no painel lateral."
        if final_state is None:
            final_state = "error" if "エラーが発生しました" in speech_text else "completed"
        self._queue_speech(caption, speech_text, final_state)

    def _speak_text(self, text: str) -> None:
        if not text:
            return
        final_state = "error" if "エラーが発生しました" in text else "completed"
        self._queue_speech("", text, final_state)

    def speak(self, text: str) -> None:
        if text:
            self.speak_requested.emit(text)

    def _enforce_fixed_size(self) -> None:
        try:
            if hasattr(self, "_base_size") and self._base_size is not None:
                if self.size() != self._base_size:
                    self.setFixedSize(self._base_size)
                    self.resize(self._base_size)
        except Exception:
            pass

    def _start_drag(self, global_pos: QPoint) -> None:
        self._drag_offset = global_pos - self.frameGeometry().topLeft()
        self._enforce_fixed_size()

    def _move_drag(self, global_pos: QPoint) -> None:
        if self._drag_offset is not None:
            self.move(global_pos - self._drag_offset)
        self._enforce_fixed_size()

    def _apply_caption_style(self, alpha: int) -> None:
        alpha = max(0, min(255, alpha))
        bg_alpha = int(alpha * 0.59)
        border_alpha = int(alpha * 0.63)
        text_alpha = int(alpha * 0.99)
        self.caption_label.setStyleSheet(
            "QLabel {"
            f"color: rgba(157, 255, 255, {text_alpha});"
            f"background-color: rgba(10, 40, 60, {bg_alpha});"
            f"border: 1px solid rgba(0, 200, 255, {border_alpha});"
            "border-radius: 6px;"
            "padding: 6px 10px;"
            "font-family: 'Consolas', 'Courier New', monospace;"
            "font-size: 9pt;"
            "}"
        )

    def _update_caption_fade(self) -> None:
        if self._caption_fading_in:
            self._caption_alpha += 25
            if self._caption_alpha >= 255:
                self._caption_alpha = 255
                self._caption_fading_in = False
                self.caption_anim_timer.stop()
            self._apply_caption_style(self._caption_alpha)
            return

        if self._caption_fading_out:
            self._caption_alpha -= 25
            if self._caption_alpha <= 0:
                self._caption_alpha = 0
                self._caption_fading_out = False
                self.caption_anim_timer.stop()
                self.caption_label.hide()
                self._apply_caption_style(0)
                return
            self._apply_caption_style(self._caption_alpha)

    def _start_caption_fade_out(self) -> None:
        if not self.caption_label.isVisible():
            return
        self._caption_fading_in = False
        self._caption_fading_out = True
        if not self.caption_anim_timer.isActive():
            self.caption_anim_timer.start()

    @Slot(str)
    def show_caption(self, text: str, duration_ms: int = None) -> None:
        if not text or not self.isVisible():
            return
        try:
            self.caption_hide_timer.stop()
            self.caption_anim_timer.stop()
            self._caption_fading_in = False
            self._caption_fading_out = False

            self.caption_label.setText(text)
            self.caption_label.adjustSize()

            label_rect = self.caption_label.sizeHint()
            x = (self.width() - label_rect.width()) // 2
            y = self.height() - label_rect.height() - max(16, int(self.height() * 0.08))
            self.caption_label.move(max(10, x), max(10, y))
            self.caption_label.show()

            self._caption_alpha = 0
            self._apply_caption_style(self._caption_alpha)
            self._caption_fading_in = True
            self.caption_anim_timer.start()

            if duration_ms is None:
                words = len(text.split())
                duration_ms = 1500 + words * 350
                duration_ms = max(3000, min(duration_ms, 20000))

            if not getattr(self, "_speech_active", False):
                self.caption_hide_timer.start(duration_ms)
        except Exception as exc:
            print(f"[Aurora caption] falha ao exibir legenda: {exc}", flush=True)

    def open_inline_input(self) -> None:
        self.hide_inline_input()
        self.caption_hide_timer.stop()
        self._start_caption_fade_out()
        self.message_input.clear()
        container_width = max(180, self.width() - 40)
        self.input_container.setFixedWidth(container_width)
        self.input_container.adjustSize()
        x = (self.width() - self.input_container.width()) // 2
        y = self.height() - self.input_container.height() - max(20, int(self.height() * 0.18))
        self.input_container.move(max(10, x), max(10, y))
        self.input_container.show()
        self.message_input.setFocus()
        self.raise_()

    def hide_inline_input(self) -> None:
        if self.input_container.isVisible():
            self.input_container.hide()

    def _on_input_editing_finished(self) -> None:
        if not self.input_container.isVisible():
            return
        if self.message_input.hasFocus() or self.send_inline_btn.hasFocus():
            return
        self.hide_inline_input()

    def _update_cancel_button(self) -> None:
        if not hasattr(self, "cancel_task_btn"):
            return
        with self._task_lock:
            task_active = self._task_active
            cancel_pending = self._task_cancel_pending
        if cancel_pending:
            self.cancel_task_btn.setText("Cancelando…")
            self.cancel_task_btn.setEnabled(False)
            return
        speech_active = self._speech_pending or self._speech_active
        self.cancel_task_btn.setEnabled(task_active or speech_active)
        if task_active:
            self.cancel_task_btn.setText("■ Cancelar")
        elif speech_active:
            self.cancel_task_btn.setText("■ Parar fala")
        else:
            self.cancel_task_btn.setText("■ Cancelar")

    def _dispatch_local_control_command(self, text: str) -> bool:
        command = _classify_hud_control_command(text)
        if command == "cancel":
            self.cancel_current_task()
            return True
        if command == "exit":
            self.close()
            return True
        return False

    def _start_agent_task(self, message: str) -> None:
        if self._agent is None:
            self.visual_state_requested.emit("error")
            self.show_caption_requested.emit(
                "Runtime da Aurora não conectado. Verifique FREELLMAPI_API_KEY."
            )
            return

        with self._task_lock:
            if self._task_active:
                self.show_caption_requested.emit(
                    "Essa nova mensagem não foi enviada à IA porque a tarefa anterior ainda está em andamento. "
                    "Para interrompê-la, pressione Cancelar ou Escape e envie a nova mensagem novamente."
                )
                return
            if self._speech_pending or self._speech_active:
                self._cancel_speech_output()
            self._agent.reset_cancel()
            self._task_cancel_pending = False
            self._task_active = True

        self._update_cancel_button()
        self.show_caption_requested.emit(f"Você: {message}")
        self.visual_state_requested.emit("thinking")

        def _run() -> None:
            task_started = time.monotonic()
            print(f"[Aurora tarefa] Início do turno; tamanho da mensagem={len(message)} caracteres.")
            try:
                reply = self._agent.handle(message)
                reply_text = getattr(reply, "text", "") or ""
                status = str(getattr(reply, "status", "") or "").upper()
                tools_used = getattr(reply, "tools_used", []) or []
                print(
                    f"[Aurora tarefa] Agente retornou status={status or 'desconhecido'}, "
                    f"duração={time.monotonic() - task_started:.1f}s, "
                    f"ferramentas={','.join(str(name) for name in tools_used) or 'nenhuma'}; "
                    f"resposta={len(reply_text)} caracteres."
                )
                if status.endswith("CANCELLED"):
                    self.show_caption_requested.emit("Tarefa cancelada.")
                    self.visual_state_requested.emit("idle")
                elif reply_text:
                    final_state = "error" if status.endswith(("FAILED", "ERROR")) else "completed"
                    self.speak_with_caption(
                        f"Aurora: {reply_text}", reply_text, final_state=final_state
                    )
                else:
                    self.visual_state_requested.emit("completed")
            except Exception as exc:
                print(
                    f"[Aurora tarefa] Exceção após {time.monotonic() - task_started:.1f}s: "
                    f"{type(exc).__name__}: {exc}"
                )
                if self._agent is not None and self._agent.cancel_event.is_set():
                    self.show_caption_requested.emit("Tarefa cancelada.")
                    self.visual_state_requested.emit("idle")
                else:
                    err = f"Erro ao processar: {exc}"
                    self.speak_with_caption(err, "エラーが発生しました。", final_state="error")
            finally:
                print(f"[Aurora tarefa] Worker saindo após {time.monotonic() - task_started:.1f}s.")
                self.agent_task_finished.emit()

        worker = threading.Thread(target=_run, daemon=True, name="AuroraAgentTask")
        self._active_task_thread = worker
        try:
            worker.start()
        except Exception as exc:
            with self._task_lock:
                self._task_active = False
                self._task_cancel_pending = False
            self._update_cancel_button()
            self.show_caption_requested.emit(f"Não consegui iniciar a tarefa: {exc}")
            self.visual_state_requested.emit("error")

    def cancel_current_task(self, show_feedback: bool = True) -> None:
        with self._task_lock:
            task_active = self._task_active
        speech_active = self._speech_pending or self._speech_active
        if not task_active and not speech_active:
            if show_feedback:
                self.show_caption_requested.emit("Não há tarefa nem fala em andamento para cancelar.")
            return

        if task_active:
            with self._task_lock:
                self._task_cancel_pending = True
        if task_active and self._agent is not None:
            self._agent.cancel()
        if speech_active:
            self._cancel_speech_output()
        dialog = self._active_approval_dialog
        if task_active and dialog is not None:
            try:
                dialog.reject()
            except Exception:
                pass

        if task_active:
            self.cancel_task_btn.setText("Cancelando…")
            self.cancel_task_btn.setEnabled(False)
            feedback = (
                "Cancelamento solicitado. Não iniciarei novas ações; se o modelo estiver "
                "respondendo, vou ignorar a resposta quando ela chegar."
            )
        else:
            feedback = "Interrompi a fala para você continuar."
        if show_feedback:
            self.show_caption_requested.emit(feedback)
        self.visual_state_requested.emit("idle")
        if task_active:
            # Mantém o botão desabilitado enquanto o turno cancelado termina de limpar o estado.
            self.cancel_task_btn.setText("Cancelando…")
            self.cancel_task_btn.setEnabled(False)
        else:
            self._update_cancel_button()

    def _on_agent_task_finished(self) -> None:
        with self._task_lock:
            self._task_active = False
            self._task_cancel_pending = False
        self._active_task_thread = None
        self._update_cancel_button()
        if self._exit_after_task:
            self._allow_close = True
            QTimer.singleShot(0, self.close)

    def _on_escape_shortcut(self) -> None:
        with self._task_lock:
            active = self._task_active
        if active or self._speech_pending or self._speech_active:
            self.cancel_current_task()
        elif self.input_container.isVisible():
            self.hide_inline_input()

    def _submit_inline_message(self) -> None:
        if not self.input_container.isVisible():
            return
        message = self.message_input.text().strip()
        self.hide_inline_input()
        if not message:
            self.show_caption_requested.emit("Digite uma mensagem primeiro.")
            self.speak("メッセージを入力してください。")
            return
        if self._dispatch_local_control_command(message):
            return
        self._start_agent_task(message)

    def show_recognized_text(self, text: str) -> None:
        self.process_recognized_text(text)

    def process_recognized_text(self, text: str) -> None:
        if not text:
            return
        message = text.strip()
        if not message:
            return
        if self._dispatch_local_control_command(message):
            return
        self._start_agent_task(message)

    def show_voice_status(self, message: str) -> None:
        print(f"[Aurora voz] {message}")

    def start_listening(self) -> None:
        # Mantém o microfone em espera pela wake word sem congelar o visual.
        #
        # O estado 'listening' só é ativado pelo VoiceListener quando ele realmente
        # reconhece a palavra "Aurora". Enquanto aguarda, a interface permanece em idle.

        try:
            self.voice_listener.start()
            self.visual_state_requested.emit("idle")
        except Exception as exc:
            self.visual_state_requested.emit("error")
            QMessageBox.warning(self, "Aurora — voz", str(exc))

    def stop_listening(self) -> None:
        self.voice_listener.stop()
        self.visual_state_requested.emit("idle")

    def toggle_listening(self) -> None:
        thread = self.voice_listener._thread
        if thread and thread.is_alive():
            self.stop_listening()
        else:
            self.start_listening()

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._allow_close:
            if self._close_prompt_active:
                event.ignore()
                return
            self._close_prompt_active = True
            question = QMessageBox()
            question.setWindowTitle("Aurora — encerrar")
            question.setIcon(QMessageBox.Icon.Question)
            question.setText(
                (
                    "Há uma tarefa em andamento. Deseja cancelar a tarefa e encerrar "
                    "a Aurora assim que a operação atual retornar?"
                    if self._task_active
                    else "Deseja realmente encerrar a Aurora?"
                )
            )
            question.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            question.setDefaultButton(QMessageBox.StandardButton.No)
            question.setEscapeButton(QMessageBox.StandardButton.No)
            question.setWindowFlags(
                Qt.WindowType.Dialog
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.CustomizeWindowHint
                | Qt.WindowType.WindowTitleHint
                | Qt.WindowType.WindowCloseButtonHint
            )
            question.setWindowModality(Qt.WindowModality.ApplicationModal)
            question.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            # Usa caixa independente e posicionamento explícito no centro do monitor da HUD.
            question.adjustSize()
            screen = self.screen()
            if screen is not None:
                area = screen.availableGeometry()
                question.move(
                    area.center().x() - question.width() // 2,
                    area.center().y() - question.height() // 2,
                )
            try:
                question.show()
                question.raise_()
                question.activateWindow()
                question.exec()
                # A decisão é baseada no botão clicado; fechar pelo X retorna None.
                accepted = (
                    question.clickedButton()
                    is question.button(QMessageBox.StandardButton.Yes)
                )
            finally:
                question.hide()
                question.deleteLater()
                self._close_prompt_active = False

            if not accepted:
                event.ignore()
                return
            if self._task_active:
                self._exit_after_task = True
                self.cancel_current_task(show_feedback=False)
                self.show_caption_requested.emit(
                    "Cancelando a tarefa antes de encerrar a Aurora…"
                )
                event.ignore()
                return
            if self._speech_pending or self._speech_active:
                self._cancel_speech_output()
            self._allow_close = True

        self.animation_timer.stop()
        self.stop_listening()
        self._shutdown_gpt_sovits_direct()
        self._shutdown_gpt_sovits_api()
        try:
            if self._runtime is not None:
                self._runtime.close()
        except Exception as exc:
            print(f"[Aurora UI] Erro ao fechar runtime: {exc}")
        super().closeEvent(event)

def _add_short_echo_wav(path: str, delay_ms: int = 38, decay: float = 0.14) -> bool:
    # Soma uma cópia bem baixa do áudio, atrasada alguns milissegundos.
    # Usa só a biblioteca padrão e mantém os canais do WAV original.
    import array
    import sys
    import wave

    try:
        with wave.open(path, "rb") as source:
            params = source.getparams()
            if params.sampwidth != 2 or params.comptype != "NONE" or params.framerate <= 0:
                return False
            frames = source.readframes(source.getnframes())

        original = array.array("h")
        original.frombytes(frames)
        if original.itemsize != 2 or params.nchannels < 1:
            return False
        if sys.byteorder != "little":
            original.byteswap()

        delay_frames = max(1, int(params.framerate * max(1, delay_ms) / 1000))
        delay_samples = delay_frames * params.nchannels
        output = array.array("h", original)
        output.extend([0] * delay_samples)

        # A cópia atrasada é calculada a partir do original para não criar repetições infinitas.
        for index, sample in enumerate(original):
            target_index = index + delay_samples
            mixed = output[target_index] + int(sample * decay)
            output[target_index] = max(-32768, min(32767, mixed))

        if sys.byteorder != "little":
            output.byteswap()

        with wave.open(path, "wb") as target:
            target.setparams(params)
            target.writeframes(output.tobytes())

        print(f"[Aurora TTS] Eco Nanami aplicado: {delay_frames / params.framerate * 1000:.0f} ms, retorno {decay:.0%}.")
        return True

    except (OSError, ValueError, EOFError, wave.Error) as exc:
        print(f"[Aurora TTS] Não foi possível aplicar o eco: {exc}")
        return False


def _lower_wav_volume(path: str, gain: float = 0.4) -> bool:
    # Reduz o volume de WAV PCM de 16 bits sem alterar a duração.
    import array
    import sys
    import wave

    try:
        with wave.open(path, "rb") as source:
            params = source.getparams()

            # Evita alterar formatos que esta função não suporta.
            if params.sampwidth != 2 or params.comptype != "NONE":
                return False

            frames = source.readframes(source.getnframes())

        samples = array.array("h")
        samples.frombytes(frames)

        if samples.itemsize != 2:
            return False

        if sys.byteorder != "little":
            samples.byteswap()

        for i, sample in enumerate(samples):
            samples[i] = max(
                -32768,
                min(32767, int(sample * gain))
            )

        if sys.byteorder != "little":
            samples.byteswap()

        with wave.open(path, "wb") as target:
            target.setparams(params)
            target.writeframes(samples.tobytes())

        return True

    except (OSError, ValueError, EOFError, wave.Error) as exc:
        print(f"[Aurora TTS] Não foi possível ajustar o volume: {exc}")
        return False
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interface holográfica da Aurora"
    )

    parser.add_argument(
        "--svg",
        type=Path,
        default=DEFAULT_SVG,
        help="Caminho do arquivo SVG.",
    )

    listen_group = parser.add_mutually_exclusive_group()

    listen_group.add_argument(
        "--listen",
        dest="listen",
        action="store_true",
        help="Inicia com o microfone ativo.",
    )

    listen_group.add_argument(
        "--no-listen",
        dest="listen",
        action="store_false",
        help="Inicia sem ativar o microfone.",
    )

    parser.set_defaults(listen=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    svg_path = args.svg.expanduser().resolve()
    if not svg_path.is_file():
        print(
            f"ERRO: não encontrei o SVG: {svg_path}\n"
            "Coloque aurora_full.svg ao lado de launch.py ou use --svg CAMINHO.",
            file=sys.stderr,
        )
        return 2

    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")

    app = QApplication(sys.argv)
    app.setApplicationName("Aurora")
    try:
        window = SvgHologram(
    svg_path,
    listen_on_start=args.listen,
)
    except Exception as exc:
        QMessageBox.critical(None, "Erro ao iniciar Aurora", str(exc))
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
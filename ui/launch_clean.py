"""Launcher da interface holográfica nativa da Aurora (PySide6 + QPainter).

Interface HUD oficial da Aurora. Integra com o runtime da Aurora.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, Callable

# Adiciona caminho da app para importar aurora
APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

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
    QWidget,
)

_JA_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]")
FREELLM_URL = os.environ.get("AURORA_FREELLM_URL", "http://localhost:3001")

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

VOICEVOX_URL = os.environ.get("AURORA_VOICEVOX_URL", "http://127.0.0.1:50021")
VOICEVOX_SPEAKER = int(os.environ.get("AURORA_VOICEVOX_SPEAKER", "3"))

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SVG = BASE_DIR / "aurora_full.svg"
DEFAULT_VOSK_MODEL = BASE_DIR / "models" / "vosk-model-small-pt-0.3"


class SvgWidget(QWidget):
    """Widget transparente com animações nativas de QPainter."""

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
        """Avança as animações usando o tempo real entre quadros."""
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

            # Mesma ordem visual do HTML fornecido.
            self.effects.paint_background(painter, rect)
            self.effects.paint_particles(painter, rect)
            self.d20.paint(painter, rect)
            self.effects.paint_core(painter, rect)
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
    """Reconhecimento Vosk com wakeword "aurora" sempre ativo."""

    def __init__(
        self,
        model_path: Path,
        text_callback: Callable[[str], None],
        status_callback: Callable[[str], None],
        wakeword_callback: Optional[Callable[[str], None]] = None,
        wakeword: str = "aurora",
    ) -> None:
        self.model_path = model_path
        self.text_callback = text_callback
        self.status_callback = status_callback
        self.wakeword_callback = wakeword_callback
        self.wakeword = wakeword.lower()
        self.wakeword_enabled = True
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=80)
        self._stream = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self.model_path.is_dir():
            raise FileNotFoundError(
                f"Modelo Vosk não encontrado: {self.model_path}\n"
                "Coloque o modelo na pasta models ou defina AURORA_VOSK_MODEL."
            )

        try:
            import sounddevice as sd
            import numpy as np
            from vosk import KaldiRecognizer, Model
        except ImportError as exc:
            raise RuntimeError(
                "A escuta por voz exige sounddevice, vosk e numpy.\n"
                "Instale com: python -m pip install sounddevice vosk numpy"
            ) from exc

        self._stop_event.clear()
        self.status_callback("Carregando modelo de voz…")

        def worker() -> None:
            try:
                model = Model(str(self.model_path))
                recognizer = KaldiRecognizer(model, 16000)

                # Limpa eventuais blocos restantes de uma sessão anterior.
                while True:
                    try:
                        self._audio_queue.get_nowait()
                    except queue.Empty:
                        break

                def audio_callback(indata, frames, time_info, status) -> None:
                    del frames, time_info
                    if status:
                        # Não poluir muito com warnings
                        pass
                    if self._stop_event.is_set():
                        raise sd.CallbackStop()
                    try:
                        pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
                        try:
                            self._audio_queue.put_nowait(pcm)
                        except queue.Full:
                            pass
                    except Exception:
                        pass

                try:
                    # Usa modo compartilhado por padrão (não exclusivo)
                    with sd.InputStream(
                        samplerate=16000,
                        channels=1,
                        dtype="float32",
                        callback=audio_callback,
                        blocksize=8000,
                    ) as stream:
                        self._stream = stream
                        self.status_callback("Escutando por 'aurora'...")
                        while not self._stop_event.is_set():
                            try:
                                block = self._audio_queue.get(timeout=0.15)
                            except queue.Empty:
                                continue
                            if recognizer.AcceptWaveform(block):
                                result = json.loads(recognizer.Result())
                                recognized = str(result.get("text") or "").strip().lower()
                                if not recognized:
                                    continue
                                if self.wakeword_enabled and self.wakeword and self.wakeword in recognized:
                                    # Extrai comando após a wakeword se houver
                                    cmd = recognized
                                    idx = cmd.find(self.wakeword)
                                    if idx >= 0:
                                        cmd = cmd[idx + len(self.wakeword) :].strip()
                                    if self.wakeword_callback:
                                        self.wakeword_callback(cmd if cmd else recognized)
                                    elif self.text_callback:
                                        self.text_callback(recognized)
                                    continue


        self._thread = threading.Thread(
            target=worker, name="AuroraVoskListener", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if thread and thread.is_alive():
            # Não apaga a referência enquanto a thread ainda está encerrando.
            return
        self._thread = None


class SvgHologram(QMainWindow):
    recognized_text = Signal(str)
    voice_status = Signal(str)
    show_caption_requested = Signal(str)
    speak_requested = Signal(str)

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
        self.resize(500, 500)
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
        self.speak_requested.connect(self._speak_text, Qt.ConnectionType.QueuedConnection)

        self._tts_engine = None
        self._tts_lock = threading.Lock()
        self._tts_voice_ja = None
        self._tts_voice_pt = None
        self._init_tts()
        self._translation_cache = {}
        self._applio_available = APPLIO_DIR.exists() and RAPHAEL_PTH.exists()
        self._voicevox_enabled = VOICEVOX_AVAILABLE

        model_path = Path(
            os.environ.get("AURORA_VOSK_MODEL", str(DEFAULT_VOSK_MODEL))
        ).expanduser()
        self.voice_listener = VoiceListener(
            model_path=model_path,
            text_callback=self.recognized_text.emit,
            status_callback=self.voice_status.emit,
            wakeword_callback=self.recognized_text.emit,
            wakeword="aurora",
        )

        # Legenda flutuante com efeito fade in/out (estável para janela frameless)
        self.caption_label = QLabel(self)
        self.caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption_label.setWordWrap(True)
        self.caption_label.setMaximumWidth(420)
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

        if listen_on_start:
            QTimer.singleShot(400, self.start_listening)
        else:
            QTimer.singleShot(600, self.start_listening)

    def _init_runtime(self) -> None:
        if not AURORA_AVAILABLE or Settings is None or build_runtime is None:
            return
        try:
            settings = Settings.load()
            runtime = build_runtime(settings, voice=False)
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
            for v in voices:
                vid = str(getattr(v, "id", "")).lower()
                vname = str(getattr(v, "name", "")).lower()
                langs = [str(l).lower() for l in getattr(v, "languages", [])]
                lang_str = " ".join(langs)
                if jp_voice is None and (
                    "jpn" in vid
                    or "ja" in vid
                    or "japan" in vname
                    or "haruka" in vname
                    or "ichiro" in vname
                    or "sayaka" in vname
                    or "ayumi" in vname
                    or "ja-jp" in lang_str
                ):
                    jp_voice = v.id
                    break
            self._tts_voice_ja = jp_voice
            if jp_voice:
                engine.setProperty("voice", jp_voice)
            engine.setProperty("rate", 165)
            engine.setProperty("volume", 1.0)
            self._tts_engine = engine
            print(f"[Aurora TTS] Voz japonesa ativa: {jp_voice}")
        except Exception as exc:
            print(f"[Aurora TTS] Falha ao iniciar TTS: {exc}")
            self._tts_engine = None

    def _translate_to_japanese(self, text: str) -> str:
        if not text:
            return text
        if _JA_RE.search(text):
            return text
        key = text.strip()
        if key in self._translation_cache:
            cached = self._translation_cache[key]
            if _JA_RE.search(cached):
                return cached
        if not HTTPX_AVAILABLE:
            return key

        def _do_translate(prompt: str) -> str:
            try:
                payload = {
                    "model": "auto",
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": key},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 512,
                }
                resp = httpx.post(f"{FREELLM_URL}/v1/chat/completions", json=payload, timeout=15.0)
                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if content:
                        translated = content.strip().strip('"').strip("'").strip("「").strip("」")
                        return translated
            except Exception as exc:
                print(f"[Aurora TTS] Falha na tradução: {exc}")
            return ""

        # Primeira tentativa
        translated = _do_translate(
            "Você é um tradutor especializado em japonês falado. Traduza TODO o texto para japonês natural, coloquial e adequado para TTS. "
            "NUNCA responda em português. NUNCA use romaji. NUNCA explique. Retorne APENAS o texto em japonês."
        )
        if translated and _JA_RE.search(translated):
            self._translation_cache[key] = translated
            return translated

        # Segunda tentativa mais agressiva
        translated = _do_translate(
            "Traduza o seguinte para japonês. Responda OBRIGATORIAMENTE em japonês. Saída: apenas japonês."
        )
        if translated and _JA_RE.search(translated):
            self._translation_cache[key] = translated
            return translated

        return key

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

    def _speak_with_applio(self, japanese_text: str) -> bool:
        if not self._applio_available:
            return False
        try:
            work_dir = Path(tempfile.mkdtemp(prefix="aurora_rvc_"))
            tts_wav = work_dir / "aurora_tts.wav"
            out_wav = work_dir / "aurora_rvc.wav"
            python_exe = str(APPLIO_DIR / "env" / "python.exe")
            if not os.path.exists(python_exe):
                python_exe = sys.executable

            # VOICEVOX temporariamente desativado - usando edge-tts (ja-JP-NanamiNeural)
            script = (
                "import sys, os, asyncio;"
                f"sys.path.insert(0, r'{APPLIO_DIR}');"
                "from rvc.lib.tools import tts;"
                "asyncio.run(tts.generate_tts(%r, 'ja-JP-NanamiNeural', 0, %r))" % (japanese_text, str(tts_wav))
            )
            proc = subprocess.run([python_exe, "-c", script], cwd=str(APPLIO_DIR), capture_output=True, timeout=60)
            if proc.returncode != 0 or not tts_wav.exists():
                print(proc.stderr.decode("utf-8", errors="replace")[:300])
                return False
            print("[Aurora TTS] Usando edge-tts (NanamiNeural) como base + Applio/RVC (Raphael_Sensei)")

            if RAPHAEL_INDEX:
                cmd2 = [
                    python_exe,
                    "-c",
                    (
                        "import sys;"
                        f"sys.path.insert(0, r'{APPLIO_DIR}');"
                        "from core import run_infer_script;"
                        f"run_infer_script(pitch=0,index_rate=0.75,volume_envelope=1.0,protect=0.33,f0_method='rmvpe',input_path=r'{tts_wav}',output_path=r'{out_wav}',pth_path=r'{RAPHAEL_PTH}',index_path=r'{RAPHAEL_INDEX}',f0_autotune=False,f0_autotune_strength=1.0,proposed_pitch=False,proposed_pitch_threshold=155.0,export_format='WAV',embedder_model='contentvec',formant_shifting=False,formant_qfrency=1.0,formant_timbre=1.0,sid=0,cleanup_mode='off',cleanup_strength=12)"
                    ),
                ]
            else:
                cmd2 = [
                    python_exe,
                    "-c",
                    (
                        "import sys;"
                        f"sys.path.insert(0, r'{APPLIO_DIR}');"
                        "from core import run_infer_script;"
                        f"run_infer_script(pitch=0,index_rate=0.75,volume_envelope=1.0,protect=0.33,f0_method='rmvpe',input_path=r'{tts_wav}',output_path=r'{out_wav}',pth_path=r'{RAPHAEL_PTH}',index_path='',f0_autotune=False,f0_autotune_strength=1.0,proposed_pitch=False,proposed_pitch_threshold=155.0,export_format='WAV',embedder_model='contentvec',formant_shifting=False,formant_qfrency=1.0,formant_timbre=1.0,sid=0,cleanup_mode='off',cleanup_strength=12)"
                    ),
                ]
            proc2 = subprocess.run(cmd2, cwd=str(APPLIO_DIR), capture_output=True, timeout=300)
            if proc2.returncode != 0 or not out_wav.exists():
                print(proc2.stderr.decode("utf-8", errors="replace")[:400])
                return False
            try:
                import winsound
                winsound.PlaySound(str(out_wav), winsound.SND_FILENAME)
            except Exception:
                pass
            return True
        except Exception as exc:
            print(f"[Aurora RVC] Erro: {exc}")
            return False

    def _speak_text(self, text: str) -> None:
        if not text:
            return

        def _run():
            with self._tts_lock:
                try:
                    to_speak = self._translate_to_japanese(text)
                    if self._applio_available and self._speak_with_applio(to_speak):
                        return
                    if self._tts_engine and self._tts_voice_ja:
                        engine = self._tts_engine
                        engine.setProperty("voice", self._tts_voice_ja)
                        engine.setProperty("rate", 165)
                        engine.say(to_speak)
                        engine.runAndWait()
                except Exception as exc:
                    print(f"[Aurora TTS] Erro ao falar: {exc}")

        threading.Thread(target=_run, daemon=True, name="AuroraTTS").start()

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
            y = self.height() - label_rect.height() - 40
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

            self.caption_hide_timer.start(duration_ms)
        except Exception as exc:
            print(f"[Aurora caption] falha ao exibir legenda: {exc}", flush=True)

    def open_inline_input(self) -> None:
        self.hide_inline_input()
        self.caption_hide_timer.stop()
        self._start_caption_fade_out()
        self.message_input.clear()
        container_width = max(320, self.width() - 80)
        self.input_container.setFixedWidth(container_width)
        self.input_container.adjustSize()
        x = (self.width() - self.input_container.width()) // 2
        y = self.height() - self.input_container.height() - 90
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

    def _submit_inline_message(self) -> None:
        if not self.input_container.isVisible():
            return
        message = self.message_input.text().strip()
        self.hide_inline_input()
        if not message:
            self.show_caption_requested.emit("Digite uma mensagem primeiro.")
            self.speak("メッセージを入力してください。")
            return
        self.show_caption_requested.emit(f"Você: {message}")
        self.speak("承知しました。")
        if self._agent is not None:
            def _run():
                try:
                    reply = self._agent.handle(message)
                    text = getattr(reply, "text", "") or ""
                    if text:
                        self.show_caption_requested.emit(f"Aurora: {text}")
                        self.speak(text)
                except Exception as exc:
                    err = f"Erro ao processar: {exc}"
                    self.show_caption_requested.emit(err)
                    self.speak("エラーが発生しました。")
            threading.Thread(target=_run, daemon=True, name="AuroraAgentCall").start()
            return
        self.show_caption_requested.emit("Runtime da Aurora não conectado. Verifique FREELLMAPI_API_KEY.")
        self.speak("ランタイムに接続できません。")

    def show_recognized_text(self, text: str) -> None:
        # Executado na thread principal por conexão de Signal.
        self.process_recognized_text(text)

    def process_recognized_text(self, text: str) -> None:
        if not text:
            return
        t = text.strip()
        if not t:
            return
        # Mostra o que foi detectado
        self.show_caption_requested.emit(f"Você: {t}")
        self.speak("承知しました。")
        if self._agent is not None:
            def _run():
                try:
                    reply = self._agent.handle(t)
                    text_reply = getattr(reply, "text", "") or ""
                    if text_reply:
                        self.show_caption_requested.emit(f"Aurora: {text_reply}")
                        self.speak(text_reply)
                except Exception as exc:
                    err = f"Erro ao processar: {exc}"
                    self.show_caption_requested.emit(err)
                    self.speak("エラーが発生しました。")
            threading.Thread(target=_run, daemon=True, name="AuroraAgentCallVoice").start()
            return
        self.show_caption_requested.emit("Runtime da Aurora não conectado.")
        self.speak("ランタイムに接続できません。")

    def show_voice_status(self, message: str) -> None:
        print(f"[Aurora voz] {message}")

    def start_listening(self) -> None:
        try:
            self.voice_listener.start()
        except Exception as exc:
            QMessageBox.warning(self, "Aurora — voz", str(exc))

    def stop_listening(self) -> None:
        self.voice_listener.stop()

    def toggle_listening(self) -> None:
        thread = self.voice_listener._thread
        if thread and thread.is_alive():
            self.stop_listening()
        else:
            self.start_listening()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.animation_timer.stop()
        self.stop_listening()
        super().closeEvent(event)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interface holográfica da Aurora")
    parser.add_argument(
        "--svg",
        type=Path,
        default=DEFAULT_SVG,
        help="Caminho do SVG (padrão: aurora_full.svg ao lado deste script).",
    )
    parser.add_argument(
        "--listen",
        action="store_true",
        help="Inicia o reconhecimento de voz automaticamente (requer modelo Vosk).",
    )
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

    try:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
    except Exception:
        pass
    try:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setApplicationName("Aurora")
    try:
        window = SvgHologram(svg_path, listen_on_start=args.listen)
    except Exception as exc:
        QMessageBox.critical(None, "Erro ao iniciar Aurora", str(exc))
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
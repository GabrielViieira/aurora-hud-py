"""Launcher da interface holográfica nativa da Aurora (PySide6 + QPainter).

Coloque este arquivo na mesma pasta de animation.py e efeitos.py.
O arquivo aurora_full.svg é validado como recurso do projeto, mas as camadas
animadas (feixes, partículas, icosaedro e núcleo) são desenhadas nativamente
por QPainter para evitar duplicação de elementos do SVG.

Uso:
    python launch_fixed.py
    python launch_fixed.py --listen
    python launch_fixed.py --svg "C:\\caminho\\para\\desenho.svg"

A captura de microfone é opcional e só é iniciada com --listen.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Callable

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
    """Reconhecimento Vosk opcional executado fora da thread da interface."""

    def __init__(
        self,
        model_path: Path,
        text_callback: Callable[[str], None],
        status_callback: Callable[[str], None],
    ) -> None:
        self.model_path = model_path
        self.text_callback = text_callback
        self.status_callback = status_callback
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=40)
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
                        self.status_callback(f"Áudio: {status}")
                    if self._stop_event.is_set():
                        raise sd.CallbackStop()
                    pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
                    try:
                        self._audio_queue.put_nowait(pcm)
                    except queue.Full:
                        # O callback não deve bloquear a thread de áudio.
                        pass

                with sd.InputStream(
                    samplerate=16000,
                    channels=1,
                    dtype="float32",
                    callback=audio_callback,
                ) as stream:
                    self._stream = stream
                    self.status_callback("Escutando… (F8 para parar/iniciar)")
                    while not self._stop_event.is_set():
                        try:
                            block = self._audio_queue.get(timeout=0.15)
                        except queue.Empty:
                            continue
                        if recognizer.AcceptWaveform(block):
                            result = json.loads(recognizer.Result())
                            recognized = str(result.get("text") or "").strip()
                            if recognized:
                                self.text_callback(recognized)
            except Exception as exc:
                if not self._stop_event.is_set():
                    self.status_callback(f"Falha no reconhecimento de voz: {exc}")
            finally:
                self._stream = None
                self.status_callback("Escuta parada")

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

        model_path = Path(
            os.environ.get("AURORA_VOSK_MODEL", str(DEFAULT_VOSK_MODEL))
        ).expanduser()
        self.voice_listener = VoiceListener(
            model_path=model_path,
            text_callback=self.recognized_text.emit,
            status_callback=self.voice_status.emit,
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
            return self._translation_cache[key]
        if not HTTPX_AVAILABLE:
            return key
        try:
            payload = {
                "model": "auto",
                "messages": [
                    {
                        "role": "system",
                        "content": "Traduza o texto para japonês natural e coloquial, adequado para TTS. Responda APENAS com o texto em japonês, sem explicações, sem romaji, sem notas.",
                    },
                    {"role": "user", "content": key},
                ],
                "temperature": 0.2,
                "max_tokens": 256,
            }
            resp = httpx.post(f"{FREELLM_URL}/v1/chat/completions", json=payload, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if content:
                    translated = content.strip().strip('"').strip("'")
                    if translated:
                        self._translation_cache[key] = translated
                        return translated
        except Exception as exc:
            print(f"[Aurora TTS] Falha na tradução: {exc}")
        return key

    def _speak_text(self, text: str) -> None:
        if not text or not self._tts_engine:
            return

        def _run():
            with self._tts_lock:
                try:
                    engine = self._tts_engine
                    to_speak = self._translate_to_japanese(text)
                    if self._tts_voice_ja:
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

    def _start_drag(self, global_pos: QPoint) -> None:
        self._drag_offset = global_pos - self.frameGeometry().topLeft()

    def _move_drag(self, global_pos: QPoint) -> None:
        if self._drag_offset is not None:
            self.move(global_pos - self._drag_offset)

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
    def show_caption(self, text: str, duration_ms: int = 4000) -> None:
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
        self.show_caption_requested.emit(
            f"Mensagem recebida:\n{message}\n\nConecte este método ao runtime da Aurora para enviar ao agente."
        )
        self.speak("メッセージを受け取りました。")

    def show_recognized_text(self, text: str) -> None:
        # Executado na thread principal por conexão de Signal.
        self.process_recognized_text(text)

    def process_recognized_text(self, text: str) -> None:
        # Placeholder: conectar ao runtime em vez de exibir uma caixa de diálogo.
        if text:
            self.show_caption_requested.emit(f"Voz: {text}")
            self.speak("聞こえています。")

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

    app = QApplication(sys.argv[:1])
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

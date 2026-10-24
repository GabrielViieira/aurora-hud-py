import sys
import os
from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont
from PySide6.QtSvg import QSvgRenderer

SVG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aurora.svg")

class AuroraWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool |
            Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(400, 200)
        self.svg_renderer = QSvgRenderer(SVG_PATH)
        self.pulse_phase = 0.0
        self.status_text = "Aurora ativa"
        
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_animation)
        self.timer.start(30)
        
    def update_animation(self):
        self.pulse_phase = (self.pulse_phase + 0.05) % (2 * 3.14159)
        self.status_text = f"Aurora • {int(self.pulse_phase * 100) % 100}%"
        self.update()
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        
        self.svg_renderer.render(painter, QRectF(0, 0, self.width(), self.height()))
        
        painter.setPen(QPen(QColor(255, 255, 255, 200)))
        font = QFont("Segoe UI", 10, QFont.Normal)
        painter.setFont(font)
        painter.drawText(10, self.height() - 10, self.status_text)
        
        glow_color = QColor(0, 212, 255, int(100 + 50 * abs(0.5 - (self.pulse_phase / (2 * 3.14159)))))
        painter.setBrush(QBrush(glow_color))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(0, 0, 8, 8, 4, 4)
        painter.drawRoundedRect(self.width() - 8, 0, 8, 8, 4, 4)

app = QApplication(sys.argv)
widget = AuroraWidget()
widget.show()
QTimer.singleShot(1000, app.quit)
sys.exit(app.exec())

"""Feixes, partículas e núcleo pulsante para o holograma Aurora.

Uso típico dentro de um QWidget:
    self.effects = HologramEffects()
    # no timer: self.effects.update(dt)
    # no paintEvent, nesta ordem:
    self.effects.paint_background(painter, self.rect())
    self.effects.paint_particles(painter, self.rect())
    # desenhe o d20 aqui, por cima das partículas...
    self.effects.paint_core(painter, self.rect())

Tudo é desenhado com QPainter, sem HTML, navegador ou SVG animado.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import cos, sin, pi

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QRadialGradient


@dataclass(frozen=True)
class Particle:
    kind: str
    tx: float
    ty: float
    radius: float
    delay: float


class HologramEffects:
    """Efeitos inspirados nas camadas SVG/CSS do HTML fornecido."""

    CANVAS_SIZE = 800.0
    CENTER = 400.0
    PARTICLE_DURATION = 5.0

    # Dez partículas e seus deslocamentos, adaptados do HTML de origem.
    _PARTICLES = (
        Particle("circle", 300, -300, 3.5, 0.0),
        Particle("rect", -320, 200, 3.0, 0.5),
        Particle("circle", 150, 340, 4.5, 1.0),
        Particle("circle", -250, -270, 2.5, 1.5),
        Particle("rect", 350, 80, 2.5, 2.0),
        Particle("circle", -330, -120, 3.5, 2.5),
        Particle("circle", 100, -350, 2.0, 3.0),
        Particle("rect", -150, 370, 2.5, 3.5),
        Particle("circle", 200, 280, 3.0, 4.0),
        Particle("circle", -280, -200, 4.0, 4.5),
    )

    # Os 12 feixes estáticos do desenho original.
    _BEAMS = (
        (0, 400, 800, 400, 1.5, 0.30),
        (400, 0, 400, 800, 1.5, 0.30),
        (117, 117, 683, 683, 1.2, 0.25),
        (117, 683, 683, 117, 1.2, 0.25),
        (0, 200, 800, 600, 0.8, 0.15),
        (0, 600, 800, 200, 0.8, 0.15),
        (200, 0, 600, 800, 0.8, 0.15),
        (600, 0, 200, 800, 0.8, 0.15),
        (0, 100, 800, 700, 0.6, 0.12),
        (0, 700, 800, 100, 0.6, 0.12),
        (100, 0, 700, 800, 0.6, 0.12),
        (700, 0, 100, 800, 0.6, 0.12),
    )

    def __init__(self) -> None:
        self.elapsed = 0.0
        self.core_phase = 0.0

    def update(self, dt: float) -> None:
        """Avança relógios de animação; chamar pelo timer da janela."""
        dt = max(0.0, min(float(dt), 0.1))
        self.elapsed += dt
        self.core_phase = (self.core_phase + dt * (2.0 * pi / 2.5)) % (2.0 * pi)

    @staticmethod
    def _begin_canvas(painter: QPainter, rect: QRectF) -> bool:
        """Estabelece coordenadas lógicas 800x800, mantendo proporção."""
        rect = QRectF(rect)
        side = min(rect.width(), rect.height())
        if side <= 0:
            return False
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(rect.center().x() - side / 2.0,
                          rect.center().y() - side / 2.0)
        painter.scale(side / HologramEffects.CANVAS_SIZE,
                      side / HologramEffects.CANVAS_SIZE)
        return True

    def paint_background(self, painter: QPainter, rect: QRectF) -> None:
        """Desenha os 12 feixes brancos discretos no fundo."""
        if not self._begin_canvas(painter, rect):
            return
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for x1, y1, x2, y2, width, opacity in self._BEAMS:
            alpha = max(0, min(255, round(opacity * 255)))
            pen = QPen(QColor(245, 248, 255, alpha))
            pen.setWidthF(width)
            painter.setPen(pen)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        painter.restore()

    def paint_particles(self, painter: QPainter, rect: QRectF) -> None:
        """Desenha explosões de partículas que saem do centro e desaparecem."""
        if not self._begin_canvas(painter, rect):
            return

        painter.setPen(Qt.PenStyle.NoPen)
        for particle in self._PARTICLES:
            # Distribui as partículas ao longo do ciclo de cinco segundos.
            progress = ((self.elapsed + particle.delay) % self.PARTICLE_DURATION) / self.PARTICLE_DURATION
            if progress < 0.10:
                alpha = progress / 0.10
            else:
                alpha = max(0.0, 1.0 - (progress - 0.10) / 0.90)
            scale = 0.2 + 1.2 * progress
            x = self.CENTER + particle.tx * progress
            y = self.CENTER + particle.ty * progress
            radius = max(0.35, particle.radius * scale)

            painter.setOpacity(max(0.0, min(1.0, alpha)))
            # Halo suave por camadas; aproxima o glow SVG sem filtro pesado.
            halo = QRadialGradient(QPointF(x, y), radius * 3.3)
            halo.setColorAt(0.0, QColor(255, 255, 255, 155))
            halo.setColorAt(0.35, QColor(225, 240, 255, 55))
            halo.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.setBrush(QBrush(halo))
            painter.drawEllipse(QPointF(x, y), radius * 3.3, radius * 3.3)

            painter.setBrush(QColor(255, 255, 255, 245))
            if particle.kind == "rect":
                painter.drawRect(QRectF(x - radius, y - radius,
                                        radius * 2.0, radius * 2.0))
            else:
                painter.drawEllipse(QPointF(x, y), radius, radius)

        painter.setOpacity(1.0)
        painter.restore()

    def paint_core(self, painter: QPainter, rect: QRectF) -> None:
        """Desenha o núcleo central com pulso e três camadas de brilho."""
        if not self._begin_canvas(painter, rect):
            return

        pulse = 0.95 + 0.075 * (1.0 - cos(self.core_phase))
        core_opacity = 0.85 + 0.15 * (1.0 - cos(self.core_phase)) / 2.0

        painter.translate(self.CENTER, self.CENTER)
        painter.scale(pulse, pulse)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setOpacity(core_opacity)

        # Grande halo difuso (aproxima o filtro heavyGlow do SVG).
        outer = QRadialGradient(QPointF(0, 0), 175.0)
        outer.setColorAt(0.0, QColor(255, 255, 255, 100))
        outer.setColorAt(0.38, QColor(245, 248, 255, 42))
        outer.setColorAt(0.72, QColor(235, 240, 255, 12))
        outer.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(QBrush(outer))
        painter.drawEllipse(QPointF(0, 0), 175.0, 175.0)

        middle = QRadialGradient(QPointF(0, 0), 110.0)
        middle.setColorAt(0.0, QColor(255, 255, 255, 230))
        middle.setColorAt(0.25, QColor(255, 255, 255, 150))
        middle.setColorAt(0.65, QColor(240, 244, 255, 50))
        middle.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(QBrush(middle))
        painter.drawEllipse(QPointF(0, 0), 110.0, 110.0)

        inner = QRadialGradient(QPointF(0, 0), 60.0)
        inner.setColorAt(0.0, QColor(255, 255, 255, 255))
        inner.setColorAt(0.25, QColor(255, 255, 255, 235))
        inner.setColorAt(0.65, QColor(245, 247, 255, 70))
        inner.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(QBrush(inner))
        painter.drawEllipse(QPointF(0, 0), 60.0, 60.0)

        # Ponto de energia central com pequeno halo.
        center_halo = QRadialGradient(QPointF(0, 0), 30.0)
        center_halo.setColorAt(0.0, QColor(255, 255, 255, 255))
        center_halo.setColorAt(0.45, QColor(255, 255, 255, 180))
        center_halo.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(QBrush(center_halo))
        painter.drawEllipse(QPointF(0, 0), 30.0, 30.0)
        painter.setBrush(QColor(255, 255, 255, 255))
        painter.drawEllipse(QPointF(0, 0), 18.0, 18.0)

        painter.setOpacity(1.0)
        painter.restore()

    def paint(self, painter: QPainter, rect: QRectF) -> None:
        """Desenha todos os efeitos sem o d20 (para uso independente)."""
        self.paint_background(painter, rect)
        self.paint_particles(painter, rect)
        self.paint_core(painter, rect)

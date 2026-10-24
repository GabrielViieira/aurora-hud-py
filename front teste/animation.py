"""Animação nativa do icosaedro (d20) da Aurora usando PySide6/QPainter.

Uso típico dentro de um QWidget:
    self.d20 = D20Animation()
    # no timer: self.d20.update(dt)
    # no paintEvent: self.d20.paint(painter, self.rect())

O desenho usa um espaço lógico de 800x800 e é escalado para caber no rect.
"""

from __future__ import annotations

from itertools import combinations
from math import cos, sin, sqrt, tau

from PySide6.QtCore import QRectF, QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen


class D20Animation:
    """Icosaedro wireframe 3D, girando suavemente em torno do eixo Y."""

    CANVAS_SIZE = 800.0
    CENTER = 400.0

    def __init__(
        self,
        *,
        scale: float = 165.0,
        rotation_speed: float = 0.72,
        tilt_x: float = 0.2,
    ) -> None:
        self.scale = float(scale)
        # 0.72 rad/s equivale a ~0.012 rad por quadro em 60 FPS,
        # como no JavaScript original.
        self.rotation_speed = float(rotation_speed)
        self.tilt_x = float(tilt_x)
        self.angle_y = 0.0
        self._vertices = self._make_vertices()
        self._edges = self._make_edges(self._vertices)

    def _make_vertices(self) -> list[tuple[float, float, float]]:
        phi = (1.0 + sqrt(5.0)) / 2.0
        raw = [
            (-1, phi, 0), (1, phi, 0), (-1, -phi, 0), (1, -phi, 0),
            (0, -1, phi), (0, 1, phi), (0, -1, -phi), (0, 1, -phi),
            (phi, 0, -1), (phi, 0, 1), (-phi, 0, -1), (-phi, 0, 1),
        ]
        result: list[tuple[float, float, float]] = []
        for x, y, z in raw:
            length = sqrt(x * x + y * y + z * z)
            result.append((x / length * self.scale,
                           y / length * self.scale,
                           z / length * self.scale))
        return result

    @staticmethod
    def _make_edges(
        vertices: list[tuple[float, float, float]],
    ) -> list[tuple[int, int]]:
        """Deriva as 30 arestas pela menor distância entre vértices.

        Isso evita depender da lista de arestas malformada no HTML colado.
        """
        distances: list[tuple[float, int, int]] = []
        for i, j in combinations(range(len(vertices)), 2):
            a, b = vertices[i], vertices[j]
            d2 = sum((a[k] - b[k]) ** 2 for k in range(3))
            distances.append((d2, i, j))
        shortest = min(item[0] for item in distances)
        tolerance = max(1e-8, shortest * 1e-5)
        return [(i, j) for d2, i, j in distances if abs(d2 - shortest) <= tolerance]

    def update(self, dt: float) -> None:
        """Avança a rotação. dt é o tempo decorrido em segundos."""
        dt = max(0.0, min(float(dt), 0.1))
        self.angle_y = (self.angle_y + self.rotation_speed * dt) % tau

    def paint(self, painter: QPainter, rect: QRectF) -> None:
        """Desenha a malha do d20 no retângulo informado."""
        rect = QRectF(rect)
        side = min(rect.width(), rect.height())
        if side <= 0:
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(rect.center())
        painter.scale(side / self.CANVAS_SIZE, side / self.CANVAS_SIZE)

        cy, sy = cos(self.angle_y), sin(self.angle_y)
        cx, sx = cos(self.tilt_x), sin(self.tilt_x)
        projected: list[tuple[float, float, float]] = []

        for x, y, z in self._vertices:
            # Rotação horizontal ao redor do eixo Y.
            x1 = x * cy - z * sy
            z1 = x * sy + z * cy
            # Inclinação fixa da câmera ao redor do eixo X.
            y2 = y * cx - z1 * sx
            z2 = y * sx + z1 * cx
            perspective = 1200.0 / (1200.0 + z2)
            projected.append((x1 * perspective, y2 * perspective, z2))

        # Desenha primeiro as arestas traseiras e depois as dianteiras.
        edges = sorted(
            self._edges,
            key=lambda edge: (projected[edge[0]][2] + projected[edge[1]][2]) / 2,
        )
        for i, j in edges:
            x1, y1, z1 = projected[i]
            x2, y2, z2 = projected[j]
            depth = (z1 + z2) / 2.0
            if depth > 0:
                alpha, width = 192, 1.8
            else:
                alpha, width = 64, 0.9

            pen = QPen(QColor(245, 250, 255, alpha))
            pen.setWidthF(width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            if depth < -40:
                pen.setStyle(Qt.PenStyle.DashLine)
                pen.setDashPattern([3.0, 3.0])
            painter.setPen(pen)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        # Nós nas 12 quinas do icosaedro.
        painter.setPen(Qt.PenStyle.NoPen)
        for x, y, z in projected:
            if z > 0:
                radius, alpha = 2.8, 220
            else:
                radius, alpha = 1.5, 77
            painter.setBrush(QColor(255, 255, 255, alpha))
            painter.drawEllipse(QPointF(x, y), radius, radius)

        painter.restore()

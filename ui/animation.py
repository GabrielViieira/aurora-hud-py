"""Icosaedro 3D (d20) da Aurora, com movimento e cor por estado."""
from __future__ import annotations

from itertools import combinations
from math import cos, sin, sqrt, tau

from PySide6.QtCore import QRectF, QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen


class D20Animation:
    CANVAS_SIZE = 800.0
    CENTER = 400.0

    PALETTES = {
        # A cor do icosaedro acompanha a HUD: ciano normalmente, dourado ao falar.
        "idle": "#38bfff",
        "listening": "#38bfff",
        "thinking": "#38bfff",
        "processing": "#38bfff",
        "speaking": "#e9b95b",
        "completed": "#38bfff",
        "success": "#38bfff",
        "error": "#38bfff",
    }

    def __init__(self, *, scale: float = 165.0, rotation_speed: float = 0.72, tilt_x: float = 0.2) -> None:
        self.scale = float(scale)
        self.rotation_speed = float(rotation_speed)
        self.tilt_x = float(tilt_x)
        self.angle_y = 0.0
        self.state = "idle"
        self.state_elapsed = 0.0
        self.scale_factor = 1.0
        self.target_scale_factor = 1.0
        self.line_color = QColor(self.PALETTES["idle"])
        self._vertices = self._make_vertices()
        self._edges = self._make_edges(self._vertices)

    def _make_vertices(self) -> list[tuple[float, float, float]]:
        phi = (1.0 + sqrt(5.0)) / 2.0
        raw = [
            (-1, phi, 0), (1, phi, 0), (-1, -phi, 0), (1, -phi, 0),
            (0, -1, phi), (0, 1, phi), (0, -1, -phi), (0, 1, -phi),
            (phi, 0, -1), (phi, 0, 1), (-phi, 0, -1), (-phi, 0, 1),
        ]
        result = []
        for x, y, z in raw:
            length = sqrt(x * x + y * y + z * z)
            result.append((x / length * self.scale, y / length * self.scale, z / length * self.scale))
        return result

    @staticmethod
    def _make_edges(vertices: list[tuple[float, float, float]]) -> list[tuple[int, int]]:
        distances = []
        for i, j in combinations(range(len(vertices)), 2):
            a, b = vertices[i], vertices[j]
            d2 = sum((a[k] - b[k]) ** 2 for k in range(3))
            distances.append((d2, i, j))
        shortest = min(item[0] for item in distances)
        tolerance = max(1e-8, shortest * 1e-5)
        return [(i, j) for d2, i, j in distances if abs(d2 - shortest) <= tolerance]

    @staticmethod
    def _normalize_state(state: str) -> str:
        key = str(state or "idle").strip().lower()
        aliases = {
            "rest": "idle", "listen": "listening", "responding": "speaking",
            "answering": "speaking", "done": "completed", "finished": "completed",
            "complete": "completed", "ok": "success", "failed": "error",
            "working": "processing", "processing_tool": "processing",
        }
        key = aliases.get(key, key)
        return key if key in D20Animation.PALETTES else "idle"

    def set_state(self, state: str) -> bool:
        normalized = self._normalize_state(state)
        if normalized != self.state:
            self.state = normalized
            self.state_elapsed = 0.0
        self.line_color = QColor(self.PALETTES[normalized])
        self.target_scale_factor = 1.20 if normalized == "speaking" else 1.0
        return True

    def update(self, dt: float) -> None:
        dt = max(0.0, min(float(dt), 0.1))
        self.state_elapsed += dt
        if self.state == "idle":
            speed = self.rotation_speed * 0.32
        elif self.state == "listening":
            speed = 4.2 if self.state_elapsed <= 0.48 else 0.0
        elif self.state == "thinking":
            speed = 1.15
        elif self.state == "processing":
            speed = 2.8
        elif self.state == "speaking":
            speed = 0.72
        elif self.state == "success":
            speed = 5.3 if self.state_elapsed <= 0.72 else 0.0
        elif self.state == "error":
            speed = 0.0
        else:  # completed: cor azul, movimento lento
            speed = self.rotation_speed * 0.30

        self.angle_y = (self.angle_y + speed * dt) % tau
        # Expansão suave, sem alterar índices/arestas: todas as conexões continuam intactas.
        blend = min(1.0, dt * 5.0)
        self.scale_factor += (self.target_scale_factor - self.scale_factor) * blend

    def paint(self, painter: QPainter, rect: QRectF) -> None:
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
        projected = []
        for x, y, z in self._vertices:
            x1 = x * cy - z * sy
            z1 = x * sy + z * cy
            y2 = y * cx - z1 * sx
            z2 = y * sx + z1 * cx
            perspective = 1200.0 / (1200.0 + z2)
            projected.append((x1 * perspective * self.scale_factor, y2 * perspective * self.scale_factor, z2))

        edges = sorted(self._edges, key=lambda e: (projected[e[0]][2] + projected[e[1]][2]) / 2.0)
        for i, j in edges:
            x1, y1, z1 = projected[i]
            x2, y2, z2 = projected[j]
            depth = (z1 + z2) / 2.0
            alpha = 208 if depth > 0 else 72
            width = 1.8 if depth > 0 else 0.9
            color = QColor(self.line_color)
            color.setAlpha(alpha)
            pen = QPen(color)
            pen.setWidthF(width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            if depth < -40:
                pen.setStyle(Qt.PenStyle.DashLine)
                pen.setDashPattern([3.0, 3.0])
            painter.setPen(pen)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        painter.setPen(Qt.PenStyle.NoPen)
        for x, y, z in projected:
            radius, alpha = (2.8, 225) if z > 0 else (1.5, 90)
            color = QColor(self.line_color)
            color.setAlpha(alpha)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(x, y), radius, radius)
        painter.restore()

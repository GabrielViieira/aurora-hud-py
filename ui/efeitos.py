"""Efeitos nativos do holograma Aurora, com presets por estado.

Estados aceitos: idle, listening, thinking, processing, speaking/responding,
completed, success e error. Os elementos são desenhados com QPainter.

Ordem recomendada no paintEvent:
    effects.paint_background(painter, rect)
    effects.paint_particles(painter, rect)
    d20.paint(painter, rect)
    effects.paint_core(painter, rect)

O núcleo inclui os três anéis de pequenos retângulos. O anel externo contínuo
e os caracteres japoneses são desenhados em paint_background, atrás do d20.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import cos, sin, pi, tau
from typing import ClassVar

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPen,
    QRadialGradient,
)


@dataclass(frozen=True)
class Particle:
    kind: str
    tx: float
    ty: float
    radius: float
    delay: float


class HologramEffects:
    CANVAS_SIZE = 800.0
    CENTER = 400.0
    PARTICLE_DURATION = 5.0

    PALETTES: ClassVar[dict[str, dict[str, str]]] = {
        # Azul-ciano é a identidade constante; dourado sinaliza fala da Aurora.
        "idle": {"primary": "#38bfff", "secondary": "#8cecff", "core": "#ffffff"},
        "listening": {"primary": "#38bfff", "secondary": "#8cecff", "core": "#ffffff"},
        "thinking": {"primary": "#38bfff", "secondary": "#8cecff", "core": "#ffffff"},
        "processing": {"primary": "#38bfff", "secondary": "#8cecff", "core": "#ffffff"},
        "speaking": {"primary": "#e9b95b", "secondary": "#ffe4a0", "core": "#fff5d6"},
        "completed": {"primary": "#38bfff", "secondary": "#8cecff", "core": "#ffffff"},
        "success": {"primary": "#38bfff", "secondary": "#8cecff", "core": "#ffffff"},
        "error": {"primary": "#38bfff", "secondary": "#8cecff", "core": "#ffffff"},
    }

    _PARTICLES: ClassVar[tuple[Particle, ...]] = (
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

    # Raios estruturais discretos; os feixes solares são gerados no paint.
    _BEAMS: ClassVar[tuple[tuple[float, float, float, float, float, float], ...]] = (
        (0, 400, 800, 400, 0.6, 0.08),
        (400, 0, 400, 800, 0.6, 0.08),
        (165, 165, 635, 635, 0.45, 0.06),
        (165, 635, 635, 165, 0.45, 0.06),
    )

    # Inscrições circulares estilizadas: referência visual de Raphael, não texto funcional.
    _SIGIL_TEXT: ClassVar[str] = (
        "RAPHAEL  ·  智慧之王  ·  ANALYSIS  ·  WISDOM  ·  "
        "RAPHAEL  ·  智慧之王  ·  ANALYSIS  ·  WISDOM  ·  "
    )

    # Three independently rotating rings of tiny rectangles around the nucleus:
    # radius, count, rectangle width, rectangle height.
    _CORE_RINGS: ClassVar[tuple[tuple[float, int, float, float], ...]] = (
        (76.0, 36, 2.5, 7.0),
        (94.0, 48, 2.2, 6.5),
        (113.0, 60, 1.8, 5.5),
    )

    # Marcas japonesas decorativas: オーロラ (Aurora), mais conceitos visuais.
    _JP_MARKS: ClassVar[tuple[str, ...]] = ("オ", "ー", "ロ", "ラ", "光", "夢", "星", "心")

    def __init__(self) -> None:
        self.state = "idle"
        self.state_elapsed = 0.0
        self.elapsed = 0.0       # particle clock
        self.beam_clock = 0.0    # independent line fade clock
        self.core_phase = 0.0
        self.ring_angles = [0.0, 0.7, 1.5]
        self.global_rotation_angle = 0.0
        self.primary_color = QColor(self.PALETTES["idle"]["primary"])
        self.secondary_color = QColor(self.PALETTES["idle"]["secondary"])
        self.core_color = QColor(self.PALETTES["idle"]["core"])

    @staticmethod
    def _normalize_state(state: str) -> str:
        key = str(state or "idle").strip().lower()
        aliases = {
            "rest": "idle", "listening_for_wakeword": "listening",
            "listen": "listening", "thinking": "thinking",
            "processing_tool": "processing", "working": "processing",
            "speaking": "speaking", "responding": "speaking",
            "answering": "speaking", "done": "completed",
            "finished": "completed", "complete": "completed",
            "ok": "success", "failed": "error",
        }
        key = aliases.get(key, key)
        return key if key in HologramEffects.PALETTES else "idle"

    def set_state(self, state: str) -> bool:
        """Troca o preset visual e reinicia a janela de impulso do estado."""
        normalized = self._normalize_state(state)
        changed = normalized != self.state
        if changed:
            self.state = normalized
            self.state_elapsed = 0.0
        palette = self.PALETTES[normalized]
        self.primary_color = QColor(palette["primary"])
        self.secondary_color = QColor(palette["secondary"])
        self.core_color = QColor(palette["core"])
        return True

    def set_palette(self, primary: str, secondary: str | None = None, core: str | None = None) -> bool:
        """Define cores personalizadas hexadecimais após validar os valores."""
        p = QColor(primary)
        s = QColor(secondary or primary)
        c = QColor(core or "#ffffff")
        if not (p.isValid() and s.isValid() and c.isValid()):
            return False
        self.primary_color, self.secondary_color, self.core_color = p, s, c
        return True

    def update(self, dt: float) -> None:
        """Avança os relógios; estados de parada congelam os elementos móveis."""
        dt = max(0.0, min(float(dt), 0.1))
        self.state_elapsed += dt

        if self.state == "error":
            motion = 0.0
            pulse_period = 0.42
        elif self.state == "listening":
            motion = 4.0 if self.state_elapsed <= 0.48 else 0.0
            pulse_period = 2.6
        elif self.state == "success":
            motion = 3.8 if self.state_elapsed <= 0.72 else 0.0
            pulse_period = 3.1
        elif self.state == "processing":
            motion = 2.8
            pulse_period = 2.5
        elif self.state == "thinking":
            motion = 1.45
            pulse_period = 1.7
        elif self.state == "speaking":
            motion = 0.85
            pulse_period = 1.05
        elif self.state == "completed":
            motion = 0.32
            pulse_period = 3.0
        else:  # idle: tudo fica mais lento
            motion = 0.28
            pulse_period = 3.4

        self.elapsed += dt * motion
        # Fade aleatório/independente das linhas no idle; no thinking também
        # continuam a variar individualmente, nunca em sincronia perfeita.
        if self.state in {"idle", "thinking"}:
            self.beam_clock += dt * (0.48 if self.state == "idle" else 1.1)
        elif self.state == "processing":
            self.beam_clock += dt * 2.8

        self.core_phase = (self.core_phase + dt * tau / pulse_period) % tau

        # Toda a HUD gira durante interações, não só o icosaedro central.
        # A rotação global compõe com os giros independentes dos anéis internos.
        if self.state == "listening":
            global_speed = 72.0
        elif self.state == "thinking":
            global_speed = 20.0
        elif self.state == "processing":
            global_speed = 42.0
        elif self.state == "speaking":
            global_speed = 15.0
        elif self.state == "success":
            global_speed = 72.0 if self.state_elapsed <= 0.72 else 8.0
        elif self.state == "completed":
            global_speed = 3.0
        else:
            global_speed = 0.0
        self.global_rotation_angle = (self.global_rotation_angle + global_speed * dt) % 360.0

        ring_speeds = self._ring_speeds()
        for index, speed in enumerate(ring_speeds):
            self.ring_angles[index] = (self.ring_angles[index] + speed * dt) % tau

    def _ring_speeds(self) -> tuple[float, float, float]:
        if self.state == "idle":
            return (0.20, -0.15, 0.11)
        if self.state == "listening":
            if self.state_elapsed <= 0.48:
                return (5.0, -4.2, 5.8)
            return (0.0, 0.0, 0.0)
        if self.state == "thinking":
            return (1.25, -0.92, 1.55)  # cada anel no próprio ritmo
        if self.state == "processing":
            return (2.8, 2.8, 2.8)       # movimento conjunto e acelerado
        if self.state == "speaking":
            return (0.68, -0.42, 0.30)
        if self.state == "success":
            return (6.0, -5.0, 7.0) if self.state_elapsed <= 0.72 else (0.0, 0.0, 0.0)
        if self.state == "error":
            return (0.0, 0.0, 0.0)
        return (0.13, -0.09, 0.06)  # completed/azul

    @staticmethod
    def _begin_canvas(painter: QPainter, rect: QRectF) -> bool:
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

    @staticmethod
    def _color_with_alpha(color: QColor, alpha: int) -> QColor:
        result = QColor(color)
        result.setAlpha(max(0, min(255, int(alpha))))
        return result

    def _beam_opacity(self, index: int, base: float) -> float:
        if self.state not in {"idle", "thinking"}:
            return base
        # Frequências/fases diferentes: linhas fazem fade em momentos distintos.
        frequency = 0.55 + (index % 5) * 0.19
        phase = (index * 2.399963229728653) % tau
        wave = (1.0 + sin(self.beam_clock * frequency + phase)) / 2.0
        # Fade entre quase apagado e a opacidade base.
        return base * (0.08 + 0.92 * wave)

    def paint_background(self, painter: QPainter, rect: QRectF) -> None:
        """Desenha feixes com fade independente e os dois anéis externos do d20."""
        if not self._begin_canvas(painter, rect):
            return
        try:
            painter.setBrush(Qt.BrushStyle.NoBrush)

            # Raios solares que partem do centro, como na imagem de referência.
            # Variamos comprimento, espessura e intensidade para evitar uma estrela regular demais.
            ray_count = 68
            for index in range(ray_count):
                angle = -pi / 2 + tau * index / ray_count
                inner_radius = 48.0 if index % 5 else 27.0
                outer_radius = 360.0 if index % 3 else 382.0
                alpha = 78 if index % 4 == 0 else (43 if index % 2 == 0 else 25)
                ray_color = self._color_with_alpha(
                    self.secondary_color if index % 4 == 0 else self.primary_color,
                    alpha,
                )
                ray_pen = QPen(ray_color, 1.15 if index % 4 == 0 else 0.65)
                ray_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(ray_pen)
                painter.drawLine(
                    QPointF(self.CENTER + cos(angle) * inner_radius,
                            self.CENTER + sin(angle) * inner_radius),
                    QPointF(self.CENTER + cos(angle) * outer_radius,
                            self.CENTER + sin(angle) * outer_radius),
                )

                # Pequenos realces usam a cor do estado atual, sem uma
                # terceira cor fixa: azul-ciano no uso normal, dourado ao falar.
                if index % 6 == 1:
                    accent_pen = QPen(
                        self._color_with_alpha(self.secondary_color, 58), 1.1
                    )
                    painter.setPen(accent_pen)
                    painter.drawLine(
                        QPointF(self.CENTER + cos(angle) * 286.0,
                                self.CENTER + sin(angle) * 286.0),
                        QPointF(self.CENTER + cos(angle) * 380.0,
                                self.CENTER + sin(angle) * 380.0),
                    )

            for index, (x1, y1, x2, y2, width, base_opacity) in enumerate(self._BEAMS):
                opacity = self._beam_opacity(index, base_opacity)
                alpha = round(opacity * 255)
                pen = QPen(self._color_with_alpha(self.secondary_color, alpha))
                pen.setWidthF(width)
                painter.setPen(pen)
                painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

            # Anel de luz central e órbitas finas, sem preenchimento opaco.
            # O icosaedro será desenhado acima destas camadas.
            for radius, width, alpha in (
                (132.0, 7.0, 72),
                (139.0, 2.2, 180),
                (151.0, 0.9, 145),
                (176.0, 0.7, 100),
                (192.0, 0.8, 100),
                (211.0, 0.8, 95),
            ):
                pen = QPen(self._color_with_alpha(
                    self.secondary_color if radius < 145.0 else self.primary_color,
                    alpha,
                ), width)
                painter.setPen(pen)
                painter.drawEllipse(QPointF(self.CENTER, self.CENTER), radius, radius)

            # Anéis exteriores e textos permanecem sem preenchimento.
            outer_radius = 232.0
            outer_rect = QRectF(
                self.CENTER - outer_radius,
                self.CENTER - outer_radius,
                outer_radius * 2.0,
                outer_radius * 2.0,
            )

            # Reflexo esmeralda/âmbar periférico, sem criar um disco opaco.
            glow_pen = QPen(self._color_with_alpha(self.primary_color, 36), 8.0)
            glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(glow_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(outer_rect)

            # Contorno principal dourado, fino e luminoso.
            border_pen = QPen(self._color_with_alpha(self.primary_color, 215), 1.35)
            border_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(border_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(outer_rect)

            # Inscrição em órbita, distribuída e girada tangencialmente.
            # Mantém a leitura como textura simbólica, sem ocupar o centro.
            sigil_radius = 269.0
            sigil_font = QFont("Georgia", 10, QFont.Weight.DemiBold)
            painter.setFont(sigil_font)
            sigil_metrics = QFontMetricsF(sigil_font)
            sigil_text = self._SIGIL_TEXT
            for index, glyph in enumerate(sigil_text):
                angle = -pi / 2 + tau * index / len(sigil_text)
                x = self.CENTER + cos(angle) * sigil_radius
                y = self.CENTER + sin(angle) * sigil_radius
                painter.save()
                painter.translate(x, y)
                painter.rotate(angle * 180.0 / pi + 90.0)
                color = self._color_with_alpha(
                    self.secondary_color if index % 7 in (0, 1) else self.primary_color,
                    220 if index % 7 in (0, 1) else 175,
                )
                painter.setPen(QPen(color, 0.7))
                bounds = sigil_metrics.boundingRect(glyph)
                painter.drawText(QPointF(-bounds.width() / 2.0, bounds.height() / 3.0), glyph)
                painter.restore()

            # Trilhos segmentados, alternando comprimentos como um instrumento
            # de análise. Os arcos giram junto com o restante da interface.
            arc_rect = outer_rect.adjusted(8.0, 8.0, -8.0, -8.0)
            for index in range(16):
                start_deg = -90.0 + index * 22.5 + 2.4
                span_deg = 10.8 if index % 2 == 0 else 6.2
                arc_color = self.primary_color if index % 4 == 0 else self.secondary_color
                arc_alpha = 235 if index % 4 == 0 else 150
                arc_pen = QPen(self._color_with_alpha(arc_color, arc_alpha),
                               2.0 if index % 4 == 0 else 1.1)
                arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(arc_pen)
                painter.drawArc(arc_rect, int(start_deg * 16), int(span_deg * 16))

            # Marcas radiais em cadência dupla: 64 subdivisões e 16 marcas longas.
            for index in range(64):
                angle = -pi / 2 + tau * index / 64
                outer_tick = 229.0
                if index % 4 == 0:
                    inner_tick = 216.0
                    tick_width = 1.45
                    tick_alpha = 220
                elif index % 2 == 0:
                    inner_tick = 222.0
                    tick_width = 0.95
                    tick_alpha = 145
                else:
                    inner_tick = 225.0
                    tick_width = 0.65
                    tick_alpha = 105
                tick_pen = QPen(self._color_with_alpha(self.secondary_color, tick_alpha),
                                tick_width)
                painter.setPen(tick_pen)
                painter.drawLine(
                    QPointF(self.CENTER + cos(angle) * inner_tick,
                            self.CENTER + sin(angle) * inner_tick),
                    QPointF(self.CENTER + cos(angle) * outer_tick,
                            self.CENTER + sin(angle) * outer_tick),
                )

            # Caracteres japoneses internos: detalhe secundário de interface.
            font = QFont("Yu Gothic UI", 13, QFont.Weight.DemiBold)
            painter.setFont(font)
            metrics = QFontMetricsF(font)
            word_color = self._color_with_alpha(self.secondary_color, 245)
            painter.setPen(QPen(word_color, 1.0))

            for index, mark in enumerate(self._JP_MARKS):
                angle = -pi / 2 + tau * index / len(self._JP_MARKS)
                text_radius = 217.0
                x = self.CENTER + cos(angle) * text_radius
                y = self.CENTER + sin(angle) * text_radius
                bounds = metrics.boundingRect(mark)
                # drawText recebe a linha de base, então centralizamos visualmente.
                painter.drawText(
                    QPointF(x - bounds.width() / 2.0, y + bounds.height() / 3.0),
                    mark,
                )
        finally:
            painter.restore()

    def paint_particles(self, painter: QPainter, rect: QRectF) -> None:
        """Partículas expansivas; congelam após o impulso em listening/success."""
        if not self._begin_canvas(painter, rect):
            return
        try:
            if self.state == "error" or (self.state == "listening" and self.state_elapsed > 0.48) or (self.state == "success" and self.state_elapsed > 0.72):
                return
            painter.setPen(Qt.PenStyle.NoPen)
            particle_color = self.secondary_color
            for particle in self._PARTICLES:
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
                halo = QRadialGradient(QPointF(x, y), radius * 3.3)
                halo.setColorAt(0.0, self._color_with_alpha(particle_color, 155))
                halo.setColorAt(0.35, self._color_with_alpha(particle_color, 55))
                halo.setColorAt(1.0, self._color_with_alpha(particle_color, 0))
                painter.setBrush(QBrush(halo))
                painter.drawEllipse(QPointF(x, y), radius * 3.3, radius * 3.3)

                painter.setBrush(self._color_with_alpha(particle_color, 245))
                if particle.kind == "rect":
                    painter.drawRect(QRectF(x - radius, y - radius, radius * 2.0, radius * 2.0))
                else:
                    painter.drawEllipse(QPointF(x, y), radius, radius)
            painter.setOpacity(1.0)
        finally:
            painter.restore()

    def _paint_core_rings(self, painter: QPainter) -> None:
        """Três círculos de retângulos pequenos, com espaçamento angular uniforme."""
        for ring_index, (radius, count, rect_w, rect_h) in enumerate(self._CORE_RINGS):
            angle_offset = self.ring_angles[ring_index]
            ring_color = self.primary_color if ring_index != 1 else self.secondary_color
            alpha = 220 if self.state != "error" else 150
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._color_with_alpha(ring_color, alpha))
            for tick in range(count):
                angle = tau * tick / count + angle_offset
                x = self.CENTER + cos(angle) * radius
                y = self.CENTER + sin(angle) * radius
                painter.save()
                painter.translate(x, y)
                painter.rotate(angle * 180.0 / pi + 90.0)
                painter.drawRoundedRect(QRectF(-rect_w / 2.0, -rect_h / 2.0, rect_w, rect_h), 0.7, 0.7)
                painter.restore()

    def paint_core(self, painter: QPainter, rect: QRectF) -> None:
        """Desenha os anéis do núcleo e o pulso de energia com a cor do estado."""
        if not self._begin_canvas(painter, rect):
            return
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Anéis ficam entre a malha externa e o núcleo luminoso.
            self._paint_core_rings(painter)

            if self.state == "listening":
                pulse = 0.975 + 0.035 * (1.0 - cos(self.core_phase)) / 2.0
                opacity = 0.88 + 0.12 * (1.0 - cos(self.core_phase)) / 2.0
            elif self.state == "error":
                pulse = 0.94 + 0.12 * (1.0 - cos(self.core_phase)) / 2.0
                opacity = 0.92 + 0.08 * (1.0 - cos(self.core_phase)) / 2.0
            elif self.state == "success":
                pulse = 0.96 + 0.07 * (1.0 - cos(self.core_phase)) / 2.0
                opacity = 0.90 + 0.10 * (1.0 - cos(self.core_phase)) / 2.0
            else:
                pulse = 0.95 + 0.075 * (1.0 - cos(self.core_phase))
                opacity = 0.85 + 0.15 * (1.0 - cos(self.core_phase)) / 2.0

            painter.translate(self.CENTER, self.CENTER)
            painter.scale(pulse, pulse)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setOpacity(opacity)

            # Explosão de luz dourada; gradients usam alpha para manter a janela
            # verdadeiramente translúcida e não cobrir o icosaedro.
            outer = QRadialGradient(QPointF(0, 0), 190.0)
            outer.setColorAt(0.0, self._color_with_alpha(self.core_color, 90))
            outer.setColorAt(0.18, self._color_with_alpha(self.secondary_color, 62))
            outer.setColorAt(0.48, self._color_with_alpha(self.primary_color, 34))
            outer.setColorAt(0.78, self._color_with_alpha(self.primary_color, 13))
            outer.setColorAt(1.0, self._color_with_alpha(self.core_color, 0))
            painter.setBrush(QBrush(outer))
            painter.drawEllipse(QPointF(0, 0), 175.0, 175.0)

            middle = QRadialGradient(QPointF(0, 0), 126.0)
            middle.setColorAt(0.0, self._color_with_alpha(self.core_color, 42))
            middle.setColorAt(0.20, self._color_with_alpha(self.secondary_color, 46))
            middle.setColorAt(0.48, self._color_with_alpha(self.primary_color, 30))
            middle.setColorAt(1.0, self._color_with_alpha(self.core_color, 0))
            painter.setBrush(QBrush(middle))
            painter.drawEllipse(QPointF(0, 0), 110.0, 110.0)

            inner = QRadialGradient(QPointF(0, 0), 72.0)
            inner.setColorAt(0.0, self._color_with_alpha(self.core_color, 130))
            inner.setColorAt(0.28, self._color_with_alpha(self.core_color, 92))
            inner.setColorAt(0.62, self._color_with_alpha(self.secondary_color, 48))
            inner.setColorAt(1.0, self._color_with_alpha(self.primary_color, 0))
            painter.setBrush(QBrush(inner))
            painter.drawEllipse(QPointF(0, 0), 60.0, 60.0)

            center_halo = QRadialGradient(QPointF(0, 0), 42.0)
            center_halo.setColorAt(0.0, self._color_with_alpha(self.core_color, 235))
            center_halo.setColorAt(0.24, self._color_with_alpha(self.core_color, 175))
            center_halo.setColorAt(0.60, self._color_with_alpha(self.secondary_color, 82))
            center_halo.setColorAt(1.0, self._color_with_alpha(self.primary_color, 0))
            painter.setBrush(QBrush(center_halo))
            painter.drawEllipse(QPointF(0, 0), 30.0, 30.0)
            painter.setBrush(self._color_with_alpha(self.core_color, 220))
            painter.drawEllipse(QPointF(0, 0), 10.0, 10.0)
            painter.setOpacity(1.0)
        finally:
            painter.restore()

    def paint(self, painter: QPainter, rect: QRectF) -> None:
        """Desenha todos os efeitos (sem o d20)."""
        self.paint_background(painter, rect)
        self.paint_particles(painter, rect)
        self.paint_core(painter, rect)

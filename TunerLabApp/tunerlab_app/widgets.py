"""Small reusable widgets for the desktop shell.

Only what the shell needs beyond the existing pages: a magnitude plot for the
four S-parameters and a coloured status pill.  Both are dependency-free (QPainter
only), so they work in a frozen build without matplotlib.
"""
from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np
from PySide6.QtCore import (
    Property, QEasingCurve, QPointF, QPropertyAnimation, QRectF, Qt, Signal,
)
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QLabel, QSizePolicy, QWidget,
)

from tuner.ui_theme import numeric_font
from .theme import PILL_STYLES

__all__ = ['StatusPill', 'MagnitudePlot', 'TracePlot', 'AxisBar', 'ToggleSwitch']

#: Trace colours, one per S-parameter, readable on white.
TRACE_COLORS = {
    'S11': '#2f6f9f',
    'S21': '#1f7a4d',
    'S12': '#b5651d',
    'S22': '#8e44ad',
}


class StatusPill(QLabel):
    """A compact coloured state label (connected / fault / idle)."""

    def __init__(self, text: str = '—', state: str = 'idle', parent=None):
        super().__init__(text, parent)
        self.setObjectName('Pill')
        self.setFont(numeric_font(9, bold=True))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_state(state, text)

    def set_state(self, state: str, text: str | None = None) -> None:
        if text is not None:
            self.setText(text)
        style = PILL_STYLES.get(state, PILL_STYLES['idle'])
        self.setStyleSheet(f'#{self.objectName()} {{ {style} }}')


class ToggleSwitch(QAbstractButton):
    """The 34×19 switch from the approved mock-up, painted directly.

    The mock-up's control is a pill with a white knob.  A ``QCheckBox`` with a
    style-sheet image could not reproduce it: Qt draws the flat PNG into the
    34×19 indicator box without honouring the image's device pixel ratio, and on
    a scaled desktop that showed up as a clipped arc instead of a switch.  This
    widget paints the same geometry at any scale, with the mock-up's 0.2 s knob
    transition, so what the operator clicks is exactly what the design shows.
    """

    WIDTH = 34
    HEIGHT = 19
    KNOB = 15
    OFF = '#dbe4f0'
    ON_A = '#4f8ef7'
    ON_B = '#6a6ef0'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._offset = 0.0
        self._anim = QPropertyAnimation(self, b'offset', self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        # Qt calls setChecked() from C++, which bypasses a Python override; the
        # toggled signal is the one hook that fires for both mouse and program
        # changes, so the animation is driven from there.
        self.toggled.connect(self._slide)

    # -- animation -------------------------------------------------------
    def get_offset(self) -> float:
        return self._offset

    def set_offset(self, value: float) -> None:
        self._offset = max(0.0, min(1.0, float(value)))
        self.update()

    offset = Property(float, get_offset, set_offset)

    def _slide(self, checked: bool) -> None:
        target = 1.0 if checked else 0.0
        if not self.isVisible():
            # No animation before the first show; the resting frame must match.
            self.set_offset(target)
            return
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(target)
        self._anim.start()

    # -- painting --------------------------------------------------------
    def sizeHint(self):  # noqa: N802 - Qt naming
        from PySide6.QtCore import QSize
        return QSize(self.WIDTH, self.HEIGHT)

    def paintEvent(self, event):  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        rect = QRectF(0, 0, self.WIDTH, self.HEIGHT)
        radius = self.HEIGHT / 2.0
        if self._offset <= 0.001:
            painter.setBrush(QColor(self.OFF))
        else:
            gradient = QLinearGradient(0, 0, self.WIDTH, self.HEIGHT)
            # Blend the track colour in so the transition reads as a fade.
            gradient.setColorAt(0.0, QColor(self.ON_A))
            gradient.setColorAt(1.0, QColor(self.ON_B))
            painter.setBrush(QBrush(gradient))
        if not self.isEnabled():
            painter.setOpacity(0.45)
        painter.drawRoundedRect(rect, radius, radius)

        travel = self.WIDTH - self.KNOB - 4
        x = 2 + travel * self._offset
        knob = QRectF(x, 2, self.KNOB, self.KNOB)
        painter.setBrush(QColor(16, 27, 45, 34))
        painter.drawEllipse(knob.translated(0, 1))
        painter.setBrush(QColor('#ffffff'))
        painter.drawEllipse(knob)
        painter.end()


class MagnitudePlot(QWidget):
    """|S| in dB versus frequency, autoscaled to the data.

    Deliberately small: it only has to answer "is the instrument returning
    something sane", not replace the instrument's own display.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._series: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(False)

    def clear(self) -> None:
        self._series.clear()
        self.update()

    def set_series(self, series: dict[str, tuple[Sequence[float], Sequence[float]]]) -> None:
        self._series = {
            name: (np.asarray(f, float), np.asarray(v, float))
            for name, (f, v) in series.items() if len(f)
        }
        self.update()

    # -- painting --------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor('#ffffff'))

        left, right, top, bottom = 56, 12, 12, 34
        area = QRectF(left, top, max(1, self.width() - left - right),
                      max(1, self.height() - top - bottom))

        if not self._series:
            painter.setPen(QPen(QColor('#9aa5b1')))
            painter.setFont(numeric_font(10))
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, '尚无扫描数据')
            return

        all_f = np.concatenate([f for f, _ in self._series.values()])
        all_v = np.concatenate([v for _, v in self._series.values()])
        f_min, f_max = float(all_f.min()), float(all_f.max())
        if f_max <= f_min:
            f_max = f_min + 1.0
        v_hi = min(10.0, float(np.nanmax(all_v)) + 5.0)
        v_lo = max(-120.0, float(np.nanmin(all_v)) - 5.0)
        if v_hi - v_lo < 10:
            v_lo, v_hi = v_hi - 10, v_hi

        def to_point(f: float, v: float) -> QPointF:
            x = area.left() + (f - f_min) / (f_max - f_min) * area.width()
            y = area.bottom() - (v - v_lo) / (v_hi - v_lo) * area.height()
            return QPointF(x, y)

        # grid + labels
        painter.setPen(QPen(QColor('#e3e8ee'), 1))
        for step in range(5):
            y = area.top() + area.height() * step / 4
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            painter.setPen(QPen(QColor('#7f8b98')))
            value = v_hi - (v_hi - v_lo) * step / 4
            painter.drawText(QRectF(0, y - 8, left - 6, 16),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f'{value:.0f}')
            painter.setPen(QPen(QColor('#e3e8ee'), 1))
        for step in range(5):
            x = area.left() + area.width() * step / 4
            painter.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
            painter.setPen(QPen(QColor('#7f8b98')))
            value = f_min + (f_max - f_min) * step / 4
            painter.drawText(QRectF(x - 30, area.bottom() + 4, 60, 16),
                             Qt.AlignmentFlag.AlignHCenter, f'{value / 1e9:.0f}G')
            painter.setPen(QPen(QColor('#e3e8ee'), 1))

        painter.setPen(QPen(QColor('#b9c4d1')))
        painter.drawRect(area)
        painter.setPen(QPen(QColor('#7f8b98')))
        painter.drawText(QRectF(0, area.top(), left - 6, 16),
                         Qt.AlignmentFlag.AlignRight, 'dB')

        # traces
        for name, (f, v) in self._series.items():
            colour = QColor(TRACE_COLORS.get(name, '#444444'))
            painter.setPen(QPen(colour, 1.6))
            previous = None
            for fi, vi in zip(f, v):
                if not np.isfinite(vi):
                    previous = None
                    continue
                point = to_point(float(fi), float(vi))
                if previous is not None:
                    painter.drawLine(previous, point)
                previous = point

        # legend
        x = area.right() - 104
        y = area.top() + 6
        for name in self._series:
            colour = QColor(TRACE_COLORS.get(name, '#444444'))
            painter.setPen(QPen(colour, 2))
            painter.drawLine(QPointF(x, y + 7), QPointF(x + 18, y + 7))
            painter.setPen(QPen(QColor('#35516b')))
            painter.drawText(QRectF(x + 22, y, 70, 16),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             name)
            y += 16
        painter.end()


class AxisBar(QWidget):
    """The vertical position bar from the approved mock-up.

    Structure is fixed: 16 px wide, 146 px tall, #f2f6fc track, 8 px radius, with
    a bottom-anchored fill painted in the mock-up's blue→violet gradient.  The only
    motion is a 180 ms easing of the fill height, so a move reads as a move instead
    of a jump; the resting frame is pixel-identical to the design.
    """

    WIDTH = 16
    HEIGHT = 146
    RADIUS = 8.0
    TRACK = '#f2f6fc'
    TOP = '#4f8ef7'
    BOTTOM = '#7b5cf0'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self._ratio = 0.0
        self._anim = QPropertyAnimation(self, b'ratio', self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def get_ratio(self) -> float:
        return self._ratio

    def set_ratio(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self._ratio) < 1e-4:
            return
        self._anim.stop()
        self._anim.setStartValue(self._ratio)
        self._anim.setEndValue(value)
        self._anim.start()

    ratio = Property(float, get_ratio, set_ratio)

    def _apply(self, value: float) -> None:
        self._ratio = value
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, self.WIDTH, self.HEIGHT),
                            self.RADIUS, self.RADIUS)
        painter.setClipPath(path)

        painter.fillRect(QRectF(0, 0, self.WIDTH, self.HEIGHT), QColor(self.TRACK))
        height = self.HEIGHT * self._ratio
        if height > 0:
            gradient = QLinearGradient(0, self.HEIGHT - height, 0, self.HEIGHT)
            gradient.setColorAt(0.0, QColor(self.TOP))
            gradient.setColorAt(1.0, QColor(self.BOTTOM))
            painter.fillRect(QRectF(0, self.HEIGHT - height, self.WIDTH, height),
                             QBrush(gradient))
        painter.end()


class TracePlot(QWidget):
    """One S-parameter trace with its own axes, scale and data cursor.

    The instrument-style layout: each trace gets a window of its own, because
    |S21| spans 0…-80 dB while |S11| may sit at -20 dB -- sharing one axis makes
    at least one of them unreadable.  Painting is QPainter only, so the frozen
    build needs no plotting dependency.
    """

    #: Emitted with the frequency under the mouse (Hz, float) or ``None`` when
    #: the pointer leaves the plot.  The VNA page relays it to every other trace
    #: so one hover draws the same readout line on all four, like the instrument.
    cursor_moved = Signal(object)

    def __init__(self, name: str, unit: str = 'dB', parent=None):
        super().__init__(parent)
        self.name = name
        self.unit = unit
        self._f: np.ndarray | None = None
        self._v: np.ndarray | None = None
        self._cursor_freq: float | None = None
        self.setMinimumHeight(190)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(False)

    # -- data ------------------------------------------------------------
    def clear(self) -> None:
        self._f = self._v = None
        self._cursor_freq = None
        self.update()

    def has_data(self) -> bool:
        return self._f is not None and len(self._f) > 0

    def set_data(self, frequencies, values) -> None:
        f = np.asarray(frequencies, float)
        v = np.asarray(values, float)
        keep = np.isfinite(f) & np.isfinite(v)
        self._f, self._v = f[keep], v[keep]
        self.update()

    # -- interaction -----------------------------------------------------
    def set_cursor_freq(self, freq: float | None) -> None:
        """Draw the readout line at *freq* (Hz) without moving the mouse here."""
        if self._cursor_freq == freq:
            return
        self._cursor_freq = freq
        self.update()

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt override
        if not self.has_data():
            return
        area, f_min, f_max, _lo, _hi = self._frame()
        pos = event.position()
        if not area.contains(pos):
            if self._cursor_freq is not None:
                self._cursor_freq = None
                self.update()
                self.cursor_moved.emit(None)
            return
        ratio = (pos.x() - area.left()) / max(1.0, area.width())
        freq = f_min + ratio * (f_max - f_min)
        self._cursor_freq = freq
        self.update()
        self.cursor_moved.emit(freq)

    def leaveEvent(self, event):  # noqa: N802 - Qt override
        if self._cursor_freq is not None:
            self._cursor_freq = None
            self.update()
            self.cursor_moved.emit(None)

    # -- painting --------------------------------------------------------
    def _frame(self) -> tuple[QRectF, float, float, float, float]:
        left, right, top, bottom = 52, 10, 26, 26
        area = QRectF(left, top, max(1, self.width() - left - right),
                      max(1, self.height() - top - bottom))
        f_min = float(self._f.min())
        f_max = float(self._f.max())
        if f_max <= f_min:
            f_max = f_min + 1.0
        v_lo = float(np.nanmin(self._v))
        v_hi = float(np.nanmax(self._v))
        if v_hi - v_lo < 3.0:
            mid = (v_hi + v_lo) / 2
            v_lo, v_hi = mid - 1.5, mid + 1.5
        pad = (v_hi - v_lo) * 0.08
        return area, f_min, f_max, v_lo - pad, v_hi + pad

    def paintEvent(self, event):  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor('#ffffff'))
        colour = QColor(TRACE_COLORS.get(self.name, '#2f6f9f'))

        # header
        painter.setPen(QPen(QColor('#dfe5ec')))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.setPen(QPen(QColor('#eef2f6')))
        painter.drawLine(1, 24, self.width() - 2, 24)
        painter.setBrush(colour)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(8, 8, 9, 9), 2, 2)
        painter.setPen(QPen(QColor('#1b2733')))
        painter.setFont(numeric_font(10, bold=True))
        painter.drawText(QRectF(23, 4, 60, 18),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         self.name)
        painter.setPen(QPen(QColor('#7f8b98')))
        painter.setFont(numeric_font(9))
        painter.drawText(QRectF(66, 4, 80, 18),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         self.unit)

        if not self.has_data():
            painter.setPen(QPen(QColor('#9aa5b1')))
            painter.setFont(numeric_font(10))
            painter.drawText(self.rect().adjusted(0, 24, 0, 0),
                             Qt.AlignmentFlag.AlignCenter, '尚无扫描数据')
            painter.end()
            return

        area, f_min, f_max, v_lo, v_hi = self._frame()

        def to_point(f: float, v: float) -> QPointF:
            x = area.left() + (f - f_min) / (f_max - f_min) * area.width()
            y = area.bottom() - (v - v_lo) / (v_hi - v_lo) * area.height()
            return QPointF(x, y)

        # grid + labels
        painter.setFont(numeric_font(8))
        for step in range(5):
            y = area.top() + area.height() * step / 4
            painter.setPen(QPen(QColor('#eef2f6'), 1))
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            painter.setPen(QPen(QColor('#7f8b98')))
            value = v_hi - (v_hi - v_lo) * step / 4
            painter.drawText(QRectF(0, y - 8, area.left() - 6, 16),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f'{value:.0f}')
        for step in range(5):
            x = area.left() + area.width() * step / 4
            painter.setPen(QPen(QColor('#eef2f6'), 1))
            painter.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
            painter.setPen(QPen(QColor('#7f8b98')))
            value = (f_min + (f_max - f_min) * step / 4) / 1e9
            painter.drawText(QRectF(x - 34, area.bottom() + 4, 68, 16),
                             Qt.AlignmentFlag.AlignHCenter,
                             f'{value:.3f}G' if (f_max - f_min) < 5e9 else f'{value:.1f}G')
        # The header dot left a brush behind; without this the axis frame would be
        # filled with the trace colour and hide the curve.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor('#c9d3de')))
        painter.drawRect(area)

        # trace
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(colour, 1.5))
        previous = None
        for fi, vi in zip(self._f, self._v):
            point = to_point(float(fi), float(vi))
            if previous is not None:
                painter.drawLine(previous, point)
            previous = point

        # min marker (the number an RF engineer looks for first)
        index = int(np.nanargmin(self._v))
        point = to_point(float(self._f[index]), float(self._v[index]))
        painter.setBrush(QColor('#ffffff'))
        painter.setPen(QPen(colour, 1.4))
        painter.drawEllipse(point, 3.2, 3.2)
        text = f'{self._v[index]:.1f} dB @ {self._f[index] / 1e9:.3f} GHz'
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 10
        box = QRectF(min(max(area.left() + 4, point.x() - width / 2),
                         area.right() - width), area.top() + 4, width, 15)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 220))
        painter.drawRoundedRect(box, 3, 3)
        painter.setPen(QPen(colour))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

        # cursor readout -- one shared frequency, each trace prints its own level
        if self._cursor_freq is not None:
            freq = min(max(self._cursor_freq, f_min), f_max)
            index = int(np.argmin(np.abs(self._f - freq)))
            mark = to_point(float(self._f[index]), float(self._v[index]))
            painter.setPen(QPen(QColor('#9aa5b1'), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(mark.x(), area.top()),
                             QPointF(mark.x(), area.bottom()))
            painter.setBrush(colour)
            painter.setPen(QPen(QColor('#ffffff'), 1))
            painter.drawEllipse(mark, 3.5, 3.5)
            label = f'{self._f[index] / 1e9:.4f} GHz   {self._v[index]:.2f} dB'
            width = metrics.horizontalAdvance(label) + 12
            x = mark.x() + 8
            if x + width > area.right() - 2:
                x = mark.x() - width - 8
            box = QRectF(max(area.left() + 2, x), area.bottom() - 20, width, 16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#1b2733'))
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(QPen(QColor('#ffffff')))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
        painter.end()

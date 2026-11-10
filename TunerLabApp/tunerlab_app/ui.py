"""Small building blocks shared by the device pages (dashboard style).

The four device pages are deliberately assembled from the same three pieces so
they read as one instrument rather than four tools:

* :class:`KpiRow` — the summary strip at the top of a page (state, count, mode…);
* :class:`Card` — a white rounded panel with a title row;
* :func:`page_header` — title + one-line explanation.

Everything is plain Qt widgets, styled by object name from :mod:`tunerlab_app.theme`.
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import (QColor, QFont, QPainter, QPen, QBrush,
                           QPainterPath)
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout,
    QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from tuner.smith_view import SmithView
from tuner.smith_interaction import SmithViewInteraction

from .fonts import px_font, ui_font

__all__ = ['Card', 'KpiTile', 'KpiRow', 'SmithPreview', 'add_card_shadow',
           'field_grid', 'field_label', 'hint_label', 'page_header',
           'smith_control_row']


class KpiTile(QFrame):
    """One number with a label and an optional footnote."""

    def __init__(self, label: str, value: str = '—', sub: str = '', parent=None):
        super().__init__(parent)
        self.setObjectName('Kpi')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 6, 14, 7)
        layout.setSpacing(1)

        self.label = QLabel(label)
        self.label.setObjectName('KpiLabel')
        self.label.setFont(px_font(11.5, 'regular'))
        self.value = QLabel(value)
        self.value.setObjectName('KpiValue')
        self.value.setFont(px_font(21, 'demibold'))
        self.sub = QLabel(sub)
        self.sub.setObjectName('KpiSub')
        self.sub.setFont(px_font(11, 'light'))
        layout.addWidget(self.label)
        layout.addWidget(self.value)
        layout.addWidget(self.sub)

    def set_value(self, value: str, sub: str | None = None, tone: str = '') -> None:
        self.value.setText(value)
        if sub is not None:
            self.sub.setText(sub)
        self.value.setProperty('tone', tone)
        # Re-polish so the tone rule takes effect immediately.
        self.value.style().unpolish(self.value)
        self.value.style().polish(self.value)


class KpiRow(QWidget):
    """A horizontal strip of :class:`KpiTile`."""

    def __init__(self, tiles: list[KpiTile], parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.tiles: list[KpiTile] = []
        for tile in tiles:
            layout.addWidget(tile)
            self.tiles.append(tile)


class Card(QFrame):
    """A white rounded panel with a title row and a body layout."""

    def __init__(self, title: str = '', parent=None, *, spacing: int = 10,
                 margins: tuple[int, int, int, int] = (16, 12, 16, 14)):
        super().__init__(parent)
        self.setObjectName('Card')
        # QSS cannot express box-shadow; the mock-up's soft elevation comes from
        # a graphics effect instead.
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(14)
        shadow.setOffset(0, 2)
        shadow.setColor(QColor(16, 27, 45, 26))
        self.setGraphicsEffect(shadow)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*margins)
        outer.setSpacing(spacing)

        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        self.title = QLabel(title)
        self.title.setObjectName('CardTitle')
        self.title.setFont(px_font(13, 'demibold'))
        self.header.addWidget(self.title)
        self.header.addStretch()
        outer.addLayout(self.header)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(spacing)
        outer.addLayout(self.body, 1)

    def add_header_widget(self, widget: QWidget) -> None:
        self.header.addWidget(widget, 0, Qt.AlignmentFlag.AlignRight)


def add_card_shadow(widget: QWidget, blur: int = 14) -> None:
    """Give a card the mock-up's soft elevation (QSS has no box-shadow)."""
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(blur)
    shadow.setOffset(0, 2)
    shadow.setColor(QColor(16, 27, 45, 26))
    widget.setGraphicsEffect(shadow)


def field_label(text: str) -> QLabel:
    """A form label in the shell's ``FieldLabel`` style.

    ``QFormLayout.addRow('…', widget)`` creates a *plain* label, so the
    calibration pages ended up with a different label style from the device
    pages.  Building the rows by hand keeps one vocabulary: a small muted
    label on the left, the input on the right.
    """
    label = QLabel(text)
    label.setObjectName('FieldLabel')
    label.setFont(px_font(11.5, 'regular'))
    return label


def hint_label(text: str) -> QLabel:
    """A small muted explanation line."""
    label = QLabel(text)
    label.setObjectName('PageHint')
    label.setWordWrap(True)
    label.setFont(px_font(11.5, 'light'))
    return label


def field_grid(*, spacing: int = 10, column_spacing: int = 12) -> QGridLayout:
    """A grid meant for label/input pairs.

    A grid rather than a form layout, because a form row can only hold one
    field: the calibration setup page needs several numeric inputs side by
    side (travel limits, sweep parameters), and nesting a grid inside a form
    row gives the grid a single line's height — which is what crushed those
    rows to a few pixels.
    """
    grid = QGridLayout()
    grid.setHorizontalSpacing(column_spacing)
    grid.setVerticalSpacing(spacing)
    grid.setColumnStretch(1, 1)
    return grid


def page_header(title: str, hint: str = '') -> QWidget:
    """Title + explanation, sized like the mock-up's page header."""
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)
    label = QLabel(title)
    label.setObjectName('PageTitle')
    label.setFont(px_font(18, 'bold'))
    layout.addWidget(label)
    if hint:
        sub = QLabel(hint)
        sub.setObjectName('PageHint')
        sub.setWordWrap(True)
        sub.setFont(px_font(11.5, 'light'))
        layout.addWidget(sub)
    return box


class SmithPreview(QWidget, SmithViewInteraction):
    """Self-contained Smith-chart preview for the wizard's point-planning step.

    Paints the constant-|Gamma| point cloud (teal → magenta by |Gamma|) and an
    amber dashed target-VSWR circle, matching the 0.3.9 mock-up's wizard preview.
    Pure QPainter, so it needs no external chart library and works in a frozen
    build.  The target ring follows ``gamma_max`` (the ``|Γ|`` max of the plan).

    Interaction (F-24 + follow-up): an explicit **缩放 / 移动** mode toggle
    drives the mouse — in *缩放* a left-drag draws a rubber-band that zooms to
    the chosen region; in *移动* a left-drag pans.  The wheel always zooms
    about the cursor, middle/Ctrl+left always pans, double click resets.  The
    preview is read-only, so clicks never pick an impedance.

    Layers (bottom → top), per 设计规格 §4.5: faint constant-r / constant-x grid
    → unit circle + real axis → equal-|Gamma| point cloud → amber target ring
    with 12 target spots → colour legend + count badge.
    """

    #: teal → blue → violet → magenta ramp for the |Gamma| point cloud.
    _RAMP = [(0x1a, 0xa8, 0x9a), (0x2f, 0x9f, 0xb6), (0x4b, 0x7f, 0xd0),
             (0x7a, 0x68, 0xd8), (0xa8, 0x5b, 0xc4), (0xd0, 0x47, 0x9b)]

    #: Pixel inset used for the unit circle in the wizard preview.
    _INSET = 14.0

    def __init__(self, parent=None, gamma_max: float = 0.95,
                 gamma_min: float = 0.0):
        super().__init__(parent)
        self.setObjectName('SmithPreview')
        self._gmax = float(gamma_max)
        self._gmin = float(gamma_min)
        self._points = 0
        self.view = SmithView()
        self._init_smith_interaction()
        self.setMinimumSize(220, 220)
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Preferred)
        self._apply_mode_cursor()
        self.setToolTip('左键拖框放大局部 · 「缩放/移动」按钮切换模式 · '
                        '滚轮以光标为中心缩放 · 中键或 Ctrl+左键拖拽平移 · '
                        '双击复位')

    # -- public API -----------------------------------------------------
    def set_target(self, gamma_max: float, gamma_min: float = 0.0) -> None:
        """Move the amber target-VSWR ring to ``|Gamma| = gamma_max``."""
        self._gmax = float(gamma_max)
        self._gmin = float(gamma_min)
        self.update()

    def set_points(self, points: int) -> None:
        """Show the planned point count in the corner badge."""
        self._points = int(points)
        self.update()

    # -- zoom/pan (F-24) ------------------------------------------------
    def zoom_in(self) -> None:
        if self.view.zoom_step(1, self.width(), self.height(), self._INSET):
            self.update()

    def zoom_out(self) -> None:
        if self.view.zoom_step(-1, self.width(), self.height(), self._INSET):
            self.update()

    def reset_view(self) -> None:
        self.view.reset()
        self.update()

    def zoom_text(self) -> str:
        return f'×{self.view.zoom:.1f}' if self.view.zoomed else '×1.0'

    # -- event dispatch (delegated to SmithViewInteraction) -------------
    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._smith_wheel(event):
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._smith_press(event):
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._smith_move(event):
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._smith_release(event):
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self._smith_double(event):
            return
        super().mouseDoubleClickEvent(event)

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _ramp(t: float) -> QColor:
        t = max(0.0, min(1.0, t))
        ramp = SmithPreview._RAMP
        n = len(ramp) - 1
        x = t * n
        i = int(x)
        if i >= n:
            return QColor(*ramp[-1])
        f = x - i
        a, b = ramp[i], ramp[i + 1]
        return QColor(*(int(a[k] + (b[k] - a[k]) * f) for k in range(3)))

    def _geom(self):
        w = self.width()
        h = self.height()
        size = min(w, h) - 14
        return w / 2, h / 2, size / 2

    # -- painting -------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy, R = self._geom()
        tau = 6.283185307179586
        gmax = max(0.0, min(1.0, self._gmax))
        gmin = max(0.0, min(gmax, self._gmin))
        ellipse = lambda x, y, r: p.drawEllipse(QRectF(x - r, y - r, 2 * r, 2 * r))

        # Zoom/pan wraps only the *chart* layers; the legend/labels below stay
        # at a fixed, readable size.  The transform is exactly SmithView's
        # to_pixel() mapping expressed as a painter matrix.
        if self.view.zoomed:
            p.save()
            p.translate(cx + self.view.pan_x, cy + self.view.pan_y)
            p.scale(self.view.zoom, self.view.zoom)
            p.translate(-cx, -cy)

        # 1) faint constant-r / constant-x grid, clipped to the unit circle.
        clip = QPainterPath()
        clip.addEllipse(QRectF(cx - R, cy - R, 2 * R, 2 * R))
        p.save()
        p.setClipPath(clip)
        p.setPen(QPen(QColor('#d7e1ec'), 1.0))
        for r in (0.5, 1.0, 2.0, 5.0):
            rx = R / (1 + r)
            ellipse(cx + R * r / (1 + r), cy, rx)
        for x in (0.5, 1.0, 2.0):
            for sign in (1, -1):
                ry = R / abs(x)
                ellipse(cx + R, cy - R / x * sign, ry)
        p.restore()

        # 2) unit circle + real axis.
        p.setPen(QPen(QColor('#8ea3b8'), 1.4))
        ellipse(cx, cy, R)
        p.setPen(QPen(QColor('#9fb2c4'), 1.0))
        p.drawLine(cx - R, cy, cx + R, cy)

        # 3) equal-|Gamma| point cloud — denser (more dots) toward the edge.
        p.setPen(Qt.PenStyle.NoPen)
        for k in range(1, 11):
            g = k / 10.0
            rr = g * R
            p.setBrush(QBrush(self._ramp(g)))
            count = max(8, int(g * 64))
            for j in range(count):
                ang = tau * j / count
                dx = cx + rr * math.cos(ang)
                dy = cy - rr * math.sin(ang)
                p.drawEllipse(QRectF(dx - 1.4, dy - 1.4, 2.8, 2.8))

        # 4) amber target-VSWR ring + 12 target spots.
        if gmax > 0.001:
            tr = gmax * R
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor('#e08a18'), 1.8, Qt.PenStyle.DashLine))
            ellipse(cx, cy, tr)
            p.setBrush(QBrush(QColor('#e08a18')))
            p.setPen(Qt.PenStyle.NoPen)
            for j in range(12):
                ang = tau * j / 12
                dx = cx + tr * math.cos(ang)
                dy = cy - tr * math.sin(ang)
                p.drawEllipse(QRectF(dx - 1.8, dy - 1.8, 3.6, 3.6))

        if self.view.zoomed:
            p.restore()

        # 5) colour legend + count badge.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setFont(px_font(9, 'regular'))
        legend_w = min(120, int(R))
        lx = int(cx - R)
        ly = int(cy + R + 3)
        p.setPen(Qt.PenStyle.NoPen)
        for s in range(legend_w):
            p.setBrush(QBrush(self._ramp(s / legend_w)))
            p.drawRect(lx + s, ly, 1, 6)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QColor('#6b7b8d'))
        p.drawText(lx, ly + 17, '|Γ| 0')
        right = f'|Γ| {gmax:.2f}'
        p.drawText(lx + legend_w - p.fontMetrics().horizontalAdvance(right),
                   ly + 17, right)
        if self._points:
            badge = f'{self._points} pts'
            p.drawText(int(cx + R - p.fontMetrics().horizontalAdvance(badge)),
                       int(cy - R + 12), badge)
        if self.view.zoomed:
            zoom_badge = f'{self.zoom_text()} · 双击复位'
            p.setFont(px_font(9, 'regular'))
            width = p.fontMetrics().horizontalAdvance(zoom_badge) + 14
            p.setPen(Qt.PenStyle.NoPen)
            bg = QColor('#ffffff')
            bg.setAlpha(215)
            p.setBrush(QBrush(bg))
            p.drawRoundedRect(QRectF(6, 6, width, 20), 4, 4)
            p.setPen(QColor('#6b7b8d'))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawText(13, 20, zoom_badge)
        self.paint_rubber_band(p)


def smith_control_row(widget, *, with_mode: bool = True, with_zoom: bool = True,
                      with_reset: bool = True) -> QHBoxLayout:
    """Build a Smith-chart control row: mode toggle, zoom +/−, reset.

    ``widget`` must mix in
    :class:`~tuner.smith_interaction.SmithViewInteraction` (both
    :class:`SmithChart` and :class:`SmithPreview` do).  Returns the
    :class:`QHBoxLayout` so the caller can drop it above the chart.  The active
    mode button is highlighted via the ``modeactive`` style property.
    """
    row = QHBoxLayout()
    row.setSpacing(8)
    if with_mode:
        group = QButtonGroup(widget)
        group.setExclusive(True)
        btn_zoom = QPushButton('缩放')
        btn_pan = QPushButton('移动')
        for button in (btn_zoom, btn_pan):
            button.setCheckable(True)
            button.setMaximumWidth(58)
            group.addButton(button)
            row.addWidget(button)
        btn_zoom.setChecked(True)
        widget.smith_set_mode(SmithViewInteraction.MODE_ZOOM)

        def _on_zoom(on: bool) -> None:
            if on:
                widget.smith_set_mode(SmithViewInteraction.MODE_ZOOM)

        def _on_pan(on: bool) -> None:
            if on:
                widget.smith_set_mode(SmithViewInteraction.MODE_PAN)

        btn_zoom.toggled.connect(_on_zoom)
        btn_pan.toggled.connect(_on_pan)

        def _sync() -> None:
            for button, active in ((btn_zoom, btn_zoom.isChecked()),
                                   (btn_pan, btn_pan.isChecked())):
                button.setProperty('modeactive', active)
                button.style().unpolish(button)
                button.style().polish(button)

        btn_zoom.toggled.connect(lambda *_: _sync())
        btn_pan.toggled.connect(lambda *_: _sync())
        _sync()
    if with_zoom:
        zoom_in = QPushButton('放大')
        zoom_out = QPushButton('缩小')
        for button in (zoom_in, zoom_out):
            button.setMaximumWidth(52)
            row.addWidget(button)
        zoom_in.clicked.connect(widget.zoom_in)
        zoom_out.clicked.connect(widget.zoom_out)
    if with_reset:
        zoom_reset = QPushButton('复位')
        zoom_reset.setMaximumWidth(52)
        zoom_reset.clicked.connect(widget.reset_view)
        row.addWidget(zoom_reset)
    row.addStretch()
    return row



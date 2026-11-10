"""Shared mouse interaction for the Smith-chart widgets (F-24 follow-up).

Adds an explicit zoom/move mode toggle and rubber-band (box) zoom so the chart
is comfortable on workshop PCs without a scroll wheel or a trackpad:

* mode ``'zoom'`` — a left-drag draws a selection rectangle and zooms to it on
  release; a plain left click (no drag) still picks a Γ (where the owning
  widget supports picking, e.g. the impedance-tuning page); the wheel zooms
  about the cursor.
* mode ``'pan'`` — a left-drag moves the chart; the wheel still zooms.
* middle-button (or Ctrl+left) always pans, regardless of mode.
* double-click resets the view.

The mixin is Qt-aware but stores no geometry itself — all magnification state
lives on ``self.view`` (a :class:`~tuner.smith_view.SmithView`), so the same
transform is shared by both chart families and stays regression-testable.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QPen


class SmithViewInteraction:
    """Mixin for :class:`PySide6.QtWidgets.QWidget` Smith-chart widgets.

    Subclasses must provide ``self.view`` (a :class:`SmithView`) and a
    ``self._INSET`` pixel inset, and should call :meth:`_init_smith_interaction`
    from their ``__init__``.  Override :meth:`_smith_pick` / :meth:`_smith_right_pick`
    to turn clicks into application actions (the light wizard preview leaves
    them as no-ops).
    """

    MODE_ZOOM = 'zoom'
    MODE_PAN = 'pan'
    _CLICK_SLOP = 4  # px: movement below this is a click, not a drag

    # -- setup ----------------------------------------------------------
    def _init_smith_interaction(self) -> None:
        self._mode = self.MODE_ZOOM
        self._rubber = None          # QRectF | None  (screen pixels)
        self._rubber_start = None    # (x, y) | None
        self._panning = False
        self._pan_last = None

    # -- mode -----------------------------------------------------------
    def smith_set_mode(self, mode: str) -> None:
        if mode not in (self.MODE_ZOOM, self.MODE_PAN):
            return
        self._mode = mode
        self._rubber = None
        self._rubber_start = None
        self._apply_mode_cursor()
        self.update()

    def smith_mode(self) -> str:
        return self._mode

    def _apply_mode_cursor(self) -> None:
        if self._mode == self.MODE_PAN:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)

    # -- subclass hooks (override to taste) -----------------------------
    def _smith_inset(self) -> float:
        return float(getattr(self, '_INSET', 36.0))

    def _smith_pick(self, gamma: complex) -> None:
        """No-drag left click in zoom mode. Default: no-op."""

    def _smith_right_pick(self, gamma: complex) -> None:
        """Right-button click. Default: no-op."""

    # -- events ---------------------------------------------------------
    def _smith_press(self, event) -> bool:
        btn = event.button()
        x = event.position().x()
        y = event.position().y()
        # Middle button, or Ctrl+left, always pans.
        if btn == Qt.MouseButton.MiddleButton or (
                btn == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._panning = True
            self._pan_last = (x, y)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return True
        if btn == Qt.MouseButton.RightButton:
            g = self.view.to_gamma(x, y, self.width(), self.height(),
                                   self._smith_inset())
            if abs(g) <= 1.0:
                self._smith_right_pick(g)
            event.accept()
            return True
        if btn == Qt.MouseButton.LeftButton:
            if self._mode == self.MODE_PAN:
                self._panning = True
                self._pan_last = (x, y)
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            else:
                self._rubber_start = (x, y)
                self._rubber = QRectF(x, y, 0.0, 0.0)
            event.accept()
            return True
        return False

    def _smith_move(self, event) -> bool:
        x = event.position().x()
        y = event.position().y()
        if self._panning and self._pan_last is not None:
            self.view.pan_by(x - self._pan_last[0], y - self._pan_last[1])
            self.view.clamp(self.width(), self.height(), self._smith_inset())
            self._pan_last = (x, y)
            self.update()
            event.accept()
            return True
        if self._rubber is not None and self._rubber_start is not None:
            x0, y0 = self._rubber_start
            self._rubber = QRectF(min(x0, x), min(y0, y),
                                  abs(x - x0), abs(y - y0))
            self.update()
            event.accept()
            return True
        return False

    def _smith_release(self, event) -> bool:
        btn = event.button()
        x = event.position().x()
        y = event.position().y()
        if self._panning and btn in (Qt.MouseButton.MiddleButton,
                                     Qt.MouseButton.LeftButton):
            self._panning = False
            self._pan_last = None
            self._apply_mode_cursor()
            event.accept()
            return True
        if (self._rubber is not None and self._rubber_start is not None
                and btn == Qt.MouseButton.LeftButton):
            x0, y0 = self._rubber_start
            moved = max(abs(x - x0), abs(y - y0))
            self._rubber = None
            self._rubber_start = None
            if moved < self._CLICK_SLOP:
                g = self.view.to_gamma(x, y, self.width(), self.height(),
                                       self._smith_inset())
                if abs(g) <= 1.0:
                    self._smith_pick(g)
            else:
                self.view.zoom_to_rect(x0, y0, x, y, self.width(),
                                       self.height(), self._smith_inset())
            self.update()
            event.accept()
            return True
        return False

    def _smith_double(self, event) -> bool:
        if event.button() == Qt.MouseButton.LeftButton and self.view.zoomed:
            self.view.reset()
            self.update()
            event.accept()
            return True
        return False

    def _smith_wheel(self, event) -> bool:
        delta = event.angleDelta().y()
        if not delta:
            return False
        pos = event.position()
        factor = 1.15 ** (1 if delta > 0 else -1)
        if self.view.zoom_at(factor, pos.x(), pos.y(), self.width(),
                             self.height(), self._smith_inset()):
            self.update()
        event.accept()
        return True

    # -- painting -------------------------------------------------------
    def paint_rubber_band(self, p) -> None:
        if self._rubber is None or self._rubber.isNull():
            return
        r = self._rubber
        pen = QPen(QColor('#1f6fb2'), 1.5)
        pen.setStyle(Qt.PenStyle.DashLine)
        p.save()
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r)
        # Small corner ticks so the selection reads clearly on a busy chart.
        tick = 4
        for cx, cy in ((r.left(), r.top()), (r.right(), r.top()),
                       (r.left(), r.bottom()), (r.right(), r.bottom())):
            p.drawLine(cx - tick, cy, cx + tick, cy)
            p.drawLine(cx, cy - tick, cx, cy + tick)
        p.restore()

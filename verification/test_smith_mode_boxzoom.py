"""Regression tests for the Smith-chart mode toggle + rubber-band zoom (0.5.5).

Run with:
    TunerLabApp/.venv/Scripts/python.exe verification/test_smith_mode_boxzoom.py

Pure-geometry checks exercise :class:`tuner.smith_view.SmithView` directly; the
interaction checks drive a real (offscreen) :class:`tuner.ui.SmithChart` with
synthetic :class:`QMouseEvent` objects to prove that a click picks a Γ, a
left-drag zooms to the box, and a drag in pan mode moves the chart.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'TunerLab'))
sys.path.insert(0, os.path.join(ROOT, 'TunerLabApp'))

from PySide6.QtCore import Qt, QEvent, QPoint, QPointF
from PySide6.QtGui import QMouseEvent, QWheelEvent

from tuner.smith_view import SmithView


# --------------------------------------------------------------------------
# 1. SmithView.zoom_to_rect geometry
# --------------------------------------------------------------------------
def test_zoom_to_rect_fits_chosen_box():
    v = SmithView()
    w, h, inset = 400.0, 360.0, 36.0
    cx, cy = w / 2, h / 2
    # Two Γ corners and their pixel positions at the current (1:1) transform.
    g1 = 0.1 + 0.2j
    g2 = 0.5 - 0.3j
    p1 = v.to_pixel(g1, w, h, inset)
    p2 = v.to_pixel(g2, w, h, inset)
    assert v.zoom_to_rect(p1[0], p1[1], p2[0], p2[1], w, h, inset)
    # The chosen Γ box is centred on the widget centre.
    centre_gamma = v.to_gamma(cx, cy, w, h, inset)
    assert abs(centre_gamma - ((g1 + g2) / 2)) < 1e-6
    # Both chosen corners are now inside the chart (within the available radius).
    for g in (g1, g2):
        x, y = v.to_pixel(g, w, h, inset)
        assert ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 <= v.radius(w, h, inset) + 1e-6
    # A zoomed-in view (smaller selection) magnifies.
    assert v.zoom > 1.0


def test_zoom_to_rect_rejects_degenerate_box():
    v = SmithView()
    w, h, inset = 400.0, 360.0, 36.0
    before = (v.zoom, v.pan_x, v.pan_y)
    assert not v.zoom_to_rect(100, 100, 102, 101, w, h, inset)
    assert (v.zoom, v.pan_x, v.pan_y) == before


# --------------------------------------------------------------------------
# 2. SmithChart interaction (offscreen, synthetic mouse events)
# --------------------------------------------------------------------------
def _mev(typ, x, y, btn, mods=Qt.KeyboardModifier.NoModifier):
    pos = QPointF(x, y)
    return QMouseEvent(typ, pos, pos, btn, btn, mods)


def _chart():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from tuner.ui import SmithChart
    chart = SmithChart()
    chart.resize(420, 360)
    return chart


def test_mode_default_is_zoom_and_toggle_works():
    chart = _chart()
    assert chart.smith_mode() == 'zoom'
    chart.smith_set_mode('pan')
    assert chart.smith_mode() == 'pan'
    chart.smith_set_mode('zoom')
    assert chart.smith_mode() == 'zoom'
    chart.deleteLater()


def test_click_in_zoom_mode_picks_gamma():
    chart = _chart()
    chart.smith_set_mode('zoom')
    cx, cy = chart.width() / 2, chart.height() / 2
    chart.mousePressEvent(_mev(QEvent.Type.MouseButtonPress, cx, cy,
                               Qt.MouseButton.LeftButton))
    chart.mouseReleaseEvent(_mev(QEvent.Type.MouseButtonRelease, cx + 1, cy,
                                 Qt.MouseButton.LeftButton))
    assert chart.picked_point is not None
    # A click at the centre picks Γ ≈ 0 (within a pixel of the origin).
    assert abs(chart.picked_point) < 0.02
    chart.deleteLater()


def test_drag_in_zoom_mode_box_zooms():
    chart = _chart()
    chart.smith_set_mode('zoom')
    chart.mousePressEvent(_mev(QEvent.Type.MouseButtonPress, 120, 120,
                               Qt.MouseButton.LeftButton))
    chart.mouseMoveEvent(_mev(QEvent.Type.MouseMove, 260, 200,
                              Qt.MouseButton.LeftButton))
    chart.mouseReleaseEvent(_mev(QEvent.Type.MouseButtonRelease, 300, 240,
                                 Qt.MouseButton.LeftButton))
    assert chart.view.zoomed, 'a left-drag in zoom mode must zoom to the box'
    chart.deleteLater()


def test_drag_in_pan_mode_moves_chart():
    chart = _chart()
    chart.smith_set_mode('pan')
    before = (chart.view.pan_x, chart.view.pan_y)
    chart.mousePressEvent(_mev(QEvent.Type.MouseButtonPress, 120, 120,
                               Qt.MouseButton.LeftButton))
    chart.mouseMoveEvent(_mev(QEvent.Type.MouseMove, 170, 150,
                              Qt.MouseButton.LeftButton))
    chart.mouseReleaseEvent(_mev(QEvent.Type.MouseButtonRelease, 170, 150,
                                 Qt.MouseButton.LeftButton))
    after = (chart.view.pan_x, chart.view.pan_y)
    assert after != before, 'a left-drag in pan mode must move the chart'
    assert abs(after[0] - before[0] - 50) < 1e-6
    assert abs(after[1] - before[1] - 30) < 1e-6
    chart.deleteLater()


def test_wheel_zooms_regardless_of_mode():
    chart = _chart()
    chart.smith_set_mode('pan')  # wheel must still zoom in pan mode
    pos = QPointF(210, 180)
    wheel = QWheelEvent(pos, pos, QPoint(0, 0), QPoint(0, 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.ScrollUpdate, False)
    chart.wheelEvent(wheel)
    assert chart.view.zoomed
    chart.deleteLater()


# --------------------------------------------------------------------------
# 3. Harness
# --------------------------------------------------------------------------
def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failures = 0
    for test in tests:
        try:
            test()
            print(f'  PASS  {test.__name__}')
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            print(f'  FAIL  {test.__name__}: {exc}')
            traceback.print_exc()
    print(f'\n{len(tests) - failures}/{len(tests)} passed')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())

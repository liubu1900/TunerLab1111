"""Regression tests for the 0.5.4 acquisition-progress and Smith zoom fixes.

Run with:
    TunerLabApp/.venv/Scripts/python.exe verification/test_progress_and_smith_zoom.py

Pure-python where possible; the Qt parts only need an offscreen QApplication.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'TunerLab'))
sys.path.insert(0, os.path.join(ROOT, 'TunerLabApp'))

import numpy as np

from tuner.smith_view import SmithView
from tuner.models import TunerConfig, Measurement
from tuner.database import CalibrationDatabase
from tuner.web import handlers
from tuner.web.state import AppState


# --------------------------------------------------------------------------
# 1. SmithView geometry
# --------------------------------------------------------------------------
def test_view_default_matches_legacy_mapping():
    v = SmithView()
    w = h = 500
    inset = 36.0
    # Legacy: radius = min(w-36, h-36)/2 ; centre = (w/2, h/2)
    r = min(w - 36, h - 36) / 2
    for g in (0j, 1 + 0j, 1j, -0.5 + 0.3j):
        x, y = v.to_pixel(g, w, h, inset)
        assert abs(x - (w / 2 + r * g.real)) < 1e-9
        assert abs(y - (h / 2 - r * g.imag)) < 1e-9
        back = v.to_gamma(x, y, w, h, inset)
        assert abs(back - g) < 1e-9


def test_view_zoom_roundtrip_and_anchor():
    v = SmithView()
    w, h, inset = 500.0, 400.0, 36.0
    anchor = (310.0, 170.0)
    before = v.to_gamma(*anchor, w, h, inset)
    assert v.zoom_at(3.0, *anchor, w, h, inset)
    after = v.to_gamma(*anchor, w, h, inset)
    assert abs(after - before) < 1e-9, 'zooming must keep the Γ under the cursor'
    assert abs(v.zoom - 3.0) < 1e-9
    # Any point still round-trips at the new zoom.
    for g in (0j, 0.4 - 0.2j, -0.8 + 0.1j):
        x, y = v.to_pixel(g, w, h, inset)
        assert abs(v.to_gamma(x, y, w, h, inset) - g) < 1e-9


def test_view_pan_and_reset_and_clamp():
    v = SmithView()
    w, h, inset = 400.0, 400.0, 36.0
    v.zoom_at(5.0, 200.0, 200.0, w, h, inset)
    v.pan_by(37.0, -12.0)
    v.clamp(w, h, inset)
    assert v.zoomed
    for g in (0j, 0.3 + 0.4j):
        x, y = v.to_pixel(g, w, h, inset)
        assert abs(v.to_gamma(x, y, w, h, inset) - g) < 1e-9
    v.reset()
    assert not v.zoomed and v.zoom == 1.0
    # Never zoom out past 1:1, never past the cap.
    assert not v.zoom_at(0.5, 200, 200, w, h, inset)
    for _ in range(100):
        v.zoom_step(1, w, h, inset)
    assert v.zoom <= v.max_zoom


# --------------------------------------------------------------------------
# 2. Authoritative progress counts + live Γ snapshot
# --------------------------------------------------------------------------
def _make_library(path, n=4):
    config = TunerConfig()
    freqs = np.array([1e9, 2e9])
    positions = np.array([[float(i), 0.1, 0.2] for i in range(n)])
    db = CalibrationDatabase(path)
    db.create(config, freqs, positions)
    return db, freqs, positions


def test_progress_counts_are_committed_points(tmp_path):
    db, freqs, positions = _make_library(tmp_path / 'p.h5')
    assert db.progress_counts() == (0, 4)
    for i in (0, 2):
        s = np.tile(np.array([[0.2 + 0.1j, 0.0], [0.9, 0.1]]), (2, 1, 1))
        db.write_point(i, positions[i], Measurement(freqs, s))
    assert db.progress_counts() == (2, 4)
    db.record_failure(1, RuntimeError('vna read'))
    assert db.progress_counts() == (2, 4), 'a failed point must not count as done'


def test_studio_task_reports_true_counts(tmp_path):
    db, freqs, positions = _make_library(tmp_path / 't.h5')
    state = AppState()
    with state.studio_lock:
        state.studio['database'] = db
        # Simulate the legacy callback: it stores a 0-based *index*.
        state.studio['calibration_progress'] = (3, 4)
        state.studio['calibration_state'] = 'loaded'
    task = handlers.studio_task(state, {})
    assert task['completed'] == 0 and task['total_points'] == 4
    assert task['progress'] == [3, 4], 'legacy field must stay untouched'
    for i in range(4):
        s = np.tile(np.array([[0.3 + 0.2j, 0.0], [0.8, 0.1]]), (2, 1, 1))
        db.write_point(i, positions[i], Measurement(freqs, s))
    task = handlers.studio_task(state, {})
    assert task['completed'] == 4 and task['total_points'] == 4
    assert task['pending'] == 0


def test_live_smith_snapshot(tmp_path):
    db, freqs, positions = _make_library(tmp_path / 's.h5')
    state = AppState()
    with state.studio_lock:
        state.studio['database'] = db
    # Empty library: no exception, empty payload.
    snap = handlers.studio_live_smith(state, {})
    assert snap['points'] == [] and snap['latest'] is None
    assert snap['total_points'] == 4 and len(snap['frequencies']) == 2

    for i in range(2):
        s = np.tile(np.array([[0.25 + 0.35j, 0.0], [0.7, 0.1]]), (2, 1, 1))
        db.write_point(i, positions[i], Measurement(freqs, s))
    snap = handlers.studio_live_smith(state, {})
    assert snap['completed'] == 2 and len(snap['points']) == 2
    assert snap['frequency_hz'] == 1e9
    assert abs(snap['points'][0]['re'] - 0.25) < 1e-9
    assert snap['latest']['index'] == 1
    # Explicit frequency selection picks the second bin.
    snap2 = handlers.studio_live_smith(state, {'frequency': 2e9})
    assert snap2['frequency_hz'] == 2e9


def test_live_smith_without_database_is_safe():
    state = AppState()
    snap = handlers.studio_live_smith(state, {})
    assert snap['points'] == [] and snap['total_points'] == 0


# --------------------------------------------------------------------------
# 3. Qt widgets expose the zoom API and rebuild
# --------------------------------------------------------------------------
def test_smith_widgets_expose_zoom_api():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from tuner.ui import SmithChart
    from tunerlab_app.ui import SmithPreview
    chart = SmithChart()
    preview = SmithPreview()
    for widget in (chart, preview):
        assert hasattr(widget, 'view')
        widget.zoom_in()
        assert widget.view.zoom > 1.0
        widget.zoom_out()
        widget.reset_view()
        assert not widget.view.zoomed
        assert widget.zoom_text() == '×1.0'
    chart.deleteLater()
    preview.deleteLater()


def main():
    import tempfile
    from pathlib import Path

    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failures = 0
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        for test in tests:
            name = test.__name__
            try:
                if 'tmp_path' in test.__code__.co_varnames:
                    test(Path(tmp) / name)
                else:
                    test()
                print(f'  PASS  {name}')
            except Exception as exc:  # noqa: BLE001
                failures += 1
                import traceback
                print(f'  FAIL  {name}: {exc}')
                traceback.print_exc()
    print(f'\n{len(tests) - failures}/{len(tests)} passed')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())

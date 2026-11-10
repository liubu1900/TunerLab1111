"""Offscreen integration check of StudioRunPage's progress + live Smith chart.

Reproduces the reported bug: a fully-committed 1152-point library used to show
"1151 / 1152" at 99% while the state pill said 已完成.  Asserts the three
readouts now agree, and that the live Smith chart receives the Γ cloud.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'TunerLab'))
sys.path.insert(0, os.path.join(ROOT, 'TunerLabApp'))

import numpy as np
from PySide6.QtWidgets import QApplication

from tuner.models import TunerConfig, Measurement
from tuner.database import CalibrationDatabase
from tuner.web.state import AppState
from tunerlab_app.pages_studio import StudioRunPage


def main():
    import tempfile
    app = QApplication.instance() or QApplication([])

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = os.path.join(tmp, 'page.h5')
        n = 12  # small stand-in for 1152; the off-by-one is identical
        freqs = np.array([1e9, 2e9])
        positions = np.array([[float(i) * 0.5, 0.1, 0.2] for i in range(n)])
        db = CalibrationDatabase(path)
        db.create(TunerConfig(), freqs, positions)

        state = AppState()
        with state.studio_lock:
            state.studio['database'] = db
            state.studio['calibration_state'] = 'loaded'
            # The legacy callback stores the 0-based index of the last point.
            state.studio['calibration_progress'] = (n - 1, n)

        page = StudioRunPage(state)

        # --- case A: only some points committed -------------------------
        for i in range(5):
            s = np.tile(np.array([[0.2 + 0.1j, 0.0], [0.8, 0.1]]), (2, 1, 1))
            db.write_point(i, positions[i], Measurement(freqs, s))
        page._smith_signature = None
        page.refresh()
        assert page.counter.text() == '5 / 12', page.counter.text()
        assert page.progress.value() == 41, page.progress.value()
        assert page.state_pill.text() not in ('已完成',), page.state_pill.text()
        print('  PASS  partial run shows 5 / 12, 41%, not 已完成')

        # --- case B: every point committed (the reported bug) ----------
        for i in range(5, n):
            s = np.tile(np.array([[0.3 + 0.2j, 0.0], [0.8, 0.1]]), (2, 1, 1))
            db.write_point(i, positions[i], Measurement(freqs, s))
        page._smith_signature = None
        page.refresh()
        assert page.counter.text() == f'{n} / {n}', page.counter.text()
        assert page.progress.value() == 100, page.progress.value()
        assert page.state_pill.text() == '已完成', page.state_pill.text()
        print(f'  PASS  finished run shows {n} / {n}, 100%, 已完成')

        # --- live Smith chart ------------------------------------------
        assert len(page.live_smith.points) == n, len(page.live_smith.points)
        assert page.live_smith.selected is not None
        assert page.smith_frequency.count() == 2
        assert '滚轮缩放' in page.smith_caption.text()
        print('  PASS  live Smith chart has 12 Γ points, latest highlighted, '
              'frequency picker populated')

        # --- zoom buttons are wired and reversible ---------------------
        page.smith_zoom_in.click()
        assert page.live_smith.view.zoom > 1.0
        page.smith_zoom_reset.click()
        assert not page.live_smith.view.zoomed
        print('  PASS  live Smith zoom buttons work')

        # --- a failed point must not be reported as 已完成 -------------
        db2n = 4
        db2 = CalibrationDatabase(os.path.join(tmp, 'failed.h5'))
        db2.create(TunerConfig(), freqs, positions[:db2n])
        for i in range(db2n - 1):
            s = np.tile(np.array([[0.3 + 0.2j, 0.0], [0.8, 0.1]]), (2, 1, 1))
            db2.write_point(i, positions[i], Measurement(freqs, s))
        db2.record_failure(db2n - 1, RuntimeError('motion timeout'))
        state2 = AppState()
        with state2.studio_lock:
            state2.studio['database'] = db2
            state2.studio['calibration_state'] = 'completed_with_errors'
            state2.studio['calibration_progress'] = (db2n - 1, db2n)
        page2 = StudioRunPage(state2)
        page2._smith_signature = None
        page2.refresh()
        assert page2.counter.text() == '3 / 4', page2.counter.text()
        assert page2.state_pill.text() == '部分完成（有失败点）'
        print('  PASS  failed point shows 3 / 4 and 部分完成（有失败点）')

        page.close()
        page2.close()
    print('\n4/4 integration checks passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Render the acquisition-run page with a partly-measured library to PNG.

Shows the new live Smith chart, the zoom controls and the corrected progress
readout.  Offscreen only — no bench traffic.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT / 'TunerLabApp', ROOT / 'TunerLab'):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import numpy as np
from PySide6.QtWidgets import QApplication, QMainWindow

OUT = ROOT / 'verification' / 'out' / 'ui'


def install_font() -> str:
    from PySide6.QtGui import QFontDatabase
    for path in (Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts' / 'NotoSansSC-VF.ttf',
                 Path(r'C:\Windows\Fonts\NotoSansSC-VF.ttf'),
                 Path(r'C:\Windows\Fonts\msyh.ttc')):
        if path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(path))
            families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
            if families:
                return families[0]
    return ''


def main() -> int:
    from tuner.models import TunerConfig, Measurement
    from tuner.database import CalibrationDatabase
    from tuner.ui_theme import STYLESHEET, apply_theme
    from tuner.web.state import AppState
    from tunerlab_app.fonts import px_font
    from tunerlab_app.pages_studio import StudioRunPage
    from tunerlab_app.theme import SHELL_QSS

    app = QApplication(sys.argv)
    family = install_font()
    apply_theme(app)
    app.setStyleSheet(STYLESHEET + SHELL_QSS)
    from PySide6.QtGui import QFont
    app.setFont(QFont(family, 13) if family else px_font(13, 'regular'))

    import tempfile
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        freqs = np.array([1e9, 2e9, 3e9])
        n = 240
        positions = np.array([[float(i % 20) * 0.4, 0.1 + 0.02 * (i // 20), 0.2]
                              for i in range(n)])
        db = CalibrationDatabase(os.path.join(tmp, 'capture.h5'))
        db.create(TunerConfig(), freqs, positions)
        # 168 of 240 points committed, with a realistic swept Γ cloud.
        for i in range(168):
            g = 0.15 + 0.7 * (i / n) * np.exp(1j * (i * 0.21))
            s = np.tile(np.array([[g, 0.0], [0.8, 0.1]]), (3, 1, 1))
            db.write_point(i, positions[i], Measurement(freqs, s))

        state = AppState()
        with state.studio_lock:
            state.studio['database'] = db
            state.studio['calibration_state'] = 'running'
            state.studio['calibration_progress'] = (167, n)

        window = QMainWindow()
        window.setWindowTitle('TunerLab · 采集运行（实时 Smith 图）')
        page = StudioRunPage(state)
        window.setCentralWidget(page)
        window.resize(1400, 900)
        window.show()
        app.processEvents()
        time.sleep(0.5)
        app.processEvents()
        page._smith_signature = None
        page.refresh()
        app.processEvents()
        time.sleep(0.3)
        app.processEvents()

        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / '10-采集运行-实时Smith图.png'
        window.grab().save(str(path))
        print(f'wrote {path}')
        print(f'counter={page.counter.text()} progress={page.progress.value()}% '
              f'state={page.state_pill.text()} '
              f'smith_points={len(page.live_smith.points)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

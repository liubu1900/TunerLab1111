"""Capture the device pages for a side-by-side check against scheme 5.

Run from the project root:

    .venv/Scripts/python.exe diagnostics/capture_pages.py

Writes ``design/shots/app-*.png``.  The connection badges are filled in by hand
so the two-link state is visible without a controller on the bench; nothing here
opens a socket.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'windows')

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ORIGINAL = ROOT.parent / 'TunerLab'
if ORIGINAL.is_dir() and str(ORIGINAL) not in sys.path:
    sys.path.insert(0, str(ORIGINAL))

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tuner.ui_theme import STYLESHEET, apply_theme  # noqa: E402

from tunerlab_app.fonts import px_font  # noqa: E402
from tunerlab_app.shell import MainWindow  # noqa: E402
from tunerlab_app.theme import SHELL_QSS  # noqa: E402


def pump(app: QApplication, seconds: float = 0.4) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    app.setStyleSheet(STYLESHEET + SHELL_QSS)
    app.setFont(px_font(13, 'regular'))

    window = MainWindow()
    window.resize(1340, 880)
    window.show()
    pump(app, 0.8)

    out = ROOT / 'design' / 'shots'
    out.mkdir(parents=True, exist_ok=True)

    vna_page = window.pages['vna']
    vna_page.timer.stop()

    for key, name in (('hw-comm', 'app-comm'),
                      ('hw-motion', 'app-motion'),
                      ('vna', 'app-vna')):
        window.go(key)
        pump(app, 0.6)
        window.grab().save(str(out / f'{name}.png'))

    # Two links, two badges: show the connected state without touching hardware.
    window.ctrl_badge.setText('● 控制器 192.168.0.11')
    window.ctrl_badge.setStyleSheet('color:#1f7a4d;')
    window._on_vna_state(True, '192.168.0.50', 'Keysight,E5071C,MY12345678,A.09.90')
    window.show_status('已连接 192.168.0.11 · 固件 2.9600')
    window.go('hw-comm')
    pump(app, 0.4)
    window.grab().save(str(out / 'app-comm-connected.png'))

    # Four traces with a shared cursor at 2 GHz.
    window.go('vna')
    pump(app, 0.4)
    f = np.linspace(0.4e9, 20e9, 900)
    for index, name in enumerate(('S11', 'S21', 'S12', 'S22')):
        values = (-18 - 6 * np.sin(f / 3.4e9 + index)
                  - 34 * np.exp(-((f - 16.5e9) / 0.45e9) ** 2)
                  + 2.0 * np.sin(f / 0.11e9 + index))
        vna_page.traces[name].set_data(f, values)
    vna_page._on_trace_cursor(2.0e9)
    pump(app, 0.5)
    window.grab().save(str(out / 'app-vna-cursor.png'))

    window.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

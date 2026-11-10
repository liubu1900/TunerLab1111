"""End-to-end desktop check: one active calibration, every page follows.

Runs the real :class:`MainWindow` offscreen against two tiny synthetic
libraries (5 GHz / 8 GHz, different point counts).  It exercises the two paths
an operator actually uses:

* activate an old library from the Calibration Browser context, and
* change the library from another face (the browser console / acquisition),
  then navigate — the fingerprint reconciliation must catch it.

Exit code 0 means every dependent page re-based itself on the active library.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'TunerLab'))
sys.path.insert(0, str(ROOT / 'TunerLabApp'))

import numpy as np
from PySide6.QtWidgets import QApplication

from tuner.database import CalibrationDatabase
from tuner.models import Measurement, TunerConfig
from tuner.web import handlers
from tunerlab_app.shell import MainWindow


def make_library(folder: Path, freq: float, name: str, x: int, seed: int) -> Path:
    rng = np.random.default_rng(seed)
    config = TunerConfig()
    xv = np.linspace(config.x_limits[0], config.x_limits[1], x)
    zv = np.linspace(config.z_limits[0], config.z_limits[1], 2)
    positions = config.grid(xv, zv)
    freqs = np.array([freq], float)
    db = CalibrationDatabase(folder / name)
    db.create(config, freqs, positions)
    for index, position in enumerate(positions):
        gamma = (0.1 + 0.7 * index / max(1, len(positions) - 1)) * np.exp(
            1j * rng.uniform(-np.pi, np.pi))
        s = np.zeros((1, 2, 2), complex)
        s[:, 0, 0] = gamma
        s[:, 1, 0] = 0.05
        s[:, 0, 1] = 0.05
        db.write_point(index, position.copy(), Measurement(freqs, s, True))
    return db.path


def main() -> int:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(smoke_test=True)
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = '') -> None:
        checks.append((name, bool(ok), detail))

    smith = window.pages['studio-smith']
    tuning = window.pages['studio-tune']
    validation = window.pages['studio-validation']
    pattern = window.pages['studio-pattern']
    dbpage = window.pages['studio-db']

    # The HDF5 + SQLite sidecar keeps a handle open on Windows; let the OS
    # clean the folder later instead of failing the whole verification run.
    with tempfile.TemporaryDirectory(prefix='tunerlab-linkage-',
                                     ignore_cleanup_errors=True) as tmp:
        folder = Path(tmp)
        lib_a = make_library(folder, 5e9, 'old-5ghz.h5', 3, 0)
        lib_b = make_library(folder, 8e9, 'old-8ghz.h5', 4, 5)

        check('启动无活动库', '○ 校准库 无' in window.cal_badge.text(),
              window.cal_badge.text())

        # --- path 1: the Calibration Browser activates an old library ------
        ok = window.state.library.activate(lib_a)
        app.processEvents()
        check('激活旧库成功', ok, window.state.library.last_error)
        check('活动库徽标 = 旧库A', lib_a.name in window.cal_badge.text(),
              window.cal_badge.text())
        check('Smith 频率对齐 5 GHz',
              abs(smith.frequency.value() - 5.0) < 1e-6,
              f'{smith.frequency.value():.4f} GHz')
        count_a = len(smith.chart.points)
        check('Smith 画出库A标定点', count_a > 0, f'{count_a} 点')
        check('校准库页显示库A', str(lib_a) in dbpage.summary.text(),
              dbpage.summary.text().splitlines()[0] if dbpage.summary.text() else '')

        # Stale conclusions from A must not survive the switch to B.
        tuning.result.setPlainText('库A候选')
        validation.report_view.setPlainText('{"library": "A"}')
        pattern.table.setRowCount(2)

        # --- path 2: another face changes the library, navigation catches it
        handlers.studio_open_db(window.state, {'path': str(lib_b)})
        check('切换后上下文仍指向A（尚未导航）',
              window.state.library.path == str(lib_a),
              window.state.library.path)
        window.go('studio-tune')
        app.processEvents()

        check('导航触发指纹对账', window.state.library.path == str(lib_b),
              window.state.library.path)
        check('活动库徽标 = 旧库B', lib_b.name in window.cal_badge.text(),
              window.cal_badge.text())
        check('Smith 频率对齐 8 GHz',
              abs(smith.frequency.value() - 8.0) < 1e-6,
              f'{smith.frequency.value():.4f} GHz')
        check('Smith 点集换成库B', len(smith.chart.points) > count_a,
              f'{count_a} -> {len(smith.chart.points)} 点')
        check('调谐页频率对齐 8 GHz',
              abs(tuning.frequency.value() - 8.0) < 1e-6,
              f'{tuning.frequency.value():.4f} GHz')
        check('旧候选已作废', tuning.result.toPlainText() == '',
              tuning.result.toPlainText())
        check('旧验证报告已作废', validation.report_view.toPlainText() == '',
              validation.report_view.toPlainText())
        check('旧图案对照表已清空', pattern.table.rowCount() == 0,
              f'{pattern.table.rowCount()} 行')
        check('验证页频率对齐 8 GHz',
              abs(validation.repeat_freq.value() - 8.0) < 1e-6,
              f'{validation.repeat_freq.value():.4f} GHz')
        check('校准库页显示库B', str(lib_b) in dbpage.summary.text(),
              dbpage.summary.text().splitlines()[0] if dbpage.summary.text() else '')

    window.close()
    for name, ok, detail in checks:
        print(f'{"PASS" if ok else "FAIL"}  {name}' + (f'  [{detail}]' if detail else ''))
    failed = [name for name, ok, _ in checks if not ok]
    print(f'\n{len(checks) - len(failed)}/{len(checks)} 通过')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())

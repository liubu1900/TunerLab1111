"""Geometry regression: the impedance-tuning results area must stay readable.

Runs the real :class:`MainWindow` offscreen at several window widths and
asserts the three result blocks — candidate table, process log, and the
target-vs-measured card — never overlap and each keeps a usable height.

The old layout stacked all three vertically and gave the log ``stretch=1``;
on a short window the log swallowed the space and squeezed the deviation card
down to its header, which looked like overlapping text.  The fix splits them
into a left column (table + log) and a full-height right column (the card).

Run with the project venv:
    python verification/verify_tuning_layout.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'TunerLab'))
sys.path.insert(0, str(ROOT / 'TunerLabApp'))

from PySide6.QtCore import QPoint, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from tunerlab_app.shell import MainWindow

CHECKS: list[tuple[str, bool, str]] = []
OUT = Path(__file__).resolve().parent / 'out'
OUT.mkdir(exist_ok=True)


def check(label: str, ok: bool, detail: str = '') -> None:
    CHECKS.append((label, bool(ok), detail))
    print(('PASS  ' if ok else 'FAIL  ') + label
          + (('  -> ' + detail) if detail else ''), flush=True)


def global_rect(widget):
    """The widget's rect mapped to screen coords — comparable across columns."""
    top_left = widget.mapToGlobal(QPoint(0, 0))
    rect = widget.rect()
    return (top_left.x(), top_left.y(), rect.width(), rect.height())


def overlaps(a, b) -> bool:
    """True when two widgets share screen area (window must be shown)."""
    ax, ay, aw, ah = global_rect(a)
    bx, by, bw, bh = global_rect(b)
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def main() -> int:
    app = QApplication.instance() or QApplication([])
    # PySide6 6.9 no longer ships a font dir; offscreen grabs render CJK as
    # tofu boxes otherwise.  Register the installed Noto font AFTER the app
    # exists (calling QFontDatabase before QApplication segfaults on 6.9).
    for font_path in ("C:/Windows/Fonts/NotoSansSC-VF.ttf",
                      "C:/Windows/Fonts/NotoSansSC-Regular.otf"):
        if Path(font_path).exists():
            QFontDatabase.addApplicationFont(font_path)
    window = MainWindow(smoke_test=True)
    # Smoke mode builds pages but leaves the window hidden; a hidden widget's
    # geometry is never laid out and isVisible() is False, which made an early
    # version of this check pass vacuously.  Show it for real offscreen.
    window.show()
    window.go('studio-tune')
    app.processEvents()
    page = window.pages['studio-tune']

    # Populate the widgets so they request their real content size.
    page.table.setRowCount(3)
    from PySide6.QtWidgets import QTableWidgetItem
    for row in range(3):
        for col in range(6):
            page.table.setItem(row, col, QTableWidgetItem(f'r{row}c{col}'))
    page.result.setPlainText('插值结果\n  位置 237.5000 | 6.0000 | 0.0000\n'
                             '  预测 Γ -0.5170+0.1321j\n  |ΔΓ| 0.158927')
    page.deviation.table.setRowCount(6)
    for row in range(6):
        for col in range(3):
            page.deviation.table.setItem(row, col, QTableWidgetItem('0.5000+j0.0000'))
    page.deviation.verdict.setText('达标 · |ΔΓ| 0.020000 · 容差 0.08')

    # A short window is the regression case; a wide one proves the columns
    # split horizontally instead of piling up.
    for width, height in ((1140, 720), (1280, 800), (1920, 1080)):
        window.resize(width, height)
        app.processEvents()
        page.layout().activate()
        app.processEvents()

        table, result, card = page.table, page.result, page.deviation
        tx, ty, tw, th = global_rect(table)
        rx, ry, rw, rh = global_rect(result)
        cx, cy, cw, ch = global_rect(card)
        check(f'{width}x{height}: 窗口已显示且各块可见',
              window.isVisible() and table.isVisible() and card.isVisible(),
              f'window={window.isVisible()} table={table.isVisible()}')
        check(f'{width}x{height}: 候选表与日志不重叠', not overlaps(table, result),
              f'table.bottom={ty + th} result.top={ry}')
        check(f'{width}x{height}: 左侧两块与右侧对照卡不重叠',
              not overlaps(table, card) and not overlaps(result, card),
              f'table.right={tx + tw} card.left={cx}')
        check(f'{width}x{height}: 左右并列(对照卡在右侧)',
              cx >= tx + tw - 2,
              f'table.right={tx + tw} card.left={cx}')
        check(f'{width}x{height}: 候选表高度可读(>=90px)', th >= 90,
              f'{th}px')
        check(f'{width}x{height}: 日志高度可读(>=70px)', rh >= 70,
              f'{rh}px')
        check(f'{width}x{height}: 对照卡高度可读(>=200px)', ch >= 200,
              f'{ch}px')
        shot = OUT / f'tuning_layout_{width}x{height}.png'
        page.grab().save(str(shot))
        print(f'      截图 -> {shot.name}', flush=True)

    window.close()
    QTimer.singleShot(0, app.quit)
    app.exec()
    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f'\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())

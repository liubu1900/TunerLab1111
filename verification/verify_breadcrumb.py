"""Regression: the top title/breadcrumb must follow page navigation.

Two ways to change page exist in the shell:

* ``go(key)`` — used by in-page links and startup.  This one has always
  updated the header.
* clicking the navigation tree — fires ``QTreeWidget.currentItemChanged`` ->
  ``_on_nav_changed``.  Before the fix this only swapped the stacked widget,
  leaving the header stuck on the page that was active at startup (e.g. the
  header kept showing "轴移动控制 / 系统" after clicking "阻抗调谐与验证").

Both paths must now call ``_apply_nav_meta``.  This script drives the tree
path with ``setCurrentItem`` and asserts the header tracks every page.

Run with the project venv:
    python verification/verify_breadcrumb.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'TunerLab'))
sys.path.insert(0, str(ROOT / 'TunerLabApp'))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from tunerlab_app.shell import MainWindow, NAVIGATION

CHECKS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = '') -> None:
    CHECKS.append((label, bool(ok), detail))
    print(('PASS  ' if ok else 'FAIL  ') + label
          + (('  -> ' + detail) if detail else ''), flush=True)


def header(window) -> tuple[str, str]:
    return window.top_title.text(), window.top_crumb.text()


def main() -> int:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(smoke_test=True)
    window.show()
    app.processEvents()

    flat = [(key, group, label)
            for group, entries in NAVIGATION for key, label, _ in entries]
    check(f'导航项齐全 ({len(flat)} 页)', len(flat) >= 12, str(len(flat)))

    # --- 1) tree-click path: setCurrentItem fires _on_nav_changed only ----
    tree_ok = True
    for key, group, label in flat:
        item = window.items[key]
        window.nav.setCurrentItem(item)          # does NOT call go()
        app.processEvents()
        title, crumb = header(window)
        ok = (title == label and crumb == group)
        tree_ok &= ok
        if not ok:
            check(f'点树导航 → {label}', False,
                  f'title={title!r}/{label!r} crumb={crumb!r}/{group!r}')
    check(f'点树导航全部 {len(flat)} 页顶栏跟随（标题+分组）', tree_ok)

    # --- 2) the exact bug scenario: start on one page, click another ------
    window.go('hw-motion')
    app.processEvents()
    check('起点 hw-motion 顶栏=轴移动控制/系统',
          header(window) == ('轴移动控制', '系统'), str(header(window)))
    window.nav.setCurrentItem(window.items['studio-tune'])  # mouse-click path
    app.processEvents()
    check('点树切到阻抗调谐后顶栏=阻抗调谐与验证/验证与追溯（原 bug 点）',
          header(window) == ('阻抗调谐与验证', '验证与追溯'), str(header(window)))
    check('点树切页确实换了 stack 当前页',
          window.stack.currentWidget() is window.views['studio-tune'])

    # --- 3) go() path must still work (in-page links / startup) -----------
    go_ok = True
    for key, group, label in flat:
        window.go(key)
        app.processEvents()
        title, crumb = header(window)
        ok = (title == label and crumb == group)
        go_ok &= ok
        if not ok:
            check(f'go() → {label}', False,
                  f'title={title!r}/{label!r} crumb={crumb!r}/{group!r}')
    check(f'go() 全部 {len(flat)} 页顶栏跟随（无回归）', go_ok)

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print(f'\n{passed}/{total} checks passed')
    window.close()
    QTimer.singleShot(0, app.quit)
    return 0 if passed == total else 1


if __name__ == '__main__':
    sys.exit(main())

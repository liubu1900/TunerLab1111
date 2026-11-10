"""Layout checker for the TunerLab desktop shell.

Screenshots showed the setup page drawing its form rows *on top of each
other* — the labels, the inputs and the grid rows all overlapped.  Eyeballing
the PNG does not tell you *why*, so this tool measures the real geometry.

For every page it reports three classes of defect:

``collapsed``
    A widget whose height is smaller than its own ``minimumSizeHint()`` —
    the layout could not give it the room it needs, so its content spills
    over its neighbours.  This is what turns a two-row grid into one
    illegible line.

``overlap``
    Two sibling widgets in the same layout whose rectangles intersect.  Their
    contents are drawn on top of each other.

``clipped``
    Text that does not fit its widget: a label whose natural width exceeds the
    width it was given, or a combo box whose current item is wider than the
    box.

Run it directly to print the report and to write offscreen screenshots::

    python -m tunerlab_app.layout_check                # report only
    python -m tunerlab_app.layout_check --shots out/  # also render PNGs
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QHeaderView, QLabel, QLayout,
    QLineEdit, QWidget,
)

__all__ = ['LayoutProblem', 'check_page', 'check_window', 'main']

#: A text deficit below this many pixels is rounding noise, not a defect.
CLIP_MARGIN = 8


class LayoutProblem:
    """One measured layout defect."""

    __slots__ = ('kind', 'page', 'widget', 'detail')

    def __init__(self, kind: str, page: str, widget: str, detail: str):
        self.kind = kind
        self.page = page
        self.widget = widget
        self.detail = detail

    def __str__(self) -> str:
        return f'[{self.kind:<8}] {self.page} · {self.widget}: {self.detail}'


def _name(widget: QWidget) -> str:
    """A readable identifier for a widget."""
    for attribute in ('objectName', 'text', 'title', 'placeholderText'):
        value = getattr(widget, attribute, None)
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001 - a broken getter is not a defect
                value = None
        if value:
            text = str(value).strip().replace('\n', ' ')
            if text:
                return f'{type(widget).__name__}({text[:28]})'
    return type(widget).__name__


def _visible(widget: QWidget) -> bool:
    if not widget.isVisible():
        return False
    # A widget inside a hidden tab still has a geometry but is never painted.
    parent = widget.parentWidget()
    while parent is not None:
        if not parent.isVisible():
            return False
        parent = parent.parentWidget()
    return True


def _rects_overlap(a: QRect, b: QRect) -> bool:
    """Do two rectangles share any area?"""
    return a.intersects(b) and (a & b).width() > 1 and (a & b).height() > 1


def _check_collapsed(page_name: str, widget: QWidget,
                     problems: list[LayoutProblem]) -> None:
    """A widget squeezed below its own minimum height spills over its neighbours."""
    if not _visible(widget):
        return
    if isinstance(widget, QHeaderView):
        # A table header legitimately renders shorter than the section size its
        # minimumSizeHint reports; that is not a collapsed widget.
        return
    if isinstance(widget.parentWidget(), QAbstractSpinBox):
        # The spin box's internal line edit is sized by the spin box itself.
        return
    if widget.minimumHeight() > 0 and widget.minimumHeight() == widget.maximumHeight():
        # A fixed-size widget (the axis status dot is 9x9) cannot be crushed;
        # its minimumSizeHint just reports the font height.
        return
    hint = widget.minimumSizeHint()
    if hint.height() <= 0:
        return
    height = widget.height()
    if height + 1 < hint.height():
        problems.append(LayoutProblem(
            'collapsed', page_name, _name(widget),
            f'高度 {height}px < 最小需要 {hint.height()}px（差 {hint.height() - height}px）'))


def _check_overlaps(page_name: str, container: QWidget,
                    problems: list[LayoutProblem]) -> None:
    """Two siblings in one layout must not share pixels."""
    layout = container.layout()
    if layout is None:
        return
    entries = []
    for index in range(layout.count()):
        item = layout.itemAt(index)
        widget = item.widget()
        if widget is None or not _visible(widget):
            continue
        # A nested layout (a grid inside a form row) is reported through its
        # own widgets, not as one opaque rectangle.
        if item.layout() is not None:
            continue
        entries.append((widget, item.geometry()))
    for i, (widget_a, rect_a) in enumerate(entries):
        for widget_b, rect_b in entries[i + 1:]:
            if _rects_overlap(rect_a, rect_b):
                problems.append(LayoutProblem(
                    'overlap', page_name,
                    f'{_name(widget_a)} × {_name(widget_b)}',
                    f'几何重叠 {rect_a.width()}×{rect_a.height()} 与 '
                    f'{rect_b.width()}×{rect_b.height()}'))


def _check_clipped_text(page_name: str, widget: QWidget,
                        problems: list[LayoutProblem]) -> None:
    """Text wider than the widget that has to show it.

    ``CLIP_MARGIN`` filters out the one- or two-pixel rounding differences
    that every QSS border produces — those are not defects an operator can
    see, and reporting them would bury the real ones.
    """
    if not _visible(widget):
        return
    if isinstance(widget.parentWidget(), QAbstractSpinBox):
        # The spin box's internal line edit is sized by the spin box itself.
        return
    if isinstance(widget, QLabel):
        text = widget.text()
        if not text or widget.wordWrap():
            return
        needed = widget.fontMetrics().horizontalAdvance(text)
        available = widget.width() - 2
        deficit = needed - available
        if deficit > CLIP_MARGIN:
            problems.append(LayoutProblem(
                'clipped', page_name, _name(widget),
                f'文字需 {needed}px，只有 {available}px（截断 {deficit}px）'))
    elif isinstance(widget, QComboBox):
        text = widget.currentText()
        if not text:
            return
        needed = widget.fontMetrics().horizontalAdvance(text)
        # Leave room for the drop-down arrow and the frame.
        available = widget.width() - 34
        deficit = needed - available
        if deficit > CLIP_MARGIN:
            problems.append(LayoutProblem(
                'clipped', page_name, _name(widget),
                f'选项「{text[:30]}」需 {needed}px，只有 {available}px'
                f'（截断 {deficit}px）'))
    elif isinstance(widget, QLineEdit):
        text = widget.text()
        if not text:
            return
        if widget.property('allowClip'):
            # A path field is expected to be longer than its box: it scrolls.
            # Marked with ``allowClip`` so the check does not cry wolf.
            return
        needed = widget.fontMetrics().horizontalAdvance(text)
        available = widget.width() - 16
        deficit = needed - available
        if deficit > CLIP_MARGIN:
            problems.append(LayoutProblem(
                'clipped', page_name, _name(widget),
                f'内容需 {needed}px，只有 {available}px（截断 {deficit}px）'))


def _walk(page_name: str, widget: QWidget,
          problems: list[LayoutProblem]) -> None:
    """Walk the tree and check every widget and every container."""
    _check_collapsed(page_name, widget, problems)
    _check_clipped_text(page_name, widget, problems)
    _check_overlaps(page_name, widget, problems)
    for child in widget.findChildren(QWidget, options=Qt.FindDirectChildrenOnly):
        _walk(page_name, child, problems)


def check_page(page_name: str, widget: QWidget) -> list[LayoutProblem]:
    """All measured defects on one page."""
    problems: list[LayoutProblem] = []
    _walk(page_name, widget, problems)
    return problems


def check_window(window, sizes=((1340, 880), (1140, 720)),
                 pages=None) -> list[LayoutProblem]:
    """Check every page of a :class:`MainWindow` at several window sizes."""
    problems: list[LayoutProblem] = []
    keys = list(pages or window.pages.keys())
    for width, height in sizes:
        window.resize(width, height)
        window.show()
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        for key in keys:
            page = window.pages.get(key)
            if page is None:
                continue
            window.go(key)
            if app is not None:
                app.processEvents()
            for problem in check_page(f'{key} @{width}×{height}', page):
                problems.append(problem)
    window.hide()
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shots', metavar='DIR', default=None,
                        help='write one offscreen PNG per page into DIR')
    parser.add_argument('--size', default=None,
                        help='a single "WIDTHxHEIGHT" instead of the two defaults')
    args = parser.parse_args(argv)

    from .shell import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(smoke_test=True)

    sizes = ((1340, 880), (1140, 720))
    if args.size:
        w, h = args.size.lower().split('x')
        sizes = ((int(w), int(h)),)

    problems = check_window(window, sizes=sizes)

    if args.shots:
        out = Path(args.shots)
        out.mkdir(parents=True, exist_ok=True)
        for key, page in window.pages.items():
            window.resize(*sizes[0])
            window.go(key)
            app.processEvents()
            page.grab().save(str(out / f'{key}.png'))
        print(f'screenshots -> {out}')

    if not problems:
        print('layout check: no problems found')
        return 0
    by_kind: dict[str, int] = {}
    for problem in problems:
        by_kind[problem.kind] = by_kind.get(problem.kind, 0) + 1
    print(f'layout check: {len(problems)} problem(s) '
          f'({", ".join(f"{k}={v}" for k, v in sorted(by_kind.items()))})')
    for problem in problems:
        print(' ', problem)
    return 1


if __name__ == '__main__':
    sys.exit(main())

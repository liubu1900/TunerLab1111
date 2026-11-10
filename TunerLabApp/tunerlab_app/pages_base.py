"""Shared base for the studio pages.

``_StudioPage`` and ``_page_title`` used to live in :mod:`pages_studio`, but
P1 added :mod:`pages_p1`, which needs the same base — and ``pages_studio``
needs a widget from ``pages_p1``.  Keeping the base here breaks that cycle:
both page modules import from here, and neither imports from the other.
"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QLabel, QMessageBox, QVBoxLayout, QWidget,
)

from tuner.ui_theme import text_font

__all__ = ['_StudioPage', '_page_title']


def _page_title(text: str, hint: str = '') -> QVBoxLayout:
    """The page heading block: a big title plus an optional explanatory line."""
    box = QVBoxLayout()
    box.setSpacing(4)
    title = QLabel(text)
    title.setObjectName('PageTitle')
    title.setFont(text_font(18, bold=True))
    box.addWidget(title)
    if hint:
        label = QLabel(hint)
        label.setObjectName('PageHint')
        label.setWordWrap(True)
        box.addWidget(label)
    return box


class _StudioPage(QWidget):
    """Shared plumbing: navigation signal, status reporting, guarded handler calls."""

    navigate = Signal(str)

    def __init__(self, app_state, parent=None):
        super().__init__(parent)
        self.state = app_state
        self.setObjectName('StudioPage')
        # Standard practice keeps one *active* calibration for the whole application.  Every
        # studio page subscribes here, so switching a library in the browser
        # refreshes the Smith chart, the pattern editor, the tuner, the
        # validation report and the audit view together — and any page added
        # later opts in simply by overriding ``on_library_changed``.
        # ``AppState`` used by the browser console has no context; the guard
        # keeps plain (non-desktop) state objects working unchanged.
        context = getattr(app_state, 'library', None)
        if context is not None and hasattr(context, 'changed'):
            context.changed.connect(self.on_library_changed)

    def on_library_changed(self, path: str) -> None:
        """React to a new active calibration library.

        Default behaviour covers pages that already implement ``refresh()``
        (system overview, acquisition run, Smith chart).  Pages whose data is
        derived from the library — candidates, verdicts, resolved patterns,
        audit rows — override this to *invalidate* the stale results first.
        """
        refresh = getattr(self, 'refresh', None)
        if callable(refresh):
            refresh()

    def report(self, message: str) -> None:
        window = self.window()
        if hasattr(window, 'show_status'):
            window.show_status(message)

    def warn(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def fail(self, exc: BaseException, title: str = '操作失败') -> None:
        self.report(f'{title}: {exc}')
        self.warn(title, f'{type(exc).__name__}: {exc}')

    def run(self, function: Callable[[], Any], on_done: Callable[[Any], None],
            title: str = '操作失败', description: str = 'studio') -> None:
        from .bg import run_in_background
        run_in_background(function, on_done=on_done,
                          on_error=lambda exc: self.fail(exc, title),
                          description=description)

    def sync_frequency_from_library(self, spin) -> bool:
        """Move the frequency field onto the loaded calibration's band.

        The studio pages are built once, but the calibration library is opened
        or replaced afterwards, and the default that ships with the page (10 GHz)
        is outside the band of the libraries this machine produces (5 GHz).  The
        search then refuses with *"frequency outside calibration band"*, which
        reads like a data problem but is really a stale field.

        A value the operator entered by hand is kept as long as it is inside the
        band -- only a value that cannot work is replaced.
        """
        studio = getattr(self.state, 'studio', None)
        if not isinstance(studio, dict):
            return False
        data = studio.get('data')
        if not data:
            return False
        try:
            frequencies = data['frequencies']
            low = float(min(frequencies))
            high = float(max(frequencies))
        except (KeyError, TypeError, ValueError, IndexError):
            return False
        current = float(spin.value()) * 1e9
        if low <= current <= high:
            return False
        target = studio.get('frequency_hz')
        try:
            target = float(target)
        except (TypeError, ValueError):
            target = low
        if not (low <= target <= high):
            target = low
        spin.setValue(target / 1e9)
        self.report(f'频率已对齐到校准库频带 {low / 1e9:.6f}–{high / 1e9:.6f} GHz')
        return True

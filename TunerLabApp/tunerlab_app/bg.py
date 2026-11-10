"""Run a blocking backend call without freezing the Qt event loop.

Every hardware call in this application blocks: opening the controller retries
the UDP discovery, a move waits for the axis to report idle, a VNA sweep waits
for ``*OPC?``.  Doing that on the GUI thread makes the window unresponsive and —
worse — makes *Stop* and *Emergency stop* unreachable exactly when they are
needed.

:func:`run_in_background` moves the call to the global thread pool and delivers
the result back on the GUI thread through queued signals.
"""
from __future__ import annotations

import traceback
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot, Qt

__all__ = ['run_in_background', 'BackgroundTask']

#: Live tasks are kept referenced until they finish; a QRunnable owned only by the
#: C++ thread pool can otherwise have its Python wrapper collected mid-flight.
_pending: set['BackgroundTask'] = set()


class _TaskSignals(QObject):
    finished = Signal(object)
    failed = Signal(object)

    def __init__(self, task, on_done, on_error):
        super().__init__()
        self.task = task
        self.on_done = on_done
        self.on_error = on_error
        # A QObject receiver with explicit queued connections guarantees GUI
        # affinity; a plain Python closure has no Qt receiver thread contract.
        self.finished.connect(self._finish, Qt.ConnectionType.QueuedConnection)
        self.failed.connect(self._fail, Qt.ConnectionType.QueuedConnection)

    def _deliver(self, callback, value):
        task = self.task
        try:
            _safe(callback, value, task.description or 'task')
        finally:
            _pending.discard(task)
            self.task = self.on_done = self.on_error = None
            self.deleteLater()

    @Slot(object)
    def _finish(self, result):
        self._deliver(self.on_done, result)

    @Slot(object)
    def _fail(self, exc):
        self._deliver(self.on_error, exc)


class BackgroundTask(QRunnable):
    """One callable executed on the thread pool."""

    def __init__(self, function: Callable[[], Any], description: str = '',
                 on_done=None, on_error=None):
        super().__init__()
        self.function = function
        self.description = description
        self.signals = _TaskSignals(self, on_done, on_error)
        # The registry below owns the lifetime explicitly.
        self.setAutoDelete(False)

    def run(self) -> None:  # noqa: D401 - QRunnable entry point
        try:
            result = self.function()
        except BaseException as exc:  # noqa: BLE001 - every failure belongs to the UI
            self.signals.failed.emit(exc)
        else:
            self.signals.finished.emit(result)


def _safe(callback: Optional[Callable[[Any], None]], value: Any,
          label: str) -> None:
    """Invoke a UI callback, tolerating a page that was destroyed meanwhile."""
    if callback is None:
        return
    try:
        callback(value)
    except RuntimeError:
        # The receiving widget was closed while the task was in flight; the task
        # result is simply dropped.
        pass
    except Exception:  # noqa: BLE001 - a callback bug must not kill the event loop
        traceback.print_exc()


def run_in_background(function: Callable[[], Any],
                      on_done: Optional[Callable[[Any], None]] = None,
                      on_error: Optional[Callable[[BaseException], None]] = None,
                      description: str = '') -> BackgroundTask:
    """Execute ``function`` off the GUI thread.

    ``on_done`` receives the return value, ``on_error`` the exception; both are
    invoked on the GUI thread.  The task object is returned so a caller can keep
    it for debugging.
    """
    task = BackgroundTask(function, description, on_done, on_error)
    _pending.add(task)
    QThreadPool.globalInstance().start(task)
    return task

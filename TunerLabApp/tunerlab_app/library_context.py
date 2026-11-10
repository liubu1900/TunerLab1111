"""One *active* calibration library, shared by every view.

Why this module exists
----------------------
A tuner workbench keeps a single **active calibration**.  The Smith
chart, the tuner, the target-pattern editor, the interpolation validation and
the report/audit view all read the same calibration file, the same frequency
grid and the same reference plane.  Choosing an old library in the Calibration
Browser therefore changes the *context of the whole application*, not just the
page that made the choice: every derived view re-reads its data, and every
conclusion produced against the previous file is discarded.

Before this module the desktop app had no such context:

* the Calibration Browser could scan / compare / export / derive libraries but
  could not make one active, so selecting an old library did nothing at all;
* ``MainWindow._refresh_calibration_views()`` refreshed **only** the Smith
  page, and that page kept its own frequency spin box at the built-in 10 GHz
  default.  A 5 GHz library was therefore queried at 10 GHz and the chart came
  back empty even though a "refresh" had run — the operator saw no linkage;
* the pattern, tuning, validation and audit pages had no refresh entry point
  at all, so they kept showing candidates, verdicts and reports from the
  library that happened to be loaded when the window was built.

The context below implements that rule in three parts:

1. :meth:`LibraryContext.activate` opens a library and **invalidates every
   conclusion derived from the previous one** (validation reports, target,
   selected point, tuning candidates — the backend already clears the latter).
2. :attr:`LibraryContext.changed` is a Qt signal broadcast to every page.  The
   :class:`~tunerlab_app.pages_base._StudioPage` base connects it once, so a
   future page opts in simply by overriding ``on_library_changed``.
3. :meth:`LibraryContext.ensure_current` reconciles the context with the
   authoritative studio state.  The browser console, a finished acquisition
   run or the "open database" menu can change the active library through the
   backend without going through ``activate``; comparing the calibration
   fingerprint catches all of those on the next navigation.

The library file itself is never modified: making a library active only sets
the shared context, exactly like loading a calibration file.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal

from tuner.web import handlers

__all__ = ['LibraryContext']


class LibraryContext(QObject):
    """The one active calibration plus the broadcast that keeps views in sync."""

    #: Emitted with the active library path (``''`` when no library is loaded).
    #: Every :class:`_StudioPage` is connected to this signal.
    changed = Signal(str)
    #: Emitted when :meth:`activate` cannot open the requested library.  The
    #: shell shows the reason in the status bar; the browser shows a dialog.
    activation_failed = Signal(str)

    def __init__(self, app_state, parent=None):
        super().__init__(parent)
        self.state = app_state
        self._path = ''
        self._fingerprint: str | None = None
        #: Human-readable reason of the last failed :meth:`activate`.
        self.last_error = ''

    # -- public state ----------------------------------------------------
    @property
    def path(self) -> str:
        """Path of the library this context last broadcast."""
        return self._path

    @property
    def fingerprint(self) -> str | None:
        """Fingerprint of the library this context last broadcast."""
        return self._fingerprint

    def _snapshot(self) -> tuple[str, str | None]:
        """Read the authoritative path/fingerprint from the studio state.

        ``state.studio`` is shared with the browser console and the calibration
        worker, so it — not this object's cache — is the source of truth.  The
        fingerprint is computed by ``handlers._load_database`` and changes on
        every library load, which is exactly what a "did the library change?"
        check needs (path alone is not enough: reloading the same file after a
        re-acquisition must also invalidate old results).
        """
        with self.state.studio_lock:
            studio = self.state.studio
            database = studio.get('database')
            path = str(database.path) if database is not None else ''
            return path, studio.get('database_fingerprint')

    # -- activation ------------------------------------------------------
    def activate(self, path) -> bool:
        """Make ``path`` the active calibration and broadcast the change.

        Returns ``True`` on success.  Any failure (a busy calibration run, a
        corrupt HDF5, a missing sidecar) is surfaced through
        :attr:`activation_failed` and :attr:`last_error`; the previously active
        library is left in place rather than half-loaded.
        """
        target = str(path)
        if not target:
            self._fail('没有指定校准库路径')
            return False
        try:
            # handlers.studio_open_db owns the busy-state gate and the actual
            # load.  Let its exception reach us so the operator sees the real
            # reason ("calibration is running" / "no such file") instead of a
            # silently stale library.
            handlers.studio_open_db(self.state, {'path': target})
        except Exception as exc:  # noqa: BLE001 - the handler raises many types
            self._fail(f'{type(exc).__name__}: {exc}')
            return False
        self.last_error = ''
        self._invalidate_derived()
        self.broadcast()
        return True

    def _invalidate_derived(self) -> None:
        """Discard conclusions that belong to the previous library.

        Standard practice binds every result to its calibration file.  ``_load_database``
        already clears candidates / last tuned / their fingerprints; the
        validation and interpolation reports, the Smith target and the selected
        point live in the same state dict and must be cleared here, otherwise
        the validation report timer would resurrect an old verdict 1.2 s after
        the operator switched libraries.
        """
        with self.state.studio_lock:
            studio = self.state.studio
            studio['validation_report'] = None
            studio['interpolation_report'] = None
            studio['last_target'] = None
            studio['last_selected'] = None

    # -- broadcast -------------------------------------------------------
    def broadcast(self) -> str:
        """Publish the current active library to every connected view."""
        path, fingerprint = self._snapshot()
        self._path, self._fingerprint = path, fingerprint
        self.changed.emit(path)
        return path

    def notify_current(self) -> str:
        """Broadcast from an external entry point (database page, menu, wizard).

        Those call ``handlers.studio_open_db`` themselves; this method only
        publishes the new state and refreshes the top-bar badge through the
        shell's ``changed`` connection.
        """
        return self.broadcast()

    def ensure_current(self) -> bool:
        """Reconcile with a library changed by another face of the app.

        The browser console, a finished acquisition run and the reference-shift
        operation all load libraries through the backend directly.  Calling
        this on page navigation broadcasts only when the fingerprint really
        changed, so ordinary navigation never triggers a needless redraw.
        Returns ``True`` when a broadcast was sent.
        """
        path, fingerprint = self._snapshot()
        if path == self._path and fingerprint == self._fingerprint:
            return False
        self.broadcast()
        return True

    # -- internals -------------------------------------------------------
    def _fail(self, message: str) -> None:
        self.last_error = message
        self.activation_failed.emit(message)

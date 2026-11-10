"""Process-level concerns: logging, crash reporting and single-instance guarding.

A hardware console that dies without a trace is worse than one that refuses to
start: the operator cannot tell whether the machine moved.  These helpers make
every session recoverable:

* a rotating log file under ``%LOCALAPPDATA%\\TunerLab\\logs`` records the Python
  side of the story;
* the operation log additionally gets a JSONL sink, so the audit trail of axis
  commands survives a crash or a power cut;
* an unhandled exception is written to the log and shown in a dialog instead of
  silently closing the window;
* a second instance raises the already-running window instead of opening a
  competing handle on the same controller DLL.
"""
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import sys
import traceback
from pathlib import Path
from typing import Optional

from . import paths

__all__ = ['setup_logging', 'install_exception_hook', 'log_path', 'SingleInstance',
           'enable_crash_log']

_LOGGER_NAME = 'tunerlab'
_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 5


def log_path() -> Path:
    return paths.log_dir() / 'tunerlab.log'


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure file + console logging once, and attach the audit-log sink."""
    paths.ensure_app_dirs()
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    if logger.handlers:  # already configured in this process
        return logger

    formatter = logging.Formatter(
        '%(asctime)s %(levelname)-7s %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S')

    file_handler = logging.handlers.RotatingFileHandler(
        log_path(), maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding='utf-8')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # The audit trail must outlive the process: without a sink the 2000-entry
    # ring buffer disappears together with the crash.
    from tuner.operation_log import operation_log
    operation_log.set_sink(paths.log_dir() / 'operations.jsonl')
    logger.info('log file: %s', log_path())
    return logger


#: Set once so the crash file stays open for the life of the process.
_crash_stream = None


def enable_crash_log() -> Path:
    """Record native crashes and Qt warnings instead of dying silently.

    A segmentation fault in the vendor DLL (or in Qt, when a widget is touched
    from a worker thread) kills the process before Python can raise anything, so
    the operator sees the window vanish and the log stays empty.  ``faulthandler``
    writes the Python stack of every thread to a file the moment the fault
    happens, which is the only evidence available afterwards.
    """
    global _crash_stream
    paths.ensure_app_dirs()
    crash_path = paths.log_dir() / 'crash.log'
    if _crash_stream is None:
        # Line-buffered and kept open: faulthandler needs a live file object.
        _crash_stream = open(crash_path, 'a', buffering=1, encoding='utf-8')
        faulthandler.enable(file=_crash_stream, all_threads=True)
        _crash_stream.write(f'\n===== {__import__("datetime").datetime.now():%Y-%m-%d %H:%M:%S} '
                            f'crash log opened =====\n')
    _install_qt_message_handler()
    return crash_path


def _install_qt_message_handler() -> None:
    """Send Qt's own warnings to the application log.

    Qt prints things such as "QObject::startTimer: Timers cannot be started from
    another thread" to stderr, which a windowed build has no console for.  Those
    messages are how a threading mistake announces itself.
    """
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:  # noqa: BLE001 - logging must never block startup
        return
    log = logging.getLogger(_LOGGER_NAME)

    def handler(mode, context, message):  # noqa: ANN001 - Qt signature
        text = f'Qt[{mode}] {message}'
        if mode in (QtMsgType.QtFatalMsg, QtMsgType.QtCriticalMsg):
            log.error(text)
        elif mode == QtMsgType.QtWarningMsg:
            log.warning(text)
        else:
            log.debug(text)

    qInstallMessageHandler(handler)


def install_exception_hook(logger: Optional[logging.Logger] = None,
                           show_dialog: bool = True) -> None:
    """Turn an unhandled exception into a log record plus a dialog."""
    log = logger or logging.getLogger(_LOGGER_NAME)

    def hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        text = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.error('unhandled exception\n%s', text)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            if QApplication.instance() is not None:
                box = QMessageBox()
                box.setIcon(QMessageBox.Icon.Critical)
                box.setWindowTitle('TunerLab 遇到未处理的错误')
                box.setText(
                    f'{exc_type.__name__}: {exc_value}\n\n'
                    '程序仍可继续操作，但建议保存当前校准库并重启。')
                box.setDetailedText(text)
                box.setInformativeText(f'完整堆栈已写入：\n{log_path()}')
                box.exec()
        except Exception:  # noqa: BLE001 - never recurse into the hook
            pass

    sys.excepthook = hook


class SingleInstance:
    """Guard that allows exactly one console per user session.

    The Zmotion DLL keeps a handle to the controller and the calibration
    database allows a single writer; two windows would fight over both.  The
    second process asks the first to come to the front and then exits.
    """

    def __init__(self, key: str = 'TunerLab.Console'):
        from PySide6.QtNetwork import QLocalServer, QLocalSocket
        self._QLocalServer = QLocalServer
        self._QLocalSocket = QLocalSocket
        self.key = key
        self.server = None
        self._window = None

    def acquire(self) -> bool:
        """``True`` when this process owns the instance; ``False`` if another did."""
        socket = self._QLocalSocket()
        socket.connectToServer(self.key)
        if socket.waitForConnected(300):
            socket.write(b'raise')
            socket.waitForBytesWritten(300)
            socket.disconnectFromServer()
            return False
        # A previous process may have died without cleaning its socket up.
        self._QLocalServer.removeServer(self.key)
        # The constructor takes a parent, not a name; the name is given to listen().
        self.server = self._QLocalServer()
        self.server.newConnection.connect(self._on_new_connection)
        return bool(self.server.listen(self.key))

    def bind_window(self, window) -> None:
        self._window = window

    def _on_new_connection(self) -> None:
        connection = self.server.nextPendingConnection()
        if connection is not None:
            connection.readyRead.connect(lambda: connection.readAll())
            connection.disconnected.connect(connection.deleteLater)
        if self._window is not None:
            self._window.showNormal()
            self._window.raise_()
            self._window.activateWindow()

    def release(self) -> None:
        if self.server is not None:
            self.server.close()
            self._QLocalServer.removeServer(self.key)
            self.server = None

"""Filesystem locations that work the same running from source and frozen.

This module is Qt-free so the web console, the CLI and the desktop shell all
resolve the same directories.  The desktop shell adds its own
:func:`app_root` on top (the frozen application's resource folder), which is
why the two modules exist side by side.

The original code wrote calibration files to ``Path.cwd()/calibrations``.  That is
fine in a development shell, but an installed application usually starts with a
working directory of ``C:\\Windows\\System32`` (shortcut) or a read-only
``Program Files`` tree, so the database creation would fail on the operator's
machine while working perfectly on the developer's.

Everything mutable therefore lives under per-user directories:

======================  =====================================================
config / logs           ``%LOCALAPPDATA%\\TunerLab``
calibration databases   ``Documents\\TunerLab\\calibrations``
exported reports        ``Documents\\TunerLab\\reports``
target patterns         ``Documents\\TunerLab\\patterns``
======================  =====================================================
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

__all__ = [
    'APP_FOLDER', 'config_dir', 'log_dir', 'data_dir', 'calibration_dir',
    'report_dir', 'pattern_dir', 'ensure_app_dirs', 'default_calibration_path',
    'default_report_path',
]

#: Folder name used under ``%LOCALAPPDATA%`` and under ``Documents``.
APP_FOLDER = 'TunerLab'


def _local_appdata() -> Path:
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
    return Path(base) if base else Path.home() / '.local' / 'share'


def config_dir() -> Path:
    """Per-user configuration and log directory."""
    return _local_appdata() / APP_FOLDER


def log_dir() -> Path:
    return config_dir() / 'logs'


def _documents() -> Path:
    """Best-effort Documents folder, tolerating a redirected/absent profile."""
    if sys.platform == 'win32':
        # CSIDL_PERSONAL honours OneDrive / domain folder redirection.  Do not
        # guess this path from USERPROFILE: Windows owns the directory policy.
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0:
            return Path(buffer.value)
    candidate = Path.home() / 'Documents'
    return candidate if candidate.parent.is_dir() else Path.home()


def data_dir() -> Path:
    """Directory for operator-visible output (calibrations, reports)."""
    override = os.environ.get('TUNERLAB_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    return _documents() / APP_FOLDER


def calibration_dir() -> Path:
    return data_dir() / 'calibrations'


def report_dir() -> Path:
    return data_dir() / 'reports'


def pattern_dir() -> Path:
    """Where the target patterns (Pattern Editor) live."""
    return data_dir() / 'patterns'


def ensure_app_dirs() -> None:
    """Create every mutable directory; safe to call more than once."""
    for path in (config_dir(), log_dir(), calibration_dir(), report_dir(),
                 pattern_dir()):
        path.mkdir(parents=True, exist_ok=True)


def default_calibration_path(prefix: str = 'cal') -> Path:
    """A fresh, timestamped calibration path that cannot collide with an old one."""
    ensure_app_dirs()
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_' + uuid.uuid4().hex[:8]
    return calibration_dir() / f'{prefix}_{stamp}.h5'


def default_report_path(prefix: str = 'report') -> Path:
    """A fresh, timestamped report path that cannot collide with an old one."""
    ensure_app_dirs()
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_' + uuid.uuid4().hex[:8]
    return report_dir() / f'{prefix}_{stamp}.json'

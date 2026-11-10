"""Filesystem locations that work the same running from source and frozen.

The shared policy lives in :mod:`tuner.appdirs` — Qt-free, so the web console,
the CLI and the desktop shell all resolve the same directories.  This module
adds the one thing that is desktop-specific: :func:`app_root`, the folder that
holds the read-only application resources.
"""
from __future__ import annotations

import sys
from pathlib import Path

from tuner.appdirs import (  # noqa: F401 - re-exported for the desktop package
    APP_FOLDER, calibration_dir, config_dir, data_dir, default_calibration_path,
    default_report_path, ensure_app_dirs, log_dir, pattern_dir, report_dir,
)

__all__ = [
    'APP_FOLDER', 'app_root', 'is_frozen', 'config_dir', 'log_dir',
    'data_dir', 'calibration_dir', 'report_dir', 'pattern_dir',
    'ensure_app_dirs', 'default_calibration_path', 'default_report_path',
]


def is_frozen() -> bool:
    """``True`` when running from a PyInstaller build."""
    return bool(getattr(sys, 'frozen', False))


def app_root() -> Path:
    """Directory that holds the read-only application resources.

    For a frozen build this is the ``_internal`` folder created by PyInstaller
    (``sys._MEIPASS``); from source it is the project root, i.e. the parent of
    the ``tunerlab_app`` package.  The vendor DLL and the web assets are
    resolved from here.
    """
    if is_frozen():
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent

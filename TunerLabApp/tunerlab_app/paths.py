"""Desktop-facing alias of :mod:`tuner.appdirs`.

The directory policy lives in a Qt-free module so the web console and the
command-line entry points resolve exactly the same paths as the desktop shell.
"""
from __future__ import annotations

from .appdirs import (  # noqa: F401 - re-exported for the desktop package
    APP_FOLDER, app_root, calibration_dir, config_dir, data_dir,
    default_calibration_path, default_report_path, ensure_app_dirs, is_frozen,
    log_dir, pattern_dir, report_dir,
)

__all__ = [
    'APP_FOLDER', 'app_root', 'is_frozen', 'config_dir', 'log_dir',
    'data_dir', 'calibration_dir', 'report_dir', 'pattern_dir',
    'ensure_app_dirs',
    'default_calibration_path', 'default_report_path',
]

"""Unified TunerLab desktop application.

``python -m tunerlab_app`` opens one window that owns the motion console, the
VNA panel, the calibration workbench and the operation log.

The application is a *shell*: every backend module still lives in the original
``TunerLab/tuner`` package, which is imported read-only and never modified.  When
running from source that package is a sibling directory, so :func:`_bootstrap`
puts it on ``sys.path``; a frozen build has it bundled already.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap() -> None:
    """Make the untouched original project importable when running from source."""
    if getattr(sys, 'frozen', False):
        return
    candidate = Path(__file__).resolve().parent.parent.parent / 'TunerLab'
    if (candidate / 'tuner').is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


_bootstrap()

# Re-point the theme helpers at Noto Sans SC before any page module binds them.
from .fonts import install_theme_fonts  # noqa: E402 - after sys.path setup

install_theme_fonts()

from .shell import MainWindow, main  # noqa: E402 - after sys.path setup

__all__ = ['MainWindow', 'main']

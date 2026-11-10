"""One typeface for the whole application: Noto Sans SC.

The host machine ships ``NotoSansSC-VF.ttf`` — a variable font covering Light
(300) through Black (900) — so headings, body copy and numeric readouts can all
come from the same family and still be told apart by weight.

The original project paired Noto Sans SC with Gilroy Light/ExtraBold for numbers.
That is a reasonable house style, but it produces two families on one page, and
the numeric faces are not installed everywhere.  This module re-points the theme
helpers at Noto Sans SC at runtime; the original files stay untouched on disk.
"""
from __future__ import annotations

from PySide6.QtGui import QFont, QFontDatabase

__all__ = ['FAMILY', 'WEIGHTS', 'ui_font', 'px_font', 'text_font', 'numeric_font',
           'install_theme_fonts', 'is_available',
           'set_ui_scale', 'get_ui_scale', 'scaled_px',
           'UI_SCALE_MIN', 'UI_SCALE_MAX']

#: The single family used everywhere.
FAMILY = 'Noto Sans SC'

#: Variable-font weights, by name.
WEIGHTS = {
    'light': QFont.Weight.Light,        # 300
    'regular': QFont.Weight.Normal,     # 400
    'medium': QFont.Weight.Medium,      # 500
    'demibold': QFont.Weight.DemiBold,  # 600
    'bold': QFont.Weight.Bold,          # 700
}

#: Window-driven UI scale.  The shell recalculates it on resize; every font helper
#: reads it so text tracks the window instead of staying pinned to design pixels.
_ui_scale = 1.0
UI_SCALE_MIN = 0.92
UI_SCALE_MAX = 1.50


def set_ui_scale(scale: float) -> None:
    """Clamp and store the global UI scale (1.0 = the 1280px design width)."""
    global _ui_scale
    _ui_scale = max(UI_SCALE_MIN, min(UI_SCALE_MAX, float(scale)))


def get_ui_scale() -> float:
    return _ui_scale


def scaled_px(px: float) -> int:
    """A design pixel size adjusted for the current window scale."""
    return max(1, int(round(float(px) * _ui_scale)))


def is_available() -> bool:
    """True when the family is installed and can be selected.

    ``QFontDatabase`` may only be touched once a ``QGuiApplication`` exists, and
    this module runs at package-import time — before ``QApplication`` is built.
    Asking the font database that early aborts the process without a traceback,
    so before the application exists the installed font file is the evidence.
    """
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is not None:
        return FAMILY in QFontDatabase.families()
    import os
    from pathlib import Path as _Path
    windir = _Path(os.environ.get('WINDIR', r'C://Windows'))
    local = _Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft/Windows/Fonts'
    for folder in (windir / 'Fonts', local):
        try:
            if any(folder.glob('NotoSansSC*')):
                return True
        except OSError:
            continue
    return False


def ui_font(size: int = 10, weight: str = 'regular') -> QFont:
    """A Noto Sans SC font at the requested size and weight."""
    font = QFont(FAMILY)
    font.setPointSizeF(max(6.0, float(size) * _ui_scale))
    font.setWeight(WEIGHTS.get(weight, QFont.Weight.Normal))
    font.setStyleHint(QFont.StyleHint.SansSerif)
    return font


def px_font(px: int, weight: str = 'regular') -> QFont:
    """Font sized in CSS pixels, so it matches the mock-up 1:1.

    ``QFont(family, pointSize)`` depends on the screen DPI; the design was written
    in px, so the sizes are applied as pixel sizes instead of points.
    """
    font = QFont(FAMILY)
    font.setPixelSize(scaled_px(px))
    font.setWeight(WEIGHTS.get(weight, QFont.Weight.Normal))
    font.setStyleHint(QFont.StyleHint.SansSerif)
    return font


def text_font(size: int = 10, bold: bool = False) -> QFont:
    """Mixed Chinese/English content."""
    return ui_font(size, 'bold' if bold else 'regular')


def numeric_font(size: int = 10, bold: bool = False) -> QFont:
    """Digits and readouts: same family, a touch heavier for legibility."""
    return ui_font(size, 'demibold' if bold else 'medium')


def install_theme_fonts() -> bool:
    """Point ``tuner.ui_theme`` at Noto Sans SC for numbers as well.

    Must run before any page module is imported: those modules do
    ``from tuner.ui_theme import numeric_font`` and bind the function object, so a
    later patch would not reach them.  ``tunerlab_app/__init__`` calls this
    between the ``sys.path`` bootstrap and the shell import.

    Returns ``True`` when the family was found and the theme was re-pointed.
    """
    if not is_available():
        return False
    from tuner import ui_theme

    ui_theme.TEXT_FAMILY = FAMILY
    ui_theme.NUMERIC_FAMILY = FAMILY
    ui_theme.NUMERIC_BOLD_FAMILY = FAMILY
    ui_theme.NUMERIC_FALLBACK = FAMILY
    ui_theme.TEXT_FALLBACK = FAMILY
    ui_theme.text_font = text_font
    ui_theme.numeric_font = numeric_font
    return True

"""Shared look-and-feel for the TunerLab desktop application.

Design tokens follow ``TunerLab_0.3.9_界面优化设计规格.md`` (the 0.3.9 mock-up):

* ``--ui-accent`` ``#1f6fb2`` — the single primary blue of the whole app;
* status palette ``ok #1f7a4d`` / ``warn #a8640a`` / ``fail #c0392b`` / ``move #6d5bd0``;
* ink ``#1b2735``, muted ``#6b7b8d``, faint ``#97a6b6``;
* surface ``bg #f2f5f9`` / ``panel #ffffff`` / ``line #dbe3ec``;
* radii: controls ``6px``, cards ``9px``.

Font rules follow the project convention:

* mixed Chinese/English content uses a single family, ``Noto Sans SC``;
* pure English or numeric content uses ``Gilroy`` Light, with ``Gilroy`` ExtraBold for
  emphasis.

Both families are installed on the target machine (``NotoSansSC-VF.ttf`` in the system
font directory, ``Gilroy-Light.otf`` / ``Gilroy-ExtraBold.otf`` in the user font
directory). The helpers fall back to a sane default when a family is missing, so the
application never renders with an unexpected fallback chain.
"""
from __future__ import annotations

from PySide6.QtGui import QFont, QFontDatabase

__all__ = [
    'TEXT_FAMILY', 'NUMERIC_FAMILY', 'STYLESHEET',
    'text_font', 'numeric_font', 'mono_family', 'apply_theme',
]

#: Family for mixed Chinese/English text.
TEXT_FAMILY = 'Noto Sans SC'

#: Family for pure English and numeric content (light weight is the default).
NUMERIC_FAMILY = 'Gilroy Light'

#: Family for emphasised pure English and numeric content.
NUMERIC_BOLD_FAMILY = 'Gilroy ExtraBold'

#: Fallback used when Gilroy is unavailable.
NUMERIC_FALLBACK = 'Consolas'

#: Fallback used when Noto Sans SC is unavailable.
TEXT_FALLBACK = 'Microsoft YaHei'

#: Light application theme, built from the 0.3.9 design tokens.
STYLESHEET = """
QMainWindow, QWidget { background: #f2f5f9; color: #1b2735; }
QGroupBox {
    background: #ffffff; border: 1px solid #dbe3ec; border-radius: 9px;
    margin-top: 14px; padding: 10px 8px 8px 8px; font-weight: 600;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #3d4d5f; }
QLabel { background: transparent; }
QPushButton {
    background: #ffffff; border: 1px solid #c2cdda; border-radius: 6px;
    padding: 6px 14px; min-height: 20px;
}
QPushButton:hover { background: #eaf0f6; border-color: #1f6fb2; }
QPushButton:pressed { background: #e0eaf4; }
QPushButton:disabled { background: #eef1f5; color: #9aa5b1; border-color: #e2e8ef; }
QPushButton[accent="true"] { background: #1f6fb2; color: #ffffff; border-color: #1f6fb2; font-weight: 600; }
QPushButton[accent="true"]:hover { background: #2f8fd0; }
QPushButton[accent="true"]:disabled { background: #a9c4dd; border-color: #a9c4dd; color: #ffffff; }
QPushButton[danger="true"] { background: #c0392b; color: #ffffff; border-color: #c0392b; font-weight: 700; }
QPushButton[danger="true"]:hover { background: #d0453a; }
QPushButton[danger="true"]:disabled { background: #e0a49d; border-color: #e0a49d; color: #ffffff; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {
    background: #ffffff; border: 1px solid #c2cdda; border-radius: 6px; padding: 4px 6px;
    selection-background-color: #1f6fb2;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: #1f6fb2; }
QTableWidget {
    background: #ffffff; border: 1px solid #dbe3ec; gridline-color: #e9eef4;
    selection-background-color: #e7f1fa; selection-color: #10202e;
}
QHeaderView::section {
    background: #eef2f7; border: 0; border-right: 1px solid #dbe3ec;
    border-bottom: 1px solid #dbe3ec; padding: 5px 6px; font-weight: 600;
}
QTableWidget::item { padding: 3px 5px; }
QListWidget { background: #ffffff; border: 1px solid #dbe3ec; border-radius: 6px; }
QListWidget::item { padding: 10px 8px; border-bottom: 1px solid #eef1f5; }
QListWidget::item:selected { background: #1f6fb2; color: #ffffff; }
QStatusBar { background: #eef2f7; color: #3d4d5f; }
QTabWidget::pane { border: 1px solid #dbe3ec; background: #ffffff; }
QTabBar::tab {
    background: #eef2f7; padding: 7px 16px; border: 1px solid #dbe3ec;
    border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px;
}
QTabBar::tab:selected { background: #ffffff; color: #1f6fb2; font-weight: 600; }
QCheckBox { background: transparent; }
QScrollBar:vertical { background: #f0f3f7; width: 10px; }
QScrollBar::handle:vertical { background: #c2ccd8; border-radius: 5px; min-height: 30px; }
QScrollBar:horizontal { background: #f0f3f7; height: 10px; }
QScrollBar::handle:horizontal { background: #c2ccd8; border-radius: 5px; min-width: 30px; }
"""


def _available(family: str) -> bool:
    return family in QFontDatabase.families()


def text_font(size: int = 10, bold: bool = False) -> QFont:
    """Font for mixed Chinese/English content.

    A single family covers both scripts, so no separate Latin font is configured.
    """
    family = TEXT_FAMILY if _available(TEXT_FAMILY) else TEXT_FALLBACK
    font = QFont(family, size)
    font.setBold(bold)
    return font


def numeric_font(size: int = 10, bold: bool = False) -> QFont:
    """Font for pure English and numeric content.

    ``Gilroy`` Light is the default; emphasis switches to ``Gilroy`` ExtraBold, which
    is a real installed face rather than a synthesised bold.
    """
    families = QFontDatabase.families()
    wanted = NUMERIC_BOLD_FAMILY if bold else NUMERIC_FAMILY
    family = wanted if wanted in families else NUMERIC_FALLBACK
    font = QFont(family, size)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    return font


def mono_family() -> str:
    """Monospaced family for log output and code snippets."""
    families = QFontDatabase.families()
    for candidate in ('Consolas', 'Cascadia Mono', 'Courier New'):
        if candidate in families:
            return candidate
    return NUMERIC_FALLBACK


def apply_theme(app) -> None:
    """Install the light theme and the default text font on a ``QApplication``."""
    app.setStyleSheet(STYLESHEET)
    app.setFont(text_font(10))

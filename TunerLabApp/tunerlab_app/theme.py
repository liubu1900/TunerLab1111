"""Extra styling for the unified shell.

``tuner.ui_theme`` already defines the light theme and the font conventions for
the whole application; this module only adds the pieces the unified shell needs
on top of it (light navigation rail, page header, status pills) so the two consoles
share one visual language.

The look follows ``TunerLab_0.3.9_界面优化设计规格.md``: a light instrument
surface, a white navigation rail (not the old dark "dashboard" rail), the single
primary blue ``#1f6fb2`` for every active/selected state, and the status palette
``ok #1f7a4d`` / ``warn #a8640a`` / ``fail #c0392b`` / ``move #6d5bd0``.
"""
from __future__ import annotations

import re

from .fonts import UI_SCALE_MIN, UI_SCALE_MAX

__all__ = ['SHELL_QSS', 'PILL_STYLES', 'scaled_stylesheet']


SHELL_QSS = """
/* 0.3.9 light instrument theme: white rail, single accent blue, soft cards. */
* { font-family: "Noto Sans SC"; font-size: 13px; }
QGroupBox::title { font-size: 13px; font-weight: 600; }
QTableWidget, QTableView { font-size: 12.5px; }
QHeaderView::section { font-size: 11.5px; font-weight: 600; }
QPushButton { font-size: 12.5px; font-weight: 500; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { font-size: 12.5px; }
QCheckBox { font-size: 12.5px; }
QTreeWidget { font-size: 12.5px; }
QMenuBar, QMenu { font-size: 12.5px; }
QStatusBar { font-size: 11.5px; }

/* ---- surface ------------------------------------------------------- */
#Root {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 #f7f9fc, stop:0.42 #f2f5f9, stop:1 #eef2f8);
}
QStackedWidget { background: transparent; }
QScrollArea { background: transparent; border: 0; }

/* ---- navigation (light rail, single accent) ----------------------- */
#Sidebar { background: #ffffff; border-right: 1px solid #dbe3ec; }
#Sidebar QWidget { background: transparent; }
#BrandRow { background: transparent; }
#BrandMark {
    background: #1f6fb2;
    border-radius: 9px; color: #ffffff;
}
#BrandName { color: #1b2735; background: transparent; }
#BrandSub { color: #97a6b6; background: transparent; }
#NavTree { background: transparent; border: 0; outline: 0; padding: 0 8px;
           selection-background-color: transparent; selection-color: #1f6fb2; }
#NavTree::item {
    height: 32px; padding-left: 10px; color: #3d4d5f; border-radius: 7px;
    border: 0; border-left: 3px solid transparent;
}
#NavTree::item:hover { background: #eaf0f6; }
#NavTree::item:selected {
    background: #e7f1fa; color: #1f6fb2; border-left: 3px solid #1f6fb2;
    font-weight: 600;
}
#NavTree::item:disabled { color: #97a6b6; }
#NavTree::branch { background: transparent; }

/* ---- top bar -------------------------------------------------------- */
#TopBar { background: transparent; }
#TopTitle { color: #1b2735; background: transparent; }
#TopCrumb { color: #6b7b8d; background: transparent; }
#TopBadge {
    background: #ffffff; border: 1px solid #dbe3ec; border-radius: 15px;
    color: #1f7a4d;
}

/* ---- cards ---------------------------------------------------------- */
#Card, QGroupBox {
    background: #ffffff; border: 1px solid #dbe3ec; border-radius: 9px;
}
/* The motion page's action bar is a card without a group caption, exactly as
   in the mock-up: no title, no reserved title strip. */
#OpsBar { margin-top: 0; padding: 12px 14px; border-radius: 9px; }
/* Scale self-check result: same card face, shown only when there is a result. */
#NoticeBar { background: #ffffff; border: 1px solid #dbe3ec; border-radius: 9px; }
QGroupBox { margin-top: 18px; padding: 12px 16px 12px 16px; }
QGroupBox::title {
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 16px; top: 2px; padding: 0 4px; color: #3d4d5f;
}
#CardTitle { color: #1b2735; background: transparent; }
#FieldLabel { color: #6b7b8d; background: transparent; }

/* ---- KPI tiles ------------------------------------------------------ */
#Kpi { background: #ffffff; border: 1px solid #dbe3ec; border-radius: 9px; }
#KpiLabel { color: #6b7b8d; background: transparent; }
#KpiValue { color: #1b2735; background: transparent; }
#KpiValue[tone="ok"] { color: #1f7a4d; }
#KpiValue[tone="warn"] { color: #a8640a; }
#KpiValue[tone="error"] { color: #c0392b; }
#KpiSub { color: #97a6b6; background: transparent; }

/* ---- inputs --------------------------------------------------------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: #ffffff; border: 1px solid #dbe3ec; border-radius: 6px;
    padding: 6px 9px; color: #1b2735; selection-background-color: #cfe2ff;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 1px solid #1f6fb2; background: #ffffff;
}
/* The mock-up uses plain text inputs: no spin arrows anywhere. */
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    width: 0; height: 0; border: 0; background: transparent;
}
QSpinBox, QDoubleSpinBox { padding-right: 9px; }
QComboBox::drop-down { border: 0; width: 18px; }
QComboBox QAbstractItemView {
    background: #ffffff; border: 1px solid #dbe3ec; border-radius: 6px;
    selection-background-color: #e7f1fa; selection-color: #1d4f74; outline: 0;
}
/* No style-sheet images here: qss images ignore the device pixel ratio and
   were painted as a clipped arc on a scaled desktop.  The 34x19 switch on the
   communication page is the self-painted ToggleSwitch widget instead. */
QCheckBox { spacing: 9px; color: #1b2735; }
QCheckBox::indicator {
    width: 15px; height: 15px; border: 1px solid #c6d6ea; border-radius: 4px;
    background: #ffffff;
}
QCheckBox::indicator:checked { background: #1f6fb2; border-color: #1f6fb2; }

/* ---- buttons -------------------------------------------------------- */
QPushButton {
    background: #ffffff; border: 1px solid #dbe3ec; border-radius: 6px;
    padding: 6px 14px; color: #1b2735;
}
QPushButton:hover { background: #eaf0f6; border-color: #1f6fb2; }
QPushButton:pressed { background: #e0eaf4; }
QPushButton:disabled { color: #a9b7c6; background: #f7f9fc; border-color: #e6ecf5; }
QPushButton[accent="true"] {
    background: #1f6fb2; border: 0; color: #ffffff; font-weight: 600;
}
QPushButton[accent="true"]:hover { background: #2f8fd0; }
QPushButton[accent="true"]:disabled {
    background: #a9c4dd; color: #ffffff; border: 0;
}
/* The active member of a Smith-chart mode toggle (缩放 / 移动). */
QPushButton[modeactive="true"] {
    background: #1f6fb2; border: 0; color: #ffffff; font-weight: 600;
}
QPushButton[modeactive="true"]:hover { background: #2f8fd0; }
/* A button that drives the machine: it must not look like the others. */
#DangerButton {
    background: #fff5e3; border: 1px solid #eed49c; border-radius: 6px;
    padding: 6px 14px; color: #a8640a; font-weight: 600;
}
#DangerButton:hover { background: #fdeed5; border-color: #e0a94a; }
#DangerButton:disabled { color: #a9b7c6; background: #f7f9fc; border-color: #e6ecf5; }

/* ---- tables --------------------------------------------------------- */
QTableWidget, QTableView {
    background: #ffffff; border: 0; gridline-color: #eef2f7;
    selection-background-color: #e7f1fa; selection-color: #1d4f74;
    alternate-background-color: #fbfcff;
}
QHeaderView::section {
    background: #f6f8fc; color: #6b7b8d; border: 0;
    border-bottom: 1px solid #dbe3ec; padding: 7px 8px;
}
QTableCornerButton::section { background: #f6f8fc; border: 0; }
QTableView::item { padding: 3px 6px; }

/* ---- misc ----------------------------------------------------------- */
#Pill { border-radius: 20px; padding: 4px 12px; }

/* ---- wizard step bar ------------------------------------------------ */
#StepBar { background: transparent; }
#StepButton {
    background: #eef2f7; border: 0; border-bottom: 3px solid #dbe3ec;
    border-radius: 0; padding: 9px 8px; color: #6b7b8d; font-weight: 500;
}
#StepButton:hover { background: #e6ecf6; color: #1b2735; }
#StepButton[state="done"] { color: #1f6fb2; border-bottom: 3px solid #b9d8ef; }
#StepButton[state="current"] {
    background: #ffffff; color: #1b2735; font-weight: 600;
    border-bottom: 3px solid #1f6fb2;
}
#StepTitle { color: #1b2735; background: transparent; }
QMenuBar { background: #ffffff; border-bottom: 1px solid #dbe3ec; }
QMenuBar::item:selected { background: #e7f1fa; }
QToolBar { background: #ffffff; border-bottom: 1px solid #dbe3ec;
           spacing: 6px; padding: 4px; }
QStatusBar { background: #ffffff; border-top: 1px solid #dbe3ec; color: #5b6b7c; }
QStatusBar QLabel { padding: 0 8px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #d7e1ec; border-radius: 5px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: #c3d2e2; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #d7e1ec; border-radius: 5px; min-width: 28px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
"""

#: Status pill colours keyed by semantic state (paired soft-bg / line tokens).
PILL_STYLES = {
    'ok': 'background:#e8f5ee; color:#1f7a4d; border:1px solid #bfe3cf;',
    'warn': 'background:#fff5e3; color:#a8640a; border:1px solid #eed49c;',
    'error': 'background:#fdecea; color:#c0392b; border:1px solid #f2c2bc;',
    'idle': 'background:#eaf0f6; color:#6b7b8d; border:1px solid #dbe3ec;',
    'move': 'background:#efecfb; color:#6d5bd0; border:1px solid #d3caf3;',
}


_FONT_SIZE_RE = re.compile(r'(font-size\s*:\s*)([0-9.]+)\s*(px)')


def scaled_stylesheet(base_qss: str, scale: float, floor: float = 12.0) -> str:
    """Return *base_qss* with type sizes and control padding scaled.

    Qt applies a stylesheet on top of per-widget fonts, so driving the sheet is
    enough to move every caption together when the window resizes.  A size floor
    keeps button labels and table text legible on a small window; a ceiling keeps
    them from overflowing their controls on a wide monitor.  A handful of ID
    selectors restore the visual hierarchy that a blanket ``*`` rule would
    otherwise flatten.
    """
    scale = max(UI_SCALE_MIN, min(UI_SCALE_MAX, float(scale)))

    def _size(match: 're.Match[str]') -> str:
        value = max(floor, float(match.group(2)) * scale)
        return f'{match.group(1)}{value:.1f}{match.group(3)}'

    qss = _FONT_SIZE_RE.sub(_size, base_qss)

    def px(value: float, minimum: float | None = None) -> float:
        return max(floor if minimum is None else minimum, value * scale)

    # Restore hierarchy + give text room to breathe as the window grows.
    qss += (
        f'\n#TopTitle {{ font-size: {px(17):.1f}px; }}'
        f'\n#BrandName {{ font-size: {px(14):.1f}px; }}'
        f'\n#BrandSub {{ font-size: {px(10.5):.1f}px; }}'
        f'\n#KpiValue {{ font-size: {px(20):.1f}px; }}'
        f'\n#CardTitle {{ font-size: {px(13.5):.1f}px; }}'
        f'\nQPushButton {{ padding: {6*scale:.0f}px {14*scale:.0f}px; '
        f'min-height: {24*scale:.0f}px; }}'
        f'\nQLineEdit, QSpinBox, QDoubleSpinBox, QComboBox '
        f'{{ padding: {4*scale:.0f}px {9*scale:.0f}px; }}'
        f'\n#NavTree::item {{ height: {max(30, int(32*scale))}px; }}'
        f'\n#StepButton {{ padding: {9*scale:.0f}px {8*scale:.0f}px; }}'
        f'\nQTabBar::tab {{ padding: {7*scale:.0f}px {16*scale:.0f}px; }}'
    )
    return qss

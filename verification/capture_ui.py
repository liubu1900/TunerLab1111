"""Render the redesigned TunerLab shell to PNG files.

The capture runs against the real ``MainWindow`` with the real theme, so the
pictures show exactly what the operator sees.  Nothing is opened on the bench:
the pages are rendered offscreen and the controller link is left alone.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT / 'TunerLabApp', ROOT / 'TunerLab'):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from PySide6.QtWidgets import QApplication  # noqa: E402

OUT = ROOT / 'verification' / 'out' / 'ui'

#: The pages that show the new design language best.
PAGES = (
    ('studio-overview', '01-总览'),
    ('studio-setup', '02-建库向导'),
    ('studio-browser', '03-库浏览器'),
    ('studio-pattern', '04-目标图案'),
    ('studio-smith', '05-Smith图'),
    ('hw-comm', '06-通讯设置'),
    ('hw-motion', '07-轴移动控制'),
    ('vna', '08-矢网控制'),
)


def install_font() -> str:
    """Load the CJK typeface into the font database.

    The offscreen platform plugin ships no fonts and does not read the system
    font database, so every Chinese label would be drawn as an empty box.  The
    file itself is installed on the machine; it just has to be handed to Qt.
    """
    from PySide6.QtGui import QFontDatabase
    candidates = [
        Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts' / 'NotoSansSC-VF.ttf',
        Path(r'C:\Windows\Fonts\NotoSansSC-VF.ttf'),
        Path(r'C:\Windows\Fonts\msyh.ttc'),
    ]
    for path in candidates:
        if path.is_file():
            font_id = QFontDatabase.addApplicationFont(str(path))
            families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
            if families:
                return families[0]
    return ''


def main() -> int:
    from tuner.ui_theme import STYLESHEET, apply_theme
    from tunerlab_app.fonts import px_font
    from tunerlab_app.shell import MainWindow
    from tunerlab_app.theme import SHELL_QSS

    app = QApplication(sys.argv)
    family = install_font()
    print(f'font family: {family or "<none>"}')
    apply_theme(app)
    app.setStyleSheet(STYLESHEET + SHELL_QSS)
    if family:
        app.setFont(px_font(13, 'regular'))
        from PySide6.QtGui import QFont
        app.setFont(QFont(family, 13))
    else:
        app.setFont(px_font(13, 'regular'))

    window = MainWindow(smoke_test=True)   # virtual mode: no bench traffic
    window.resize(1400, 900)
    window.show()
    app.processEvents()
    time.sleep(0.4)
    app.processEvents()

    OUT.mkdir(parents=True, exist_ok=True)
    for key, name in PAGES:
        if key not in window.pages:
            continue
        window.go(key)
        app.processEvents()
        time.sleep(0.35)
        app.processEvents()
        path = OUT / f'{name}.png'
        window.grab().save(str(path))
        print(f'  {path.name}', flush=True)

    # The wizard's "布点计划" step carries the Smith preview that the mock-up
    # showed, so it gets its own picture.
    from PySide6.QtWidgets import QPushButton
    wizard = window.pages.get('studio-setup')
    if wizard is not None:
        window.go('studio-setup')
        app.processEvents()
        for button in wizard.findChildren(QPushButton):
            if '布点计划' in button.text():
                button.click()
                break
        app.processEvents()
        time.sleep(0.5)
        app.processEvents()
        path = OUT / '09-布点计划-Smith预览.png'
        window.grab().save(str(path))
        print(f'  {path.name}', flush=True)

    print(f'\nwrote {len(list(OUT.glob("*.png")))} images to {OUT}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

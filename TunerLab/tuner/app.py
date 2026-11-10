"""TunerLab hardware console.

A compact desktop front end for the Zmotion motion controller: communication setup,
four-axis manual movement and per-axis parameter settings. The calibration studio in
:mod:`tuner.ui` remains the RF-side application; this console is the machine-side one.

Run with::

    python -m tuner.app
"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget, QMainWindow, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .settings import SettingsStore
from .ui_hardware import (
    AxisMotionPage, AxisParameterPage, CommunicationPage, HardwareSession,
    OperationLogPage,
)
from .ui_theme import apply_theme, numeric_font, text_font

__all__ = ['HardwareConsole', 'main']

PAGE_TITLES = ('通讯设置', '轴移动控制', '轴参数设定', '操作日志')


class HardwareConsole(QMainWindow):
    """Main window hosting the three hardware pages."""

    def __init__(self, store: SettingsStore | None = None):
        super().__init__()
        self.setWindowTitle('TunerLab · 运动控制台')
        self.resize(1160, 820)
        self.session = HardwareSession(store or SettingsStore())

        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = QWidget()
        sidebar.setFixedWidth(210)
        sidebar.setStyleSheet('background:#e9eef4;')
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(0, 16, 0, 16)
        brand = QLabel('TunerLab')
        brand.setFont(text_font(17, bold=True))
        brand.setStyleSheet('color:#2f6f9f;padding:0 16px;')
        subtitle = QLabel('运动控制台')
        subtitle.setFont(text_font(10))
        subtitle.setStyleSheet('color:#5b6b7c;padding:0 16px 12px 16px;')
        side_layout.addWidget(brand)
        side_layout.addWidget(subtitle)

        self.nav = QListWidget()
        self.nav.addItems(PAGE_TITLES)
        self.nav.setFrameShape(QListWidget.Shape.NoFrame)
        self.nav.setStyleSheet(
            'QListWidget {background:#e9eef4;border:0;}'
            'QListWidget::item {padding:13px 16px;color:#2b3b4b;}'
            'QListWidget::item:selected {background:#2f6f9f;color:#ffffff;}')
        side_layout.addWidget(self.nav, 1)

        self.state_label = QLabel('未连接')
        self.state_label.setFont(numeric_font(10, bold=True))
        self.state_label.setStyleSheet('color:#8a6d3b;padding:0 16px;')
        side_layout.addWidget(self.state_label)
        layout.addWidget(sidebar)

        self.stack = QStackedWidget()
        self.pages = (
            CommunicationPage(self.session),
            AxisMotionPage(self.session),
            AxisParameterPage(self.session),
            OperationLogPage(self.session),
        )
        for page in self.pages:
            self.stack.addWidget(page)
        layout.addWidget(self.stack, 1)

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.session.connection_changed.connect(self.on_connection_changed)
        self.session.status_changed.connect(self.on_status)
        # The pages rebuild themselves when the tuner model changes; the console
        # only reports it in the status bar.
        self.session.settings_changed.connect(self.on_settings_changed)
        self.nav.setCurrentRow(0)

    def on_settings_changed(self) -> None:
        """Report the active tuner model in the status bar."""
        count = self.session.settings.axis_count
        self.statusBar().showMessage(
            f'tuner 型号: {count} 轴 ({", ".join(self.session.axes)})')

    def on_connection_changed(self, connected: bool) -> None:
        self.state_label.setText('已连接' if connected else '未连接')
        self.state_label.setStyleSheet(
            f'color:{"#1f7a4d" if connected else "#8a6d3b"};padding:0 16px;')

    def on_status(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def closeEvent(self, event):
        """Close the controller link before the window goes away."""
        try:
            if self.session.connected:
                self.session.disconnect()
        except Exception:  # noqa: BLE001 - never block shutdown on a link error
            pass
        event.accept()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app)
    window = HardwareConsole()
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())

"""Settings and about page: where everything lives and which build this is.

Support calls start with "which version, which DLL, which log" — this page answers
all three without asking the operator to open a terminal.
"""
from __future__ import annotations

import os
import platform
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QTextEdit,
    QVBoxLayout, QWidget,
)

from . import version
from tuner.ui_theme import numeric_font, text_font
from tuner.zmotion import vendor_dir
from . import paths
from .pages_studio import _page_title
from .runtime import log_path


class SettingsAboutPage(QWidget):
    """Version, paths and environment facts."""

    def __init__(self, app_state, parent=None):
        super().__init__(parent)
        self.state = app_state
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)
        root.addLayout(_page_title(
            '设置与关于',
            '配置、日志与校准数据分开放：配置和日志在本地应用数据目录，'
            '校准库与报告在「文档」目录，卸载或升级都不会删除测量数据。'))

        about = QGroupBox('版本')
        form = QFormLayout(about)
        name = QLabel(f'{version.__app_name__} {version.__version__}')
        name.setFont(text_font(15, bold=True))
        form.addRow('程序', name)
        form.addRow('构建日期', QLabel(version.__release_date__))
        form.addRow('运行方式', QLabel('安装包（冻结）' if paths.is_frozen() else '源码'))
        form.addRow('Python', QLabel(platform.python_version()))
        root.addWidget(about)

        where = QGroupBox('目录')
        where_form = QFormLayout(where)
        self._add_path_row(where_form, '配置文件', str(self.state.hw.store.path))
        self._add_path_row(where_form, '日志文件', str(log_path()))
        self._add_path_row(where_form, '操作日志 (JSONL)',
                           str(paths.log_dir() / 'operations.jsonl'))
        self._add_path_row(where_form, '校准库', str(paths.calibration_dir()))
        self._add_path_row(where_form, '报告', str(paths.report_dir()))
        dll = vendor_dir() / 'zauxdll.dll'
        self._add_path_row(where_form, 'Zmotion SDK',
                           str(dll) if dll.is_file() else f'{dll}（缺失）')
        root.addWidget(where)

        buttons = QHBoxLayout()
        for label, target in (
                ('打开配置目录', paths.config_dir()),
                ('打开日志目录', paths.log_dir()),
                ('打开校准库目录', paths.calibration_dir()),
                ('打开报告目录', paths.report_dir())):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, p=target: self._open(p))
            buttons.addWidget(button)
        buttons.addStretch()
        root.addLayout(buttons)

        notes = QGroupBox('运行约束（请勿忽略）')
        notes_layout = QVBoxLayout(notes)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(
            '· 软件急停不能替代硬件安全联锁；急停后必须复位并重新回零才允许运动。\n'
            '· 各轴独立运动：X 轴不再受探针位置约束，软件也不会自动回缩探针；\n'
            '  移动 X 前的碰撞判断由操作员与硬件联锁负责，请务必确认探针已避让。\n'
            '· UNITS 与编码器标度不一致时，软件会拒绝运动——这是保护，不是故障。\n'
            '· 校准库的 simulated 标记不可混用：模拟数据不能当作实测结果交付。\n'
            '· HDF5 主库与同名 SQLite 边车必须一起保存，否则任务状态与失败原因会丢失。')
        text.setFont(text_font(10))
        notes_layout.addWidget(text)
        root.addWidget(notes, 1)

    @staticmethod
    def _add_path_row(form: QFormLayout, label: str, value: str) -> None:
        widget = QLabel(value)
        widget.setFont(numeric_font(9))
        widget.setWordWrap(True)
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(label, widget)

    @staticmethod
    def _open(path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))  # noqa: S606 - Windows shell open
        except Exception:  # noqa: BLE001 - opening a folder is best effort
            pass

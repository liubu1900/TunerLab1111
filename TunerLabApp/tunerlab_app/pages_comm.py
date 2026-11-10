"""Communication page, laid out exactly like the approved scheme-5 mock-up.

Two cards side by side (控制器连接 / 连接状态) and a full-width 诊断 note, all with
the dashboard card styling from the theme.  The controls are the same ones the
original page offered; only the arrangement changed.
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from tuner.axis_params import TUNER_MODELS, tuner_axes
from tuner.operation_log import operation_log
from tuner.ui_hardware import HardwareSession
from tuner.zmotion import ZmotionController, probe_controller, vendor_dir

from .bg import run_in_background
from .fonts import px_font
from .ui import Card
from .widgets import ToggleSwitch

__all__ = ['CommunicationPage', 'SafeCommunicationPage']


class _SwitchRow(QWidget):
    """Switch plus caption, clickable anywhere along the row.

    The old checkbox only reacted inside its 108 px hit area while the layout
    stretched the widget to 978 px, so most clicks on the caption did nothing.
    The whole row toggles now, with a pointing-hand cursor to say so.
    """

    clicked = Signal()

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt override
        if (event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class CommunicationPage(QWidget):
    """Connection settings in the scheme-5 card layout."""

    def __init__(self, session: HardwareSession, parent=None):
        super().__init__(parent)
        self.session = session

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 0, 22, 18)
        root.setSpacing(14)

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self._build_connection_card(), 105)
        body.addWidget(self._build_status_card(), 100)
        root.addLayout(body)
        root.addWidget(self._build_note_card())
        root.addStretch(1)

        session.connection_changed.connect(self.on_connection_changed)
        session.status_changed.connect(self.on_status)
        self.on_connection_changed(session.connected)

    # ---------------- construction ----------------
    def _build_connection_card(self) -> Card:
        card = Card('控制器连接')
        form = QGridLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setColumnStretch(1, 1)

        def label(text: str) -> QLabel:
            item = QLabel(text)
            item.setObjectName('FieldLabel')
            item.setFont(px_font(11.5, 'regular'))
            return item

        self.ip_edit = QLineEdit(self.session.settings.connection.ip)
        self.ip_edit.setPlaceholderText('192.168.0.11')
        self.ip_edit.setEnabled(self.session.settings.mode == 'real')
        self.ip_edit.editingFinished.connect(self._save_connection)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem('真实控制器 (Zmotion)', 'real')
        self.mode_combo.addItem('虚拟控制器 (无硬件演练)', 'virtual')
        self.mode_combo.setCurrentIndex(
            self.mode_combo.findData(self.session.settings.mode))
        self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)

        self.model_combo = QComboBox()
        for count in sorted(TUNER_MODELS):
            self.model_combo.addItem(
                f'{count} 轴 ({"/".join(tuner_axes(count))})', count)
        self.model_combo.setCurrentIndex(
            self.model_combo.findData(self.session.settings.axis_count))
        self.model_combo.currentIndexChanged.connect(self.on_model_changed)

        # A painted switch, not a QCheckBox with a style-sheet image: the image
        # was drawn into the 34x19 indicator box without its device pixel ratio
        # and appeared as a clipped arc, so half the control seemed dead.
        self.auto_switch = ToggleSwitch()
        self.auto_switch.setChecked(self.session.settings.connection.connect_on_start)
        self.auto_switch.toggled.connect(self._on_auto_connect_toggled)
        self.auto_connect = self.auto_switch  # kept for the existing call sites

        auto_row = _SwitchRow()
        auto_row.setCursor(Qt.CursorShape.PointingHandCursor)
        auto_layout = QHBoxLayout(auto_row)
        auto_layout.setContentsMargins(0, 0, 0, 0)
        auto_layout.setSpacing(9)
        auto_layout.addWidget(self.auto_switch)
        auto_caption = QLabel('启动时自动连接')
        auto_caption.setFont(px_font(12.5, 'regular'))
        auto_caption.setStyleSheet('color:#22304a; background:transparent;')
        auto_layout.addWidget(auto_caption)
        auto_layout.addStretch(1)
        auto_row.clicked.connect(self.auto_switch.toggle)

        form.addWidget(label('控制器 IP'), 0, 0)
        form.addWidget(self.ip_edit, 0, 1)
        form.addWidget(label('控制器模式'), 1, 0)
        form.addWidget(self.mode_combo, 1, 1)
        form.addWidget(label('tuner 型号'), 2, 0)
        form.addWidget(self.model_combo, 2, 1)
        form.addWidget(label('启动'), 3, 0)
        form.addWidget(auto_row, 3, 1)
        card.body.addLayout(form)

        self.auto_hint = QLabel('')
        self.auto_hint.setObjectName('PageHint')
        self.auto_hint.setWordWrap(True)
        self.auto_hint.setFont(px_font(11.5, 'light'))
        card.body.addWidget(self.auto_hint)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.connect_button = QPushButton('连接')
        self.connect_button.setProperty('accent', True)
        self.disconnect_button = QPushButton('断开')
        self.search_button = QPushButton('广播搜索')
        row.addWidget(self.connect_button)
        row.addWidget(self.disconnect_button)
        row.addWidget(self.search_button)
        row.addStretch()
        card.body.addLayout(row)

        self.connect_button.clicked.connect(self.on_connect)
        self.disconnect_button.clicked.connect(self.on_disconnect)
        self.search_button.clicked.connect(self.on_search)
        self._sync_auto_hint()
        return card

    def _build_status_card(self) -> Card:
        card = Card('连接状态')
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(9)
        grid.setColumnStretch(1, 1)

        def caption(text: str) -> QLabel:
            item = QLabel(text)
            item.setObjectName('FieldLabel')
            item.setFont(px_font(11.5, 'regular'))
            return item

        self.state_label = QLabel('未连接')
        self.state_label.setFont(px_font(12.5, 'demibold'))
        self.firmware_label = QLabel('—')
        self.firmware_label.setFont(px_font(12.5, 'medium'))
        self.address_label = QLabel('—')
        self.address_label.setFont(px_font(12.5, 'medium'))
        self.library_label = QLabel('—')
        self.library_label.setFont(px_font(11, 'light'))
        self.library_label.setWordWrap(True)
        self.settings_label = QLabel(str(self.session.store.path))
        self.settings_label.setFont(px_font(11, 'light'))
        self.settings_label.setWordWrap(True)

        for row, (name, widget) in enumerate((
                ('状态', self.state_label),
                ('固件版本', self.firmware_label),
                ('控制器地址', self.address_label),
                ('SDK 库', self.library_label),
                ('设置文件', self.settings_label))):
            grid.addWidget(caption(name), row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(widget, row, 1)
        card.body.addLayout(grid)

        row = QHBoxLayout()
        row.setSpacing(8)
        save_button = QPushButton('保存通讯设置')
        reveal_button = QPushButton('打开所在文件夹')
        row.addWidget(save_button)
        row.addWidget(reveal_button)
        row.addStretch()
        card.body.addLayout(row)

        save_button.clicked.connect(self.on_save)
        reveal_button.clicked.connect(self.on_reveal)
        self.refresh_library_label()
        return card

    def _build_note_card(self) -> Card:
        card = Card('诊断')
        text = QLabel(
            '控制器使用以太网口与上位机通信，ZAux_OpenEth 通过 UDP 广播在本网段内定位设备，'
            '因此上位机 IP 必须与控制器处于同一网段。勾选「启动时自动连接」后，'
            '该设置立即写入配置文件，下次启动会在窗口出现后自动连接。')
        text.setObjectName('PageHint')
        text.setWordWrap(True)
        text.setFont(px_font(11.5, 'light'))
        card.body.addWidget(text)
        return card

    # ---------------- behaviour ----------------
    def _sync_auto_hint(self) -> None:
        on = self.session.settings.connection.connect_on_start
        if on:
            mode = '真实控制器' if self.session.settings.mode == 'real' else '虚拟控制器'
            self.auto_hint.setText(
                f'已保存：下次启动自动连接（{self.session.settings.connection.ip} · {mode}）')
            self.auto_hint.setStyleSheet('color:#12996a;')
        else:
            self.auto_hint.setText('已保存：启动时保持断开，需要手动点「连接」。')
            self.auto_hint.setStyleSheet('color:#8496ad;')

    def _on_auto_connect_toggled(self, checked: bool) -> None:
        try:
            self.session.set_connection_settings(
                self.ip_edit.text().strip(), connect_on_start=bool(checked))
        except Exception as exc:  # noqa: BLE001 - must be visible, not silent
            operation_log.exception('通讯', '保存自动连接', exc)
            self.auto_hint.setText(f'保存失败：{exc}')
            self.auto_hint.setStyleSheet('color:#c0392b;')
            return
        operation_log.info('通讯', '自动连接设置',
                           f'connect_on_start={bool(checked)}',
                           ip=self.ip_edit.text().strip())
        self._sync_auto_hint()
        self._flash('自动连接已' + ('开启' if checked else '关闭'))

    def _save_connection(self) -> None:
        try:
            self.session.set_connection_settings(
                self.ip_edit.text().strip(),
                connect_on_start=self.auto_connect.isChecked())
        except Exception as exc:  # noqa: BLE001
            operation_log.exception('通讯', '保存通讯设置', exc)

    def _flash(self, message: str) -> None:
        window = self.window()
        if hasattr(window, 'show_status'):
            window.show_status(message)

    def refresh_library_label(self) -> None:
        path = vendor_dir() / 'zauxdll.dll'
        self.library_label.setText(str(path) if path.is_file() else f'{path}（未安装）')

    # ---------------- session events ----------------
    def on_connection_changed(self, connected: bool) -> None:
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.state_label.setText('已连接' if connected else '未连接')
        self.state_label.setStyleSheet(
            'color:#12996a;' if connected else 'color:#8496ad;')
        if connected and self.session.link is not None:
            self.firmware_label.setText(self.session.link.firmware or '未知')
            self.address_label.setText(self.session.link.ip)
        else:
            self.firmware_label.setText('—')
            self.address_label.setText('—')

    def on_status(self, message: str) -> None:
        self.state_label.setText(message)

    # ---------------- actions ----------------
    def on_connect(self) -> None:
        mode = self.mode_combo.currentData()
        ip = self.ip_edit.text().strip()
        if mode == 'real' and not ip:
            QMessageBox.warning(self, '通讯设置', '请填写控制器 IP 地址')
            return
        self._save_connection()
        self.connect_button.setEnabled(False)
        try:
            self.session.connect(ip, mode=mode)
        except Exception as exc:  # noqa: BLE001
            operation_log.exception('通讯', '连接控制器', exc, ip=ip)
            QMessageBox.warning(self, '操作失败', f'{type(exc).__name__}: {exc}')
            self.on_connection_changed(self.session.connected)

    def on_disconnect(self) -> None:
        try:
            self.session.disconnect()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, '断开控制器', str(exc))
        self.on_connection_changed(self.session.connected)

    def on_search(self) -> None:
        """Look for controllers on the network.

        Two mechanisms are needed because neither alone is enough on this bench.
        The subnet broadcast (``ZAux_SearchEthlist``) finds a controller whose
        address is unknown, but it is unreliable on a multi-NIC workstation and
        the controller rate-limits the replies — measured on 2026-11-08 it
        answered roughly one call in eight.  A direct link (``ZAux_OpenEth``)
        answers reliably but needs the address, and it cannot be opened while
        the session already holds one.  Both are tried and the operator is told
        which one answered.

        The work runs off the GUI thread: opening a link blocks for a few
        seconds and a frozen window would look like a hung application.
        """
        ip = self.ip_edit.text().strip()
        already_connected = bool(self.session.connected)
        link = getattr(self.session, 'link', None)
        session_firmware = getattr(link, 'firmware', '') or ''

        def action() -> tuple[list[str], dict]:
            controller = ZmotionController()
            found = controller.search_list(timeout_ms=1500)
            if already_connected:
                # Opening a second link to a controller that is already linked
                # would disturb the live session, so the probe is skipped.
                probe = {'ip': ip, 'reachable': True, 'firmware': session_firmware,
                         'error': ''}
            elif ip:
                probe = probe_controller(ip, timeout=3.0)
            else:
                probe = {'ip': ip, 'reachable': False, 'firmware': '',
                         'error': '未填写 IP 地址'}
            return found, probe

        def on_done(result) -> None:
            found, probe = result
            lines = []
            if found:
                lines.append('广播搜索发现控制器：' + '、'.join(found))
            if probe.get('reachable'):
                firmware = probe.get('firmware') or '未知'
                lines.append(f'{probe.get("ip")} 可连接，固件 {firmware}')
            elif probe.get('error'):
                lines.append(f'{ip or "（未填 IP）"} 未能连接：{probe["error"]}')
            if not lines:
                lines.append('未发现控制器。请确认控制器已上电、网线连接正常，'
                             '且上位机与控制器处于同一网段。')
            QMessageBox.information(self, '搜索控制器', '\n'.join(lines))
            self.search_button.setEnabled(True)

        def on_error(exc) -> None:
            QMessageBox.warning(self, '搜索控制器', f'{type(exc).__name__}: {exc}')
            self.search_button.setEnabled(True)

        self.search_button.setEnabled(False)
        run_in_background(action, on_done=on_done, on_error=on_error,
                          description='搜索控制器')

    def on_save(self) -> None:
        self._save_connection()
        self._sync_auto_hint()
        QMessageBox.information(self, '保存成功',
                                f'设置已保存到\n{self.session.store.path}')

    def on_reveal(self) -> None:
        folder = Path(self.session.store.path).parent
        try:
            os.startfile(str(folder))  # noqa: S606 - Windows shell open
        except Exception as exc:  # noqa: BLE001
            QMessageBox.information(self, '打开文件夹', f'无法打开：{exc}')

    def on_mode_changed(self) -> None:
        mode = str(self.mode_combo.currentData())
        try:
            self.session.set_mode(mode)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, '控制器模式', str(exc))
            return
        self.ip_edit.setEnabled(mode == 'real')
        self._sync_auto_hint()

    def on_model_changed(self) -> None:
        count = int(self.model_combo.currentData())
        try:
            self.session.set_axis_count(count)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, 'tuner 型号', str(exc))


#: The shell refers to this name; both names point at the same page.
SafeCommunicationPage = CommunicationPage

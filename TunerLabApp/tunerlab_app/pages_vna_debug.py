"""SCPI debug toolbar: probe a real VNA without leaving the application.

The ``-113 Undefined header`` failure on the first Agilent session showed that
the six built-in vendor profiles cannot cover every firmware command tree.
Instead of asking the operator to run a Python script, this page runs the same
``tuner.vna_probe`` routine used by the CLI and Web API, then lets them:

* read ``*IDN?`` (model + firmware) and which SCPI commands are accepted;
* copy the whole report or export it as JSON for troubleshooting;
* **apply** the discovered dialect so subsequent connects skip the -113s.

A probe needs its own transport (VISA GPIB resources cannot be opened twice),
so the page offers to disconnect first.  All hardware work runs on the shared
thread pool; progress is polled through a lock-protected tuple, never by
touching widgets from the worker thread.
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFileDialog, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from tuner.web import handlers
from .bg import run_in_background
from .fonts import px_font

__all__ = ['VnaDebugPage']

#: Verdict -> (foreground colour, Chinese hint).  UNKNOWN is not a failure:
#: it means the instrument has no error queue to answer with.
VERDICT_STYLE = {
    'OK': ('#1e7f45', '被接受'),
    'REJECT': ('#b03a2e', '被拒绝'),
    'UNKNOWN': ('#8a5a00', '无法判定（无错误队列，请对照手册）'),
    'NO DATA': ('#5b6b7c', '无返回数据'),
    'ERROR': ('#b03a2e', '通讯异常'),
}


class VnaDebugPage(QWidget):
    """Instrument SCPI diagnostics, reachable from the 系统 navigation group."""

    def __init__(self, app_state, parent=None):
        super().__init__(parent)
        self.state = app_state
        self.result: dict[str, Any] | None = None
        self._running = False
        self._stop = threading.Event()
        self._progress_lock = threading.Lock()
        self._progress: tuple[int, int, str] = (0, 1, '待命')

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 0, 22, 18)
        root.setSpacing(10)

        root.addWidget(self._build_toolbar())
        root.addLayout(self._build_progress_row())
        root.addWidget(self._build_identity_card())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_command_table(), '命令探测结果')
        self.tabs.addTab(self._build_report_view(), '文本报告')
        root.addWidget(self.tabs, 1)

        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(150)
        self._progress_timer.timeout.connect(self._poll_progress)

        self._load_config()
        self._sync_interface()
        self._set_buttons()

    # ---------------- construction ----------------
    def _build_toolbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName('DebugToolbar')
        bar.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        title = QLabel('SCPI 调试工具栏')
        title.setFont(px_font(14, 'medium'))
        layout.addWidget(title)
        hint = QLabel(
            '连接报 -113 或换用新仪器时，探测一次即可读出型号/固件与真实命令集。'
            '探测只发查询与无害配置，会临时创建 TL_probe_* 测量并改动迹线 1，'
            '请在前面板按需恢复；GPIB 资源需要独占，探测前会先断开连接。')
        hint.setObjectName('PageHint')
        hint.setWordWrap(True)
        hint.setFont(px_font(11.5, 'light'))
        layout.addWidget(hint)

        params = QGridLayout()
        params.setHorizontalSpacing(8)
        params.setVerticalSpacing(6)
        self.interface_combo = QComboBox()
        for key, label in (handlers.vna_interfaces(self.state, {})
                           .get('interfaces', {}).items()):
            self.interface_combo.addItem(label, key)
        params.addWidget(QLabel('接口'), 0, 0)
        params.addWidget(self.interface_combo, 0, 1)

        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText('192.168.2.111')
        params.addWidget(QLabel('目标'), 0, 2)
        params.addWidget(self.target_edit, 0, 3)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(5025)
        self.port_label = QLabel('端口')
        params.addWidget(self.port_label, 0, 4)
        params.addWidget(self.port_spin, 0, 5)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(500, 30000)
        self.timeout_spin.setSingleStep(500)
        self.timeout_spin.setSuffix(' ms')
        self.timeout_spin.setValue(3000)
        self.timeout_spin.setToolTip('单条命令超时。老式 GPIB 仪器建议 2000–3000 ms。')
        params.addWidget(QLabel('单条超时'), 0, 6)
        params.addWidget(self.timeout_spin, 0, 7)
        params.setColumnStretch(3, 1)
        layout.addLayout(params)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.detect_button = QPushButton('检测 VISA')
        self.config_button = QPushButton('读取当前配置')
        self.probe_button = QPushButton('开始探测')
        self.probe_button.setProperty('accent', True)
        self.disconnect_probe_button = QPushButton('断开并探测')
        self.stop_button = QPushButton('中止')
        self.apply_button = QPushButton('应用探测到的方言')
        self.copy_button = QPushButton('复制报告')
        self.export_button = QPushButton('导出 JSON')
        self.clear_button = QPushButton('清空')
        for widget in (self.detect_button, self.config_button,
                       self.probe_button, self.disconnect_probe_button,
                       self.stop_button, self.apply_button, self.copy_button,
                       self.export_button, self.clear_button):
            widget.setMinimumHeight(30)
            buttons.addWidget(widget)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.detect_button.clicked.connect(self.on_detect_visa)
        self.config_button.clicked.connect(self._load_config)
        self.probe_button.clicked.connect(lambda: self.on_probe(False))
        self.disconnect_probe_button.clicked.connect(lambda: self.on_probe(True))
        self.stop_button.clicked.connect(self.on_stop)
        self.apply_button.clicked.connect(self.on_apply_dialect)
        self.copy_button.clicked.connect(self.on_copy_report)
        self.export_button.clicked.connect(self.on_export_json)
        self.clear_button.clicked.connect(self.on_clear)
        self.interface_combo.currentIndexChanged.connect(self._sync_interface)
        return bar

    def _build_progress_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(12)
        row.addWidget(self.progress, 1)
        self.progress_label = QLabel('待命')
        self.progress_label.setObjectName('PageHint')
        self.progress_label.setFont(px_font(11.5, 'light'))
        self.progress_label.setMinimumWidth(280)
        row.addWidget(self.progress_label)
        self.dialect_label = QLabel('当前固定方言：无（按厂商预设自动协商）')
        self.dialect_label.setObjectName('PageHint')
        self.dialect_label.setFont(px_font(11.5, 'light'))
        row.addWidget(self.dialect_label)
        return row

    def _build_identity_card(self) -> QGroupBox:
        box = QGroupBox('仪器身份与能力')
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(12)
        self.idn_label = QLabel('尚未探测')
        self.idn_label.setWordWrap(True)
        self.idn_label.setFont(px_font(12, 'medium'))
        grid.addWidget(QLabel('*IDN?'), 0, 0)
        grid.addWidget(self.idn_label, 0, 1)
        self.model_label = QLabel('—')
        self.model_label.setWordWrap(True)
        grid.addWidget(QLabel('型号 / 固件'), 1, 0)
        grid.addWidget(self.model_label, 1, 1)
        self.caps_label = QLabel('—')
        self.caps_label.setWordWrap(True)
        grid.addWidget(QLabel('SCPI 能力'), 2, 0)
        grid.addWidget(self.caps_label, 2, 1)
        self.verdict_label = QLabel('—')
        self.verdict_label.setWordWrap(True)
        self.verdict_label.setFont(px_font(11.5, 'medium'))
        grid.addWidget(QLabel('结论'), 3, 0)
        grid.addWidget(self.verdict_label, 3, 1)
        grid.setColumnStretch(1, 1)
        return box

    def _build_command_table(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(6, 6, 6, 6)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ['分组', '判定', '命令', '说明', '错误队列', '返回值'])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        # The shell wraps this page in a QScrollArea; without an explicit
        # minimum the table collapses to a few rows at 1140x720.
        self.table.setMinimumHeight(300)
        header = self.table.horizontalHeader()
        for col in (2, 3, 4, 5):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.table_note = QLabel(
            '判定 OK = 仪器接受该命令；REJECT = 报 -113 等错误；UNKNOWN = 仪器没有错误队列，需对照手册。')
        self.table_note.setObjectName('PageHint')
        self.table_note.setWordWrap(True)
        layout.addWidget(self.table_note)
        return widget

    def _build_report_view(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(6, 6, 6, 6)
        self.report_view = QPlainTextEdit()
        self.report_view.setReadOnly(True)
        self.report_view.setMinimumHeight(300)
        self.report_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.report_view.setFont(px_font(11.5, 'regular'))
        self.report_view.setPlainText('尚未运行探测。')
        layout.addWidget(self.report_view)
        return widget

    # ---------------- form ----------------
    def _sync_interface(self) -> None:
        gpib = self.interface_combo.currentData() == 'gpib'
        self.port_label.setVisible(not gpib)
        self.port_spin.setVisible(not gpib)
        self.target_edit.setPlaceholderText(
            'GPIB0::17::INSTR（也可只填主地址 17）' if gpib else '192.168.2.111')
        # Switching interfaces must not leave an IP in the GPIB box (or vice
        # versa); otherwise the first probe would try to open "192.168..." as
        # a VISA resource and fail with a confusing message.
        cfg = self.state.vna.get('config', {}) if hasattr(self, 'state') else {}
        text = self.target_edit.text().strip() if hasattr(self, 'target_edit') else ''
        if not hasattr(self, 'target_edit'):
            return
        if gpib and (not text or re.match(r'^\d+\.\d+', text)):
            self.target_edit.setText(str(cfg.get('resource') or 'GPIB0::17::INSTR'))
        elif not gpib and (not text or text.upper().startswith('GPIB')):
            self.target_edit.setText(str(cfg.get('host') or '192.168.2.111'))

    def _load_config(self) -> None:
        cfg = self.state.vna.get('config', {})
        iface = str(cfg.get('interface') or 'tcpip')
        index = self.interface_combo.findData(iface)
        if index >= 0:
            self.interface_combo.setCurrentIndex(index)
        self.target_edit.setText(str(
            cfg.get('resource') or 'GPIB0::17::INSTR') if iface == 'gpib'
            else str(cfg.get('host') or '192.168.2.111'))
        self.port_spin.setValue(int(cfg.get('port') or 5025))
        dialect = cfg.get('dialect') or {}
        self.dialect_label.setText(
            '当前固定方言：' + '，'.join(f'{k}={v}' for k, v in dialect.items())
            if dialect else '当前固定方言：无（按厂商预设自动协商）')

    def _body(self) -> dict[str, Any]:
        gpib = self.interface_combo.currentData() == 'gpib'
        target = self.target_edit.text().strip()
        return {
            'interface': 'gpib' if gpib else 'tcpip',
            'host': '' if gpib else target,
            'port': self.port_spin.value(),
            'resource': target if gpib else '',
            'timeout_ms': self.timeout_spin.value(),
        }

    def _set_buttons(self) -> None:
        running = self._running
        has_result = self.result is not None
        self.probe_button.setEnabled(not running)
        self.disconnect_probe_button.setEnabled(not running)
        self.detect_button.setEnabled(not running)
        self.config_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.apply_button.setEnabled(not running and has_result)
        self.copy_button.setEnabled(not running and has_result)
        self.export_button.setEnabled(not running and has_result)
        self.clear_button.setEnabled(not running)
        self.probe_button.setText('探测中…' if running else '开始探测')
        self.report(f'SCPI 探测进行中：{self.target_edit.text().strip()}'
                    if running else 'SCPI 调试工具就绪')

    def report(self, message: str) -> None:
        window = self.window()
        if hasattr(window, 'show_status'):
            window.show_status(message)

    # ---------------- progress (worker -> UI) ----------------
    def _on_step(self, section: str, index: int, total: int, message: str) -> None:
        with self._progress_lock:
            self._progress = (index, total, f'{section}：{message}')

    def _poll_progress(self) -> None:
        with self._progress_lock:
            index, total, text = self._progress
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(min(max(1, total), index + 1))
        self.progress_label.setText(text)

    # ---------------- actions ----------------
    def on_probe(self, disconnect_first: bool) -> None:
        if self._running:
            return
        body = self._body()
        if body['interface'] == 'gpib':
            from tuner.vna_drivers import normalise_gpib_resource
            if not body['resource']:
                QMessageBox.warning(self, 'SCPI 探测', '请填写 GPIB 地址或 VISA 资源名')
                return
            try:
                body['resource'] = normalise_gpib_resource(body['resource'])
            except ValueError as exc:
                QMessageBox.warning(self, 'SCPI 探测', str(exc))
                return
            self.target_edit.setText(body['resource'])
        elif not body['host']:
            QMessageBox.warning(self, 'SCPI 探测', '请填写仪器 IP 地址')
            return

        if bool(self.state.vna.get('connected')) and not disconnect_first:
            answer = QMessageBox.question(
                self, 'SCPI 探测',
                '矢网当前处于连接状态。GPIB 资源不能被两个会话同时打开，\n'
                '需要先断开连接再探测。是否断开并继续？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                return
            disconnect_first = True

        self._stop.clear()
        self._running = True
        self.progress.setValue(0)
        self.progress_label.setText('正在连接仪器…')
        self._set_buttons()
        self._progress_timer.start()

        def work():
            if disconnect_first:
                handlers.vna_disconnect(self.state, {})
            return handlers.vna_probe(self.state, body,
                                      should_stop=self._stop.is_set,
                                      on_step=self._on_step)

        run_in_background(work, on_done=self._finished, on_error=self._failed,
                          description='vna scpi probe')

    def on_stop(self) -> None:
        if not self._running:
            return
        self._stop.set()
        self.progress_label.setText('已请求中止，等待当前命令超时后返回…')
        self.stop_button.setEnabled(False)

    def _finished(self, result: dict) -> None:
        self._running = False
        self._progress_timer.stop()
        self.result = result
        self._render(result)
        self._set_buttons()
        self.progress_label.setText(
            '探测已中止（结果不完整）' if result.get('aborted') else '探测完成')
        self.report(f"SCPI 探测完成：{result.get('idn') or '未读到 IDN'}")
        if result.get('aborted'):
            QMessageBox.information(self, 'SCPI 探测', '探测已中止，表格中只包含已完成的部分。')

    def _failed(self, exc: BaseException) -> None:
        self._running = False
        self._progress_timer.stop()
        self._set_buttons()
        self.progress_label.setText('探测失败')
        QMessageBox.warning(self, 'SCPI 探测失败', f'{type(exc).__name__}: {exc}')
        self.report(f'SCPI 探测失败: {exc}')

    def on_apply_dialect(self) -> None:
        dialect = (self.result or {}).get('suggested_dialect') or {}
        if not dialect:
            QMessageBox.information(
                self, '应用方言',
                '本次探测没有可直接应用的命令组合（可能取数/功率命令不同）。\n'
                '请把报告发回，以便按型号新增专用 profile。')
            return
        result = handlers.vna_apply_dialect(self.state, {'dialect': dialect})
        self._load_config()
        if result.get('persisted'):
            persist_note = '命令组合已写入设置文件，重启应用后自动生效。'
        else:
            persist_note = ('注意：未能写入设置文件'
                            + (f'（{result.get("persist_error")}）'
                               if result.get('persist_error') else '')
                            + '，重启应用后需重新应用。')
        QMessageBox.information(
            self, '应用方言',
            '已固定以下 SCPI 命令，下次「连接并配置」直接使用：\n\n' +
            '\n'.join(f'{k} = {v}' for k, v in result['dialect'].items()) +
            f'\n\n{persist_note}')
        self.report('已应用探测到的 SCPI 方言')

    def on_copy_report(self) -> None:
        text = (self.result or {}).get('report') or self.report_view.toPlainText()
        QApplication.clipboard().setText(text)
        self.report('SCPI 探测报告已复制到剪贴板')

    def on_export_json(self) -> None:
        if not self.result:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, '导出 SCPI 探测结果',
            'vna_scpi_probe.json', 'JSON 文件 (*.json);;所有文件 (*.*)')
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(self.result, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            QMessageBox.warning(self, '导出失败', str(exc))
            return
        self.report(f'SCPI 探测结果已导出：{path}')

    def on_clear(self) -> None:
        self.result = None
        self.table.setRowCount(0)
        self.report_view.setPlainText('尚未运行探测。')
        self.idn_label.setText('尚未探测')
        self.model_label.setText('—')
        self.caps_label.setText('—')
        self.verdict_label.setText('—')
        self.verdict_label.setStyleSheet('')
        self.progress.setValue(0)
        self.progress_label.setText('待命')
        self._set_buttons()

    def on_detect_visa(self) -> None:
        self.detect_button.setEnabled(False)
        self.detect_button.setText('检测中…')

        def restore(info: Any = None) -> None:
            self.detect_button.setEnabled(not self._running)
            self.detect_button.setText('检测 VISA')

        def failed(exc: BaseException) -> None:
            restore()
            QMessageBox.warning(self, 'VISA 检测失败', f'{type(exc).__name__}: {exc}')

        def done(info: dict) -> None:
            restore()
            gpib = [r for r in info.get('resources', [])
                    if str(r).upper().startswith('GPIB')]
            if gpib:
                self.interface_combo.setCurrentIndex(
                    max(0, self.interface_combo.findData('gpib')))
                self.target_edit.setText(gpib[0])
                self.report(f'VISA 后端 {info.get("backend")} · GPIB 资源 {", ".join(gpib)}')
            else:
                QMessageBox.information(
                    self, 'VISA 资源',
                    f'VISA 后端：{info.get("backend") or "—"}\n\n未发现 GPIB 资源。'
                    '请确认 82357B 已插入、仪器已上电，并在 Connection Expert 中可见。')

        run_in_background(lambda: handlers.vna_visa_resources(self.state, {}),
                          on_done=done, on_error=failed,
                          description='visa resources')

    # ---------------- rendering ----------------
    def _render(self, result: dict) -> None:
        fields = result.get('idn_fields') or {}
        self.idn_label.setText(result.get('idn') or '<无响应>')
        if fields.get('model'):
            self.model_label.setText(
                f"{fields['manufacturer']} {fields['model']} · "
                f"固件 {fields['firmware']} · 序列号 {fields['serial']}")
        else:
            self.model_label.setText('IDN 不是标准四段格式，请把原文发回')
        caps = result.get('capabilities') or {}
        self.caps_label.setText(
            f"*CLS {'有' if caps.get('cls') else '无'} · "
            f":SYST:ERR? {'有' if caps.get('error_queue') else '无'} · "
            f"测量目录回读 {'有' if caps.get('readback') else '无'}"
            + ('；' + '；'.join(caps.get('notes', [])) if caps.get('notes') else ''))

        summary = result.get('summary') or []
        self.verdict_label.setText('\n'.join(summary))
        accepted = result.get('accepted_define') or []
        self.verdict_label.setStyleSheet(
            'color:#1e7f45;' if accepted else 'color:#b03a2e;')

        rows: list[tuple[str, dict]] = (
            [('测量定义', r) for r in result.get('define', [])] +
            [('其他命令', r) for r in result.get('other', [])])
        self.table.setRowCount(len(rows))
        for row, (group, item) in enumerate(rows):
            verdict = str(item.get('verdict') or '')
            values = [group, verdict, str(item.get('cmd') or ''),
                      str(item.get('note') or ''), str(item.get('error') or ''),
                      str(item.get('value') or '')]
            for col, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setFont(px_font(11.5, 'medium' if col == 1 else 'regular'))
                if col == 1 and verdict in VERDICT_STYLE:
                    cell.setForeground(QColor(VERDICT_STYLE[verdict][0]))
                    cell.setToolTip(VERDICT_STYLE[verdict][1])
                if col in (0, 1):
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignHCenter
                                          | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, col, cell)
        self.table.resizeRowsToContents()

        self.report_view.setPlainText(
            result.get('report') or json.dumps(result, ensure_ascii=False, indent=2))
        self.tabs.setCurrentIndex(0)

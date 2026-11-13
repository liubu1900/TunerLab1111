"""VNA control: four independent traces and a sweep that matches its numbers.

Two long-standing problems are fixed here:

**1. One plot for four S-parameters.**  Overlaying S11/S21/S12/S22 on a single
axis forces one shared dB scale, and |S21| (0…-80 dB) flattens |S11| (≈ -15 dB)
into a straight line.  A real instrument shows one window per trace; this page
does the same, each with its own autoscale, its own minimum marker and a data
cursor that reads frequency and level under the mouse.

**2. The step shown was not the step swept.**  The old page computed
``(stop - start) / (points - 1)`` purely for display: editing it changed nothing,
and a scan reused whatever the instrument had been configured with.  Now points
and step are two views of one span and stay consistent:

* editing the step recomputes the point count;
* editing the point count recomputes the step.

**Instrument settings are read, not overwritten (0.5.13.3).**  Connecting only
opens the SCPI session, and 单次扫描 only triggers and reads: neither writes
start/stop/points/power/IFBW.  The analyzer's own setup is queried read-only so
the returned frequency axis matches its trace.  The sweep form is locked against
accidental edits (click a box to unlock it) and its values reach the instrument
only through 下发扫描参数.  从矢网读取当前参数 refreshes the form from the box.
"""
from __future__ import annotations

from typing import Any

import numpy as np
from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .fonts import px_font
from tuner.web import handlers
from .bg import run_in_background
from .widgets import StatusPill, TracePlot

__all__ = ['VnaPage']

#: S-parameters in the order a 2-port instrument displays them.
S_PARAMETERS = ('S11', 'S21', 'S12', 'S22')

#: Rows kept in the table; a 2000-point sweep is summarised, not dumped.
MAX_TABLE_ROWS = 400

#: The button only opens the SCPI session now; the label must not promise a
#: configuration that would silently overwrite the operator's front panel.
CONNECT_LABEL = '连接'
READ_CONFIG_LABEL = '从矢网读取当前参数'
APPLY_LABEL = '下发扫描参数'

#: Subtle tint used while a sweep field is locked (light instrument theme).
LOCKED_BASE = QColor('#eaf0f6')


class _ClickToEdit(QObject):
    """Keep a spin box read-only until the operator explicitly clicks into it.

    A stray wheel scroll or key press used to change the sweep settings without
    anybody noticing.  ``setReadOnly`` blocks keyboard, wheel and arrow input;
    a mouse press on the box (or its line edit) unlocks it, and leaving the
    field locks it again.  Programmatic ``setValue`` still works while locked,
    so reading the instrument's config can refresh the form without exposing
    it to accidental edits.
    """

    def __init__(self, spin, parent=None):
        super().__init__(spin if parent is None else parent)
        self._spin = spin
        self._locked = True
        self._base = spin.palette().color(QPalette.ColorRole.Base)
        spin.setReadOnly(True)
        spin.setToolTip('默认锁定；用鼠标点一下即可修改，移开焦点后自动重新锁定')
        spin.installEventFilter(self)
        line = spin.lineEdit()
        if line is not None:
            line.installEventFilter(self)
        self._tint(True)

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress:
            self.unlock()
        elif kind == QEvent.Type.FocusOut and watched is not self._spin:
            self.lock()
        return super().eventFilter(watched, event)

    def _tint(self, locked: bool) -> None:
        palette = self._spin.palette()
        palette.setColor(QPalette.ColorRole.Base,
                         LOCKED_BASE if locked else self._base)
        self._spin.setPalette(palette)

    def unlock(self) -> None:
        if not self._locked:
            return
        self._locked = False
        self._spin.setReadOnly(False)
        self._tint(False)

    def lock(self) -> None:
        if self._locked:
            return
        self._locked = True
        self._spin.setReadOnly(True)
        self._tint(True)

    @property
    def locked(self) -> bool:
        return self._locked


class VnaPage(QWidget):
    """Instrument connection, sweep setup and four-trace acquisition."""

    #: (connected, host, idn) whenever the instrument state is re-read, so the
    #: shell's top bar can show the VNA link next to the controller link.
    status_changed = Signal(bool, str, str)

    def __init__(self, app_state, parent=None):
        super().__init__(parent)
        self.state = app_state
        cfg = self.state.vna['config']
        self._syncing = False
        self._loading = False
        self._last_scan_step_hz = 0.0
        self._is_connected = False
        self._param_busy = False
        self._scan_busy = False
        self._param_locks: list[_ClickToEdit] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 0, 22, 18)
        root.setSpacing(10)


        # A QSplitter scaled the control column below its minimum on a 1440 px
        # window (372 px requested, 265 px drawn, spin boxes clipped).  A fixed
        # rail plus an expanding work area is deterministic at every width.
        body = QHBoxLayout()
        body.setSpacing(0)
        root.addLayout(body, 1)

        # ---------------- left: controls ----------------
        controls = QWidget()
        controls.setFixedWidth(330)
        controls.setMinimumHeight(1)
        left = QVBoxLayout(controls)
        left.setContentsMargins(0, 0, 8, 0)
        left.setSpacing(10)

        left.addWidget(self._build_connection_box(cfg))
        left.addWidget(self._build_sweep_box(cfg))
        left.addWidget(self._build_status_box())
        left.addStretch()

        # The rail is taller than the window at 900 px: without its own scroll
        # area every card was squeezed until the spin boxes had no text height.
        rail = QScrollArea()
        rail.setWidgetResizable(True)
        rail.setFrameShape(QFrame.Shape.NoFrame)
        rail.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rail.setFixedWidth(346)
        rail.setWidget(controls)
        body.addWidget(rail)

        # ---------------- right: traces + table ----------------
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(8)
        right_layout.addLayout(self._build_trace_toolbar())

        self.stale_label = QLabel('')
        self.stale_label.setWordWrap(True)
        self.stale_label.setVisible(False)
        right_layout.addWidget(self.stale_label)

        self.trace_grid = QGridLayout()
        self.trace_grid.setSpacing(8)
        self.traces: dict[str, TracePlot] = {}
        for index, name in enumerate(S_PARAMETERS):
            plot = TracePlot(name)
            plot.cursor_moved.connect(self._on_trace_cursor)
            self.traces[name] = plot
            self.trace_grid.addWidget(plot, index // 2, index % 2)
            self.trace_grid.setRowStretch(index // 2, 1)
        for column in range(2):
            self.trace_grid.setColumnStretch(column, 1)
        right_layout.addLayout(self.trace_grid, 1)

        self.summary_label = QLabel('尚未扫描')
        self.summary_label.setObjectName('PageHint')
        self.summary_label.setWordWrap(True)
        self.summary_label.setFont(px_font(11.5, 'light'))
        right_layout.addWidget(self.summary_label)

        self.table_note = QLabel('')
        self.table_note.setObjectName('PageHint')
        self.table_note.setWordWrap(True)
        right_layout.addWidget(self.table_note)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ['频率 (GHz)', 'Δf (MHz)', '|S11| dB', '|S21| dB', '|S12| dB', '|S22| dB'])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(96)
        self.table.setMaximumHeight(180)
        right_layout.addWidget(self.table)
        body.addWidget(right, 1)


        self.connect_button.clicked.connect(self.on_connect)
        self.disconnect_button.clicked.connect(self.on_disconnect)
        self.apply_button.clicked.connect(self.on_apply_sweep)
        self.read_button.clicked.connect(self.on_read_config)
        self.scan_button.clicked.connect(self.on_scan)
        self.errors_button.clicked.connect(self.on_read_errors)
        self.clear_button.clicked.connect(self.on_clear)
        self.interface_combo.currentIndexChanged.connect(self._sync_interface)
        self.detect_button.clicked.connect(self.on_detect_visa)

        # Moving from one trace to the next emits leave-then-move; a short delay
        # keeps the shared cursor from blinking off between the two events.
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setSingleShot(True)
        self._cursor_timer.setInterval(60)
        self._cursor_timer.timeout.connect(self._clear_trace_cursor)

        self._load_config_into_form(cfg)
        self._sync_interface()
        self.points_spin.valueChanged.connect(self._update_step_from_points)
        self.step_spin.valueChanged.connect(self._update_points_from_step)
        self._update_step_from_points()
        # Lock every sweep field against accidental edits.  A mouse click
        # unlocks one field until it loses focus; apply / read-back re-lock all.
        self._param_locks = [_ClickToEdit(spin) for spin in (
            self.start_spin, self.stop_spin, self.points_spin,
            self.step_spin, self.power_spin, self.ifbw_spin)]
        self.refresh_status()

        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start()

    # ---------------- construction ----------------
    def _build_connection_box(self, cfg: dict) -> QGroupBox:
        box = QGroupBox('仪器连接')
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.interface_combo = QComboBox()
        for key, label in (handlers.vna_interfaces(self.state, {})
                           .get('interfaces', {}).items()):
            self.interface_combo.addItem(label, key)
        form.addRow('接口', self.interface_combo)

        self.vendor_combo = QComboBox()
        for key, label in handlers.vna_vendors(self.state, {}).get('vendors', {}).items():
            self.vendor_combo.addItem(label, key)
        self.vendor_combo.insertItem(0, '自动识别 (*IDN?)', 'auto')
        form.addRow('厂商', self.vendor_combo)

        self.host_label = QLabel('IP 地址')
        self.host_edit = QLineEdit(str(cfg['host']))
        self.host_edit.setPlaceholderText('192.168.2.111')
        form.addRow(self.host_label, self.host_edit)

        self.port_label = QLabel('端口')
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(int(cfg['port']))
        form.addRow(self.port_label, self.port_spin)

        self.resource_label = QLabel('VISA 资源')
        self.resource_edit = QLineEdit(str(cfg.get('resource') or 'GPIB0::17::INSTR'))
        self.resource_edit.setPlaceholderText('GPIB0::17::INSTR')
        self.resource_edit.setToolTip(
            '填仪器 GPIB 主地址（如 17）或完整 VISA 资源名（如 GPIB0::17::INSTR）。\n'
            'Agilent/Keysight 82357B USB-GPIB 卡需要安装 Keysight IO Libraries Suite。')
        self.detect_button = QPushButton('检测 VISA')
        self.detect_button.setToolTip('读取 VISA 运行时实际可见的 GPIB 资源')
        resource_row = QHBoxLayout()
        resource_row.setContentsMargins(0, 0, 0, 0)
        resource_row.addWidget(self.resource_edit, 1)
        resource_row.addWidget(self.detect_button)
        self.resource_field = QWidget()
        self.resource_field.setLayout(resource_row)
        form.addRow(self.resource_label, self.resource_field)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(500, 120000)
        self.timeout_spin.setSingleStep(500)
        self.timeout_spin.setSuffix(' ms')
        self.timeout_spin.setValue(int(cfg['timeout_ms']))
        form.addRow('超时', self.timeout_spin)

        row = QHBoxLayout()
        self.connect_button = QPushButton(CONNECT_LABEL)
        self.connect_button.setProperty('accent', True)
        self.connect_button.setToolTip(
            '只建立通信连接并读取仪器当前设置；不改变扫频、功率或中频带宽。\n'
            '如需修改仪器参数，请点「下发扫描参数」。')
        self.disconnect_button = QPushButton('断开')
        self.disconnect_button.setEnabled(False)
        row.addWidget(self.connect_button)
        row.addWidget(self.disconnect_button)
        form.addRow('', row)
        return box

    def _build_sweep_box(self, cfg: dict) -> QGroupBox:
        box = QGroupBox('扫描参数')
        form = QFormLayout(box)
        # Label above field: a right-aligned label column left the spin boxes too
        # narrow to show "20.0000 GHz" plus its arrows inside a 412 px rail.
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self.start_spin = self._freq_spin(cfg['start_hz'])
        self.stop_spin = self._freq_spin(cfg['stop_hz'])
        form.addRow('起始频率', self.start_spin)
        form.addRow('终止频率', self.stop_spin)

        self.points_spin = QSpinBox()
        self.points_spin.setRange(2, 20001)
        self.points_spin.setSingleStep(10)
        self.points_spin.setValue(int(cfg['points']))
        form.addRow('点数', self.points_spin)

        self.step_spin = QDoubleSpinBox()
        self.step_spin.setRange(0.0001, 10000.0)
        self.step_spin.setDecimals(4)
        self.step_spin.setSuffix(' MHz')
        self.step_spin.setKeyboardTracking(False)
        self.step_spin.setToolTip('与点数联动：改步进会重算点数，改点数会重算步进。')
        form.addRow('步进', self.step_spin)

        self.span_label = QLabel('—')
        self.span_label.setObjectName('PageHint')
        self.span_label.setFont(px_font(11.5, 'light'))
        # The span text is longer than the 330 px control column; without
        # wrapping it is clipped instead of shown on two lines.
        self.span_label.setWordWrap(True)
        form.addRow('', self.span_label)

        self.power_spin = QDoubleSpinBox()
        self.power_spin.setRange(-80.0, 30.0)
        self.power_spin.setDecimals(2)
        self.power_spin.setSuffix(' dBm')
        self.power_spin.setValue(float(cfg['power_dbm']))
        form.addRow('源功率', self.power_spin)

        self.ifbw_spin = QDoubleSpinBox()
        self.ifbw_spin.setRange(1.0, 1e7)
        self.ifbw_spin.setDecimals(0)
        self.ifbw_spin.setSuffix(' Hz')
        self.ifbw_spin.setValue(float(cfg['ifbw_hz']))
        form.addRow('中频带宽', self.ifbw_spin)

        self.lock_hint = QLabel(
            '参数默认锁定（浅色底）：鼠标点一下输入框才能修改，移开焦点自动重新锁定。\n'
            '只有点「下发扫描参数」才会写入仪器。')
        self.lock_hint.setObjectName('PageHint')
        self.lock_hint.setWordWrap(True)
        self.lock_hint.setFont(px_font(11, 'light'))
        form.addRow('', self.lock_hint)

        self.read_button = QPushButton(READ_CONFIG_LABEL)
        self.read_button.setToolTip(
            '只读查询矢网当前的起止频率 / 点数 / 功率 / 中频带宽并回填表单，'
            '不修改仪器任何设置')
        self.read_button.setEnabled(False)
        form.addRow('', self.read_button)

        self.apply_button = QPushButton(APPLY_LABEL)
        self.apply_button.setToolTip(
            '把表单中的参数写入矢网；连接和单次扫描都不会写入任何参数')
        self.apply_button.setEnabled(False)
        form.addRow('', self.apply_button)
        return box

    def _build_status_box(self) -> QGroupBox:
        box = QGroupBox('仪器状态')
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.vendor_label = QLabel('—')
        self.idn_label = QLabel('—')
        self.idn_label.setWordWrap(True)
        self.idn_label.setFont(px_font(11, 'medium'))
        self.error_label = QLabel('—')
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet('color:#b03a2e;')
        form.addRow('识别厂商', self.vendor_label)
        form.addRow('IDN', self.idn_label)
        form.addRow('最近错误', self.error_label)
        return box

    def _build_trace_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.scan_button = QPushButton('单次扫描（4 路）')
        self.scan_button.setProperty('accent', True)
        self.scan_button.setEnabled(False)
        self.errors_button = QPushButton('读取错误队列')
        self.errors_button.setEnabled(False)
        self.clear_button = QPushButton('清除迹线')
        self.scan_button.setMinimumHeight(30)
        self.scan_button.setMinimumWidth(140)
        self.errors_button.setMinimumHeight(30)
        self.clear_button.setMinimumHeight(30)
        row.addWidget(self.scan_button)
        row.addWidget(self.errors_button)
        row.addWidget(self.clear_button)
        row.addStretch()
        self.cursor_hint = QLabel('悬停任一 Trace：四路同步显示该频点的读数')
        self.cursor_hint.setObjectName('PageHint')
        self.cursor_hint.setWordWrap(True)
        row.addWidget(self.cursor_hint)
        return row

    # ---------------- linked cursor ----------------
    def _on_trace_cursor(self, freq) -> None:
        """Mirror the hovered frequency onto all four traces.

        Each trace still reads its own curve at that frequency, so a single
        vertical line answers "what are S11/S21/S12/S22 at 2 GHz" at once.
        """
        if freq is None:
            self._cursor_timer.start()
            return
        self._cursor_timer.stop()
        for plot in self.traces.values():
            plot.set_cursor_freq(float(freq))

    def _clear_trace_cursor(self) -> None:
        for plot in self.traces.values():
            plot.set_cursor_freq(None)

    def _freq_spin(self, value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.000001, 200.0)
        # 4 decimals = 0.1 MHz, enough for a 0.4–20 GHz sweep and narrow enough
        # that the value plus its suffix fits the control column.
        spin.setDecimals(4)
        spin.setSuffix(' GHz')
        spin.setKeyboardTracking(False)
        spin.setValue(float(value) / 1e9)
        spin.valueChanged.connect(self._update_step_from_points)
        return spin

    # ---------------- interface switching ----------------
    def _sync_interface(self) -> None:
        """Show TCP/IP fields or the VISA resource field, never both."""
        gpib = self.interface_combo.currentData() == 'gpib'
        for widget in (self.host_label, self.host_edit,
                       self.port_label, self.port_spin):
            widget.setVisible(not gpib)
        self.resource_label.setVisible(gpib)
        self.resource_field.setVisible(gpib)

    def on_detect_visa(self) -> None:
        """Read the GPIB resources the installed VISA backend can actually see."""
        self.detect_button.setEnabled(False)
        self.detect_button.setText('检测中…')
        run_in_background(lambda: handlers.vna_visa_resources(self.state, {}),
                          on_done=self._visa_detected, on_error=self._visa_failed,
                          description='visa resources')

    def _restore_detect_button(self) -> None:
        self.detect_button.setEnabled(True)
        self.detect_button.setText('检测 VISA')

    def _visa_detected(self, result: dict) -> None:
        self._restore_detect_button()
        backend = result.get('backend') or 'VISA'
        gpib = [r for r in result.get('resources', [])
                if str(r).upper().startswith('GPIB')]
        if gpib:
            self.resource_edit.setText(gpib[0])
            self.report(f'VISA 后端 {backend} · GPIB 资源 {", ".join(gpib)}')
            if len(gpib) > 1:
                QMessageBox.information(
                    self, 'VISA 资源',
                    '检测到多个 GPIB 资源：\n' + '\n'.join(gpib) +
                    '\n\n已填入第一个，可在资源框中手动修改。')
        else:
            QMessageBox.information(
                self, 'VISA 资源',
                f'VISA 后端：{backend}\n\n未发现 GPIB 资源。请确认 82357B 已插入、'
                '仪器已上电，并在 Keysight Connection Expert 中能看到该仪器。')

    def _visa_failed(self, exc: BaseException) -> None:
        self._restore_detect_button()
        QMessageBox.warning(self, 'VISA 检测失败', f'{type(exc).__name__}: {exc}')
        self.report(f'VISA 检测失败: {exc}')

    # ---------------- sweep model ----------------
    def _load_config_into_form(self, cfg: dict) -> None:
        self.vendor_combo.setCurrentIndex(
            max(0, self.vendor_combo.findData(str(cfg.get('vendor_key') or 'auto'))))
        self.interface_combo.setCurrentIndex(
            max(0, self.interface_combo.findData(
                str(cfg.get('interface') or 'tcpip'))))
        self.resource_edit.setText(str(cfg.get('resource') or 'GPIB0::17::INSTR'))
        self._loading = True
        try:
            self.start_spin.setValue(float(cfg['start_hz']) / 1e9)
            self.stop_spin.setValue(float(cfg['stop_hz']) / 1e9)
        finally:
            self._loading = False

    def _span_hz(self) -> float:
        return (self.stop_spin.value() - self.start_spin.value()) * 1e9

    def _update_step_from_points(self) -> None:
        """Point count is authoritative: recompute the step it implies."""
        if self._syncing:
            return
        span = self._span_hz()
        points = max(2, self.points_spin.value())
        step = span / (points - 1) if span > 0 else 0.0
        self._syncing = True
        self.step_spin.setValue(step / 1e6)
        self._syncing = False
        self._update_span_label(step)
        self._mark_stale()

    def _update_points_from_step(self) -> None:
        """Step edited by the operator: keep the span, recompute the points."""
        if self._syncing:
            return
        span = self._span_hz()
        step = self.step_spin.value() * 1e6
        if span <= 0 or step <= 0:
            self._update_span_label(0.0)
            return
        points = int(round(span / step)) + 1
        points = max(2, min(20001, points))
        self._syncing = True
        self.points_spin.setValue(points)
        self._syncing = False
        actual = span / (points - 1)
        # Report the step the hardware will really use, not the typed one.
        if abs(actual - step) > 1e-3:
            self._syncing = True
            self.step_spin.setValue(actual / 1e6)
            self._syncing = False
        self._update_span_label(actual)
        self._mark_stale()

    def _update_span_label(self, step_hz: float) -> None:
        span = self._span_hz()
        if span <= 0:
            self.span_label.setText('终止频率必须大于起始频率')
            self.span_label.setStyleSheet('color:#b03a2e;')
            return
        self.span_label.setText(
            f'跨度 {span / 1e9:.4f} GHz · {self.points_spin.value()} 点 · '
            f'步进 {step_hz / 1e6:.4f} MHz')
        if hasattr(self, 'kpi_points'):
            self.kpi_points.set_value(str(self.points_spin.value()),
                                      f'{self.start_spin.value():.3f}–'
                                      f'{self.stop_spin.value():.3f} GHz')
            self.kpi_step.set_value(f'{step_hz / 1e6:.4f} MHz', '设定步进')
        self.span_label.setStyleSheet('color:#5b6b7c;')

    def _mark_stale(self, text: str | None = None) -> None:
        """Warn that the trace on screen no longer matches the instrument."""
        if self._loading or not any(plot.has_data()
                                    for plot in self.traces.values()):
            return
        self.stale_label.setText(text or (
            '⚠ 表单参数已修改但尚未下发：仪器仍按它自己的设置工作。\n'
            '点「下发扫描参数」写入仪器，再点「单次扫描」刷新曲线。'))
        self.stale_label.setStyleSheet(
            'color:#8a5a00; background:#fff6e0; border:1px solid #f0d9a8;'
            'border-radius:4px; padding:5px 8px;')
        self.stale_label.setVisible(True)

    # ---------------- body ----------------
    def _target_text(self) -> str:
        """What the top-bar badge should show for the active interface."""
        if self.interface_combo.currentData() == 'gpib':
            return self.resource_edit.text().strip()
        return self.host_edit.text().strip()

    def _body(self) -> dict[str, Any]:
        return {
            'vendor_key': self.vendor_combo.currentData(),
            'interface': self.interface_combo.currentData(),
            'host': self.host_edit.text().strip(),
            'port': self.port_spin.value(),
            'resource': self.resource_edit.text().strip(),
            'timeout_ms': self.timeout_spin.value(),
            'start_hz': self.start_spin.value() * 1e9,
            'stop_hz': self.stop_spin.value() * 1e9,
            'points': self.points_spin.value(),
            'step_hz': self.step_spin.value() * 1e6,
            'power_dbm': self.power_spin.value(),
            'ifbw_hz': self.ifbw_spin.value(),
        }

    # ---------------- reporting ----------------
    def report(self, message: str) -> None:
        window = self.window()
        if hasattr(window, 'show_status'):
            window.show_status(message)

    def report_error(self, exc: BaseException) -> None:
        QMessageBox.warning(self, '矢网操作失败', f'{type(exc).__name__}: {exc}')
        self.report(f'矢网操作失败: {exc}')

    # ---------------- actions ----------------
    def _set_param_busy(self, busy: bool, active: str = '') -> None:
        """Disable both parameter buttons while a request is in flight."""
        self._param_busy = busy
        self.read_button.setText(
            '读取中…' if busy and active == 'read' else READ_CONFIG_LABEL)
        self.apply_button.setText(
            '下发中…' if busy and active == 'apply' else APPLY_LABEL)
        self._render_param_buttons()

    def _render_param_buttons(self) -> None:
        enabled = self._is_connected and not self._param_busy
        self.read_button.setEnabled(enabled)
        self.apply_button.setEnabled(enabled)

    def _lock_param_fields(self) -> None:
        for guard in self._param_locks:
            guard.lock()

    def on_connect(self) -> None:
        body = self._body()
        # A connection is communication only.  Strip every sweep value from
        # the request so no code path can silently apply them to the analyzer.
        for key in ('start_hz', 'stop_hz', 'points', 'step_hz',
                    'power_dbm', 'ifbw_hz'):
            body.pop(key, None)
        if body['interface'] == 'gpib':
            from tuner.vna_drivers import normalise_gpib_resource
            try:
                body['resource'] = normalise_gpib_resource(body['resource'])
            except ValueError as exc:
                QMessageBox.warning(self, '矢网连接', str(exc))
                return
            self.resource_edit.setText(body['resource'])
        elif not body['host']:
            QMessageBox.warning(self, '矢网连接', '请填写仪器 IP 地址')
            return
        self.connect_button.setEnabled(False)
        self.connect_button.setText('连接中…')
        run_in_background(lambda: handlers.vna_connect(self.state, body),
                          on_done=self._connected, on_error=self._connect_failed,
                          description='vna connect')

    def _connected(self, status: dict) -> None:
        self.connect_button.setText(CONNECT_LABEL)
        self.connect_button.setEnabled(True)
        self._render(status)
        self.report('矢网已连接（仅通信，未修改仪器参数）: '
                    f"{status.get('idn') or ''}")

    def _connect_failed(self, exc: BaseException) -> None:
        self.connect_button.setText(CONNECT_LABEL)
        self.connect_button.setEnabled(True)
        self.report_error(exc)

    def on_disconnect(self) -> None:
        try:
            status = handlers.vna_disconnect(self.state, {})
        except Exception as exc:  # noqa: BLE001
            self.report_error(exc)
            return
        self._render(status)
        self.report('矢网已断开')

    def on_read_config(self) -> None:
        """Query the analyzer's current settings into the form (read-only)."""
        self._set_param_busy(True, 'read')

        def done(result):
            self._set_param_busy(False)
            self._config_read(result)

        def failed(exc):
            self._set_param_busy(False)
            self.report_error(exc)

        run_in_background(
            lambda: handlers.vna_read_config(self.state, {}),
            on_done=done, on_error=failed, description='vna read config')

    def _config_read(self, result: dict) -> None:
        cfg = dict((result or {}).get('config') or {})
        # Programmatic fills must not be mistaken for an operator edit.
        self._loading = True
        try:
            for spin, key, scale in (
                    (self.start_spin, 'start_hz', 1e9),
                    (self.stop_spin, 'stop_hz', 1e9),
                    (self.points_spin, 'points', 1),
                    (self.power_spin, 'power_dbm', 1),
                    (self.ifbw_spin, 'ifbw_hz', 1)):
                value = cfg.get(key)
                if value is None:
                    continue
                spin.setValue(int(value) if spin is self.points_spin
                              else float(value) / scale)
        finally:
            self._loading = False
        self._lock_param_fields()
        fallback = cfg.get('fallback') or []
        if cfg.get('source') == 'cached' or len(fallback) >= 5:
            self.report('仪器未返回配置参数，表单保留当前值')
        elif fallback:
            self.report('已从矢网读取当前参数（部分字段未返回，保留本机值：'
                        + '、'.join(str(f) for f in fallback) + '）')
        else:
            self.report(
                f"已从矢网读取当前参数：{self.start_spin.value():.4f}–"
                f"{self.stop_spin.value():.4f} GHz · {self.points_spin.value()} 点 · "
                f"{self.power_spin.value():.2f} dBm · "
                f"{self.ifbw_spin.value():.0f} Hz")

    def on_apply_sweep(self) -> None:
        """The only action that writes sweep settings to the instrument."""
        body = self._body()
        if body['stop_hz'] <= body['start_hz']:
            QMessageBox.warning(self, '扫描参数', '终止频率必须大于起始频率')
            return
        self._set_param_busy(True, 'apply')

        def apply_all():
            handlers.vna_set_sweep(self.state, body)
            handlers.vna_set_power(self.state, body)
            handlers.vna_set_ifbw(self.state, body)
            return handlers.vna_status(self.state, {})

        def done(status):
            self._set_param_busy(False)
            self._applied(status)

        def failed(exc):
            self._set_param_busy(False)
            self.report_error(exc)

        run_in_background(apply_all, on_done=done, on_error=failed,
                          description='vna sweep')

    def _applied(self, status: dict) -> None:
        self._render(status)
        self._lock_param_fields()
        self.report(
            f"已下发扫描参数：{self.start_spin.value():.4f}–{self.stop_spin.value():.4f} GHz · "
            f"{self.points_spin.value()} 点 · 步进 {self.step_spin.value():.4f} MHz")
        self._mark_stale('⚠ 扫描参数已写入仪器，图上仍是上一次扫描的数据；'
                         '点「单次扫描」按新设置重新采集。')

    def on_scan(self) -> None:
        """Trigger one sweep and read it, using the analyzer's own setup.

        No ``set_sweep`` / ``set_power`` / ``set_ifbw`` and no form validation:
        the form is a view and an editor for 下发扫描参数, not a precondition
        for scanning.  ``vna_fetch`` refreshes the local frequency grid with
        read-only queries, and ``measure()`` performs the single trigger --
        calling ``vna_trigger`` here as well used to sweep the box twice.
        """
        self.scan_button.setEnabled(False)
        self.scan_button.setText('扫描中…')
        self._scan_busy = True
        self.stale_label.setVisible(False)

        run_in_background(lambda: handlers.vna_fetch(self.state, {}),
                          on_done=self._show_scan, on_error=self._scan_failed,
                          description='vna scan')

    def _scan_failed(self, exc: BaseException) -> None:
        self._scan_busy = False
        self.scan_button.setEnabled(True)
        self.scan_button.setText('单次扫描（4 路）')
        self.report_error(exc)

    def on_clear(self) -> None:
        for plot in self.traces.values():
            plot.clear()
        self.table.setRowCount(0)
        self.table_note.setText('')
        self.summary_label.setText('尚未扫描')
        self.stale_label.setVisible(False)

    # ---------------- results ----------------
    def _show_scan(self, data: dict) -> None:
        self._scan_busy = False
        self.scan_button.setEnabled(True)
        self.scan_button.setText('单次扫描（4 路）')
        frequencies = np.asarray(data['frequencies_hz'], float)
        if frequencies.size == 0:
            self.report_error(RuntimeError('仪器返回 0 个频点'))
            return

        for name in S_PARAMETERS:
            payload = data['s'].get(name)
            if payload is None:
                self.traces[name].clear()
                continue
            self.traces[name].set_data(frequencies, np.asarray(payload['mag_db'], float))

        requested = data.get('requested') or {}
        self._fill_table(frequencies, data['s'])
        self._summarise(frequencies, requested)
        self.stale_label.setVisible(False)
        self._last_scan_step_hz = self._actual_step(frequencies)
        self.report(f"扫描完成（按矢网当前配置，未下发参数）: {len(frequencies)} 点 · "
                    f"{data.get('vendor') or ''}")

    @staticmethod
    def _actual_step(frequencies: np.ndarray) -> float:
        if frequencies.size < 2:
            return 0.0
        return float((frequencies[-1] - frequencies[0]) / (frequencies.size - 1))

    def _summarise(self, frequencies: np.ndarray, requested: dict) -> None:
        count = int(frequencies.size)
        step = self._actual_step(frequencies)
        start, stop = float(frequencies[0]), float(frequencies[-1])
        # The KPI tiles are optional: the page was laid out without them, and
        # every other update site guards with hasattr().  An unguarded call here
        # raised AttributeError inside the background-task callback, which meant
        # the scan finished but the summary was never written.
        if hasattr(self, 'kpi_points'):
            self.kpi_points.set_value(str(count),
                                      f'{start / 1e9:.3f}–{stop / 1e9:.3f} GHz')
        if hasattr(self, 'kpi_step'):
            self.kpi_step.set_value(f'{step / 1e6:.4f} MHz', '实测步进')
        self.summary_label.setText(
            f'实测：{count} 点 · {start / 1e9:.6f}–{stop / 1e9:.6f} GHz · '
            f'步进 {step / 1e6:.4f} MHz')
        self.summary_label.setStyleSheet('color:#5b6b7c;')

        problems = []
        if requested:
            want_points = int(requested.get('points') or count)
            want_step = float(requested.get('step_hz') or 0.0)
            if abs(count - want_points) > max(2, want_points * 0.002):
                problems.append(f'点数 {count} ≠ 设定 {want_points}')
            if want_step > 0 and abs(step - want_step) / want_step > 0.02:
                problems.append(f'步进 {step / 1e6:.4f} MHz ≠ 设定 {want_step / 1e6:.4f} MHz')
        if problems:
            self.summary_label.setStyleSheet('color:#b03a2e; font-weight:600;')
            self.summary_label.setText(
                '实测：' + ' · '.join(problems) +
                '（仪器未接受设定，或已按内部规则取整）')

    def _fill_table(self, frequencies: np.ndarray, series: dict) -> None:
        count = int(frequencies.size)
        stride = max(1, int(np.ceil(count / MAX_TABLE_ROWS)))
        rows = list(range(0, count, stride))
        if rows and rows[-1] != count - 1:
            rows.append(count - 1)
        self.table.setRowCount(len(rows))
        for row, index in enumerate(rows):
            step = (frequencies[index] - frequencies[index - 1]) if index else (
                frequencies[1] - frequencies[0] if count > 1 else 0.0)
            values = [f'{frequencies[index] / 1e9:.6f}', f'{step / 1e6:.4f}']
            for name in S_PARAMETERS:
                payload = series.get(name)
                values.append(f"{payload['mag_db'][index]:.3f}" if payload else '—')
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFont(px_font(11.5, 'medium'))
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, column, item)
        if stride > 1:
            self.table_note.setText(
                f'共 {count} 点，表格抽样显示 {len(rows)} 行（每 {stride} 点取 1，末点保留）；'
                '完整数据在图上。')
        else:
            self.table_note.setText(f'共 {count} 点，全部显示。')

    def on_read_errors(self) -> None:
        def action():
            with self.state.vna_lock:
                controller = self.state.vna.get('controller')
                if controller is None or not controller.connected:
                    raise RuntimeError('VNA not connected')
                return controller.read_errors()

        run_in_background(action, on_done=self._show_errors,
                          on_error=self.report_error, description='vna errors')

    def _show_errors(self, errors: list) -> None:
        text = '；'.join(errors) if errors else '错误队列为空'
        self.error_label.setText(text)
        self.report(f'矢网错误队列: {text}')

    # ---------------- status ----------------
    def refresh_status(self) -> None:
        try:
            status = handlers.vna_status(self.state, {})
        except Exception:  # noqa: BLE001 - polling must never raise
            return
        self._render(status)

    def _render(self, status: dict) -> None:
        connected = bool(status.get('connected'))
        self._is_connected = connected
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.scan_button.setEnabled(connected and not self._scan_busy)
        self.errors_button.setEnabled(connected)
        self._render_param_buttons()
        if hasattr(self, 'kpi_state'):
            self.kpi_state.set_value('已连接' if connected else '未连接',
                                     str(status.get('idn') or '未连接仪器')[:40],
                                     'ok' if connected else '')
        if connected:
            self.vendor_label.setText(str(status.get('vendor') or '—'))
            self.idn_label.setText(str(status.get('idn') or '—'))
            if hasattr(self, 'kpi_vendor'):
                self.kpi_vendor.set_value(str(status.get('vendor') or '—'))
        else:
            self.vendor_label.setText('—')
            if hasattr(self, 'kpi_vendor'):
                self.kpi_vendor.set_value('—')
        error = status.get('last_error')
        self.error_label.setText(str(error) if error else '—')
        self.status_changed.emit(
            connected, self._target_text(), str(status.get('idn') or ''))

    def connection_info(self) -> tuple[bool, str, str]:
        """Current (connected, target, idn) without waiting for the next poll."""
        connected = bool(self.state.vna.get('connected'))
        target = self._target_text() or str(
            self.state.vna['config'].get('host') or '')
        return connected, target, str(self.state.vna.get('idn') or '')

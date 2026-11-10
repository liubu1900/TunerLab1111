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
* editing the point count recomputes the step;
* pressing 单次扫描 pushes start/stop/points/power/IFBW to the instrument *before*
  triggering, then compares the returned frequency axis with the request and
  reports any disagreement instead of silently showing stale data.
"""
from __future__ import annotations

from typing import Any

import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
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
        self._last_scan_step_hz = 0.0

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
        self.scan_button.clicked.connect(self.on_scan)
        self.errors_button.clicked.connect(self.on_read_errors)
        self.clear_button.clicked.connect(self.on_clear)

        # Moving from one trace to the next emits leave-then-move; a short delay
        # keeps the shared cursor from blinking off between the two events.
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setSingleShot(True)
        self._cursor_timer.setInterval(60)
        self._cursor_timer.timeout.connect(self._clear_trace_cursor)

        self._load_config_into_form(cfg)
        self.points_spin.valueChanged.connect(self._update_step_from_points)
        self.step_spin.valueChanged.connect(self._update_points_from_step)
        self._update_step_from_points()
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

        self.vendor_combo = QComboBox()
        for key, label in handlers.vna_vendors(self.state, {}).get('vendors', {}).items():
            self.vendor_combo.addItem(label, key)
        self.vendor_combo.insertItem(0, '自动识别 (*IDN?)', 'auto')
        form.addRow('厂商', self.vendor_combo)

        self.host_edit = QLineEdit(str(cfg['host']))
        self.host_edit.setPlaceholderText('192.168.2.111')
        form.addRow('IP 地址', self.host_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(int(cfg['port']))
        form.addRow('端口', self.port_spin)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(500, 120000)
        self.timeout_spin.setSingleStep(500)
        self.timeout_spin.setSuffix(' ms')
        self.timeout_spin.setValue(int(cfg['timeout_ms']))
        form.addRow('超时', self.timeout_spin)

        row = QHBoxLayout()
        self.connect_button = QPushButton('连接并配置')
        self.connect_button.setProperty('accent', True)
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

        self.apply_button = QPushButton('下发扫描参数')
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

    # ---------------- sweep model ----------------
    def _load_config_into_form(self, cfg: dict) -> None:
        self.vendor_combo.setCurrentIndex(
            max(0, self.vendor_combo.findData(str(cfg.get('vendor_key') or 'auto'))))
        self.start_spin.blockSignals(True)
        self.stop_spin.blockSignals(True)
        self.start_spin.setValue(float(cfg['start_hz']) / 1e9)
        self.stop_spin.setValue(float(cfg['stop_hz']) / 1e9)
        self.start_spin.blockSignals(False)
        self.stop_spin.blockSignals(False)

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

    def _mark_stale(self) -> None:
        if any(plot.has_data() for plot in self.traces.values()):
            self.stale_label.setText(
                '⚠ 扫描参数已修改：图上仍是上一次扫描的数据，点「单次扫描」刷新。')
            self.stale_label.setStyleSheet(
                'color:#8a5a00; background:#fff6e0; border:1px solid #f0d9a8;'
                'border-radius:4px; padding:5px 8px;')
            self.stale_label.setVisible(True)

    # ---------------- body ----------------
    def _body(self) -> dict[str, Any]:
        return {
            'vendor_key': self.vendor_combo.currentData(),
            'host': self.host_edit.text().strip(),
            'port': self.port_spin.value(),
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
    def on_connect(self) -> None:
        body = self._body()
        if not body['host']:
            QMessageBox.warning(self, '矢网连接', '请填写仪器 IP 地址')
            return
        self.connect_button.setEnabled(False)
        self.connect_button.setText('连接中…')
        run_in_background(lambda: handlers.vna_connect(self.state, body),
                          on_done=self._connected, on_error=self._connect_failed,
                          description='vna connect')

    def _connected(self, status: dict) -> None:
        self.connect_button.setText('连接并配置')
        self.connect_button.setEnabled(True)
        self._render(status)
        self.report(f"矢网已连接: {status.get('idn') or ''}")
        self._mark_stale()

    def _connect_failed(self, exc: BaseException) -> None:
        self.connect_button.setText('连接并配置')
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

    def on_apply_sweep(self) -> None:
        body = self._body()
        if body['stop_hz'] <= body['start_hz']:
            QMessageBox.warning(self, '扫描参数', '终止频率必须大于起始频率')
            return

        def apply_all():
            handlers.vna_set_sweep(self.state, body)
            handlers.vna_set_power(self.state, body)
            handlers.vna_set_ifbw(self.state, body)
            return handlers.vna_status(self.state, {})

        run_in_background(apply_all, on_done=self._applied,
                          on_error=self.report_error, description='vna sweep')

    def _applied(self, status: dict) -> None:
        self._render(status)
        self.report(
            f"已下发扫描参数：{self.start_spin.value():.4f}–{self.stop_spin.value():.4f} GHz · "
            f"{self.points_spin.value()} 点 · 步进 {self.step_spin.value():.4f} MHz")
        self._mark_stale()

    def on_scan(self) -> None:
        """Configure, trigger and read in one go.

        A sweep that is triggered without re-sending the parameters returns
        whatever the instrument was last set to -- which is exactly how the old
        page ended up showing a 0.5 GHz grid while the form said 10 MHz.
        """
        body = self._body()
        if body['stop_hz'] <= body['start_hz']:
            QMessageBox.warning(self, '扫描参数', '终止频率必须大于起始频率')
            return

        self.scan_button.setEnabled(False)
        self.scan_button.setText('扫描中…')
        self.stale_label.setVisible(False)

        def acquire():
            handlers.vna_set_sweep(self.state, body)
            handlers.vna_set_power(self.state, body)
            handlers.vna_set_ifbw(self.state, body)
            handlers.vna_trigger(self.state, {})
            data = handlers.vna_fetch(self.state, {})
            data['requested'] = {
                'start_hz': body['start_hz'], 'stop_hz': body['stop_hz'],
                'points': body['points'], 'step_hz': body['step_hz'],
            }
            return data

        run_in_background(acquire, on_done=self._show_scan,
                          on_error=self._scan_failed, description='vna scan')

    def _scan_failed(self, exc: BaseException) -> None:
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
        self.report(f"扫描完成: {len(frequencies)} 点 · {data.get('vendor') or ''}")

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
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.scan_button.setEnabled(connected)
        self.errors_button.setEnabled(connected)
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
            connected, self.host_edit.text().strip(), str(status.get('idn') or ''))

    def connection_info(self) -> tuple[bool, str, str]:
        """Current (connected, host, idn) without waiting for the next poll."""
        connected = bool(self.state.vna.get('connected'))
        host = self.host_edit.text().strip() or str(self.state.vna['config'].get('host') or '')
        return connected, host, str(self.state.vna.get('idn') or '')

"""Point operations: vector S11 repeatability vs library + Γ-plan derivation.

The math lives in :mod:`tuner.repeatability` and
:mod:`tuner.library_plan_derive`; these pages only pick points, show results
and forward the operator's decisions to the shared web handlers.

* :class:`VectorRepeatabilityPage` — tick measured points, re-measure once and
  compare with the stored (in-memory) S11 using 20·log10|S11^m − S11^c|.
* :class:`GammaPlanDerivePage` — derive a new measured-truth library whose
  points follow an equal-|Γ| / Smith-uniform plan (no re-measurement).
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout,
)

from tuner.ui import SmithChart
from tuner.ui_theme import numeric_font
from tuner.web import handlers
from .pages_base import _StudioPage, _page_title
from .ui import Card, field_label, hint_label, smith_control_row


class VectorRepeatabilityPage(_StudioPage):
    """Select library points, re-measure once, compare S11 against the library."""

    def __init__(self, app_state, parent=None):
        super().__init__(app_state, parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)
        root.addLayout(_page_title(
            '指定点位 · 矢量重复性校验',
            '勾选库中已测点位，现场逐点复测一次 S11，与库内第一次数据比较：'
            'Repeatability = 20·log₁₀|S11ᵐ − S11ᶜ| (dB)。会移动机械并触发矢网。'))

        top = QHBoxLayout()
        top.setSpacing(10)
        self.load_btn = QPushButton('载入库点位')
        self.all_btn = QPushButton('全选')
        self.none_btn = QPushButton('清空')
        top.addWidget(self.load_btn)
        top.addWidget(self.all_btn)
        top.addWidget(self.none_btn)
        top.addWidget(field_label('频率'))
        self.freq_combo = QComboBox()
        self.freq_combo.setMinimumWidth(130)
        self.freq_combo.setToolTip('从当前校准库的频率列表中选择；矢量重复性必须在选定频率上进行')
        top.addWidget(self.freq_combo)
        self.tol = QDoubleSpinBox()
        self.tol.setRange(-200.0, 0.0)
        self.tol.setDecimals(1)
        self.tol.setValue(-30.0)
        self.tol.setSuffix(' dB')
        top.addWidget(field_label('判定阈值'))
        top.addWidget(self.tol)
        top.addStretch()
        self.run_btn = QPushButton('开始矢量重复性测试')
        self.run_btn.setProperty('accent', True)
        self.run_btn.setObjectName('DangerButton')
        top.addWidget(self.run_btn)
        root.addLayout(top)

        body = QHBoxLayout()
        body.setSpacing(12)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['选', '#', '机械位置', '|Γ|'])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setMinimumWidth(440)
        smith_box = QVBoxLayout()
        smith_box.setSpacing(6)
        self.smith = SmithChart()
        smith_box.addLayout(smith_control_row(self.smith))
        self.smith.setMinimumSize(360, 320)
        smith_box.addWidget(self.smith)
        smith_box.addWidget(hint_label(
            '单击图中的点可选中对应点位；滚轮 / 缩放、移动 按钮或拖拽可平移，'
            '双击或「复位」恢复原视图。'))
        body.addWidget(self.table, 1)
        body.addLayout(smith_box)
        root.addLayout(body, 1)

        self.selected_label = QLabel('已选 0 点')
        self.selected_label.setFont(numeric_font(10, bold=True))
        root.addWidget(self.selected_label)

        self.result = QTextEdit()
        self.result.setReadOnly(True)
        self.result.setFont(numeric_font(9))
        self.result.setPlaceholderText('逐点 repeatability 结果会显示在这里')
        root.addWidget(self.result, 1)

        self.load_btn.clicked.connect(self.load_points)
        self.all_btn.clicked.connect(self.select_all)
        self.none_btn.clicked.connect(self.select_none)
        self.run_btn.clicked.connect(self.on_run)
        self.freq_combo.currentIndexChanged.connect(self._on_frequency_changed)
        self.smith.picked.connect(self._on_smith_picked)

        self._points = []
        self._frequencies: list[float] = []
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._poll)

    # -- point list -------------------------------------------------------
    def on_library_changed(self, path: str) -> None:
        self.load_points()

    def refresh(self) -> None:
        self.load_points()

    def _selected_frequency(self):
        return self.freq_combo.currentData()

    def _sync_frequencies(self, frequencies: list[float]) -> None:
        values = [float(f) for f in frequencies]
        if values == self._frequencies:
            return
        previous = self.freq_combo.currentData()
        self._frequencies = values
        self.freq_combo.blockSignals(True)
        self.freq_combo.clear()
        for hz in values:
            self.freq_combo.addItem(f'{hz / 1e9:.4f} GHz', hz)
        restore = self.freq_combo.findData(previous) if previous is not None else -1
        if restore >= 0:
            self.freq_combo.setCurrentIndex(restore)
        self.freq_combo.blockSignals(False)

    def _on_frequency_changed(self) -> None:
        # Re-read |Γ| at the chosen frequency, keeping the checked point indices.
        selected = set(self._selected_indices())
        self.load_points()
        for row in range(self.table.rowCount()):
            index = int(self.table.item(row, 1).text())
            if index in selected:
                self.table.item(row, 0).setCheckState(Qt.Checked)
        self._update_selection()

    def load_points(self) -> None:
        body = {}
        frequency = self._selected_frequency()
        if frequency is not None:
            body['frequency_hz'] = float(frequency)
        try:
            data = handlers.studio_library_points(self.state, body)
        except Exception as exc:  # noqa: BLE001
            self.warn('载入失败', f'{type(exc).__name__}: {exc}')
            return
        self._sync_frequencies(data.get('frequencies_hz', []))
        self._points = data.get('points', [])
        try:
            self.table.itemChanged.disconnect(self._on_check_changed)
        except (TypeError, RuntimeError):
            pass
        self.table.setRowCount(0)
        for p in self._points:
            row = self.table.rowCount()
            self.table.insertRow(row)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            check.setCheckState(Qt.Unchecked)
            self.table.setItem(row, 0, check)
            self.table.setItem(row, 1, QTableWidgetItem(str(p['index'])))
            self.table.setItem(row, 2, QTableWidgetItem(
                ' | '.join(f'{v:.4f}' for v in p['position'])))
            self.table.setItem(row, 3, QTableWidgetItem(f"{p['gamma_mag']:.5f}"))
        self.table.itemChanged.connect(self._on_check_changed)
        self._update_selection()

    def _on_smith_picked(self, gamma) -> None:
        """Map a click on the Smith chart back to the closest measured point."""
        if not self._points or gamma is None:
            return
        target = complex(gamma)
        best_row, best_dist = None, float('inf')
        for row, p in enumerate(self._points):
            point_gamma = complex(p['gamma_re'], p['gamma_im'])
            distance = abs(point_gamma - target)
            if distance < best_dist:
                best_row, best_dist = row, distance
        if best_row is None or best_dist > 0.25:
            return
        self.table.blockSignals(True)
        self.table.item(best_row, 0).setCheckState(Qt.Checked)
        self.table.blockSignals(False)
        self._update_selection()
        self.selected_label.setText(
            f'已选 {len(self._selected_indices())} 点 · 图选 Γ≈{target.real:.3f}{target.imag:+.3f}j')

    def _on_check_changed(self, _item) -> None:
        self._update_selection()

    def _selected_indices(self):
        out = []
        for row in range(self.table.rowCount()):
            cell = self.table.item(row, 0)
            if cell is not None and cell.checkState() == Qt.Checked:
                out.append(int(self.table.item(row, 1).text()))
        return out

    def _update_selection(self) -> None:
        indices = set(self._selected_indices())
        self.selected_label.setText(f'已选 {len(indices)} 点')
        all_gamma = np.asarray([complex(p['gamma_re'], p['gamma_im'])
                                for p in self._points], complex)
        self.smith.points = all_gamma
        sel_gamma = np.asarray([complex(p['gamma_re'], p['gamma_im'])
                                for p in self._points if p['index'] in indices], complex)
        self.smith.selected = complex(sel_gamma[0]) if sel_gamma.size else None
        self.smith.update()

    def select_all(self) -> None:
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(Qt.Checked)
        self.table.blockSignals(False)
        self._update_selection()

    def select_none(self) -> None:
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(Qt.Unchecked)
        self.table.blockSignals(False)
        self._update_selection()

    # -- action -----------------------------------------------------------
    def on_run(self) -> None:
        frequency = self._selected_frequency()
        if frequency is None:
            QMessageBox.information(
                self, '矢量重复性', '请先从频率列表选择一个频率，再开始校验')
            return
        indices = self._selected_indices()
        if not indices:
            QMessageBox.information(self, '矢量重复性', '请先勾选至少一个点位')
            return
        body = {'indices': indices, 'tolerance_db': float(self.tol.value()),
                'frequency_hz': float(frequency)}
        if QMessageBox.question(
                self, '确认实测',
                f'将对 {len(indices)} 个点位在 {float(frequency) / 1e9:.4f} GHz '
                '逐点移动机械并触发矢网复测一次。\n'
                '请确认回零与行程可用、矢网已连接。是否继续？') != QMessageBox.Yes:
            return
        try:
            handlers.studio_vector_repeatability(self.state, body)
        except Exception as exc:  # noqa: BLE001
            self.fail(exc, '启动矢量重复性失败')
            return
        self.run_btn.setEnabled(False)
        self.result.setPlainText('测试中…')
        self.timer.start()

    def _poll(self) -> None:
        state = self.state.studio.get('calibration_state')
        if state not in ('validated', 'failed'):
            return
        self.timer.stop()
        self.run_btn.setEnabled(True)
        report = self.state.studio.get('validation_report') or {}
        if report.get('kind') != 'vector_repeatability_memory':
            self.result.setPlainText('测试结束，但未产生矢量重复性报告。')
            return
        lines = [
            '矢量重复性（20log|S11ᵐ − S11ᶜ|）',
            f"点数 {report.get('n')}  ·  最差 {report.get('worst_db'):.2f} dB"
            f"  ·  最差均值 {report.get('mean_of_worst_db'):.2f} dB",
            '',
            f"{'点位':<8}{'最差(dB)':>12}  判定",
        ]
        for row in report.get('rows', []):
            verdict = ('—' if 'passed' not in row
                       else ('通过' if row['passed'] else '超差'))
            lines.append(f"{row.get('point_index', '-'):<8}"
                         f"{row.get('worst_db', 0):>12.2f}  {verdict}")
        self.result.setPlainText('\n'.join(lines))
        self.report(f"矢量重复性完成：最差 {report.get('worst_db'):.2f} dB")


class GammaPlanDerivePage(_StudioPage):
    """Derive a measured-truth library following an equal-|Γ| / Smith plan."""

    def __init__(self, app_state, parent=None):
        super().__init__(app_state, parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)
        root.addLayout(_page_title(
            '按 Γ 分布派生新库',
            '按等 Γ 圆分层或 Smith 图均匀布点的目标 Γ，从当前库挑出 Γ 最接近的已测真值点，'
            '用其原坐标与原 S 参数重组成新库。全部为实测真值，无需重新测试。'))

        form = QGroupBox('派生参数')
        grid = QHBoxLayout(form)
        self.plan = QComboBox()
        self.plan.addItem('等 Γ 圆分层（冷启动）', 'gamma_rings')
        self.plan.addItem('Smith 图均匀布点', 'smith')
        self.density = QComboBox()
        self.density.addItem('低 · 95 点', 'low')
        self.density.addItem('中 · 181 点', 'medium')
        self.density.addItem('高 · 361 点', 'high')
        self.density.setCurrentIndex(1)
        self.freq_combo = QComboBox()
        self.freq_combo.setMinimumWidth(130)
        self.freq_combo.setToolTip('从当前校准库的频率列表中选择；派生库只保留该频点')
        self.rmin = QDoubleSpinBox(); self.rmin.setRange(0.0, 1.0); self.rmin.setDecimals(3); self.rmin.setValue(0.0)
        self.rmax = QDoubleSpinBox(); self.rmax.setRange(0.0, 1.0); self.rmax.setDecimals(3); self.rmax.setValue(0.95)
        for w, label in ((self.plan, '布点方式'), (self.density, '密度'),
                         (self.freq_combo, '频率'),
                         (self.rmin, '|Γ|最小'), (self.rmax, '|Γ|最大')):
            grid.addWidget(field_label(label))
            grid.addWidget(w)
        grid.addStretch()
        root.addWidget(form)

        out_row = QHBoxLayout()
        self.path_edit = QLabel('')
        self.browse = QPushButton('选择输出…')
        self.preview_btn = QPushButton('预览目标 Γ')
        self.derive_btn = QPushButton('派生新库')
        self.derive_btn.setProperty('accent', True)
        out_row.addWidget(field_label('输出'))
        out_row.addWidget(self.path_edit, 1)
        out_row.addWidget(self.browse)
        out_row.addWidget(self.preview_btn)
        out_row.addWidget(self.derive_btn)
        root.addLayout(out_row)

        body = QHBoxLayout()
        smith_box = QVBoxLayout()
        smith_box.setSpacing(6)
        self.smith = SmithChart()
        smith_box.addLayout(smith_control_row(self.smith))
        self.smith.setMinimumSize(360, 320)
        smith_box.addWidget(self.smith)
        body.addLayout(smith_box)
        self.result = QTextEdit()
        self.result.setReadOnly(True)
        self.result.setFont(numeric_font(9))
        body.addWidget(self.result, 1)
        root.addLayout(body, 1)

        self.browse.clicked.connect(self.on_browse)
        self.preview_btn.clicked.connect(self.on_preview)
        self.derive_btn.clicked.connect(self.on_derive)
        self.freq_combo.currentIndexChanged.connect(self._on_frequency_changed)
        self._frequencies: list[float] = []
        self.refresh()

    def on_library_changed(self, path: str) -> None:
        self.refresh()

    def refresh(self) -> None:
        """Follow the active library's frequency list (mid-band default)."""
        try:
            data = handlers.studio_library_points(self.state, {})
        except Exception:  # noqa: BLE001 - no library yet
            return
        frequencies = [float(v) for v in data.get('frequencies_hz', [])]
        if frequencies == self._frequencies:
            return
        previous = self.freq_combo.currentData()
        self._frequencies = frequencies
        self.freq_combo.blockSignals(True)
        self.freq_combo.clear()
        for hz in frequencies:
            self.freq_combo.addItem(f'{hz / 1e9:.4f} GHz', hz)
        restore = (self.freq_combo.findData(previous) if previous is not None else -1)
        if restore < 0 and frequencies:
            restore = len(frequencies) // 2
        self.freq_combo.setCurrentIndex(max(0, restore))
        self.freq_combo.blockSignals(False)

    def _on_frequency_changed(self) -> None:
        self.on_preview()

    def on_browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, '派生库输出', '', 'HDF5 (*.h5 *.hdf5)')
        if path:
            self.path_edit.setText(path)

    def _body(self):
        body = {
            'plan': self.plan.currentData(),
            'density': self.density.currentData(),
            'r_min': float(self.rmin.value()),
            'r_max': float(self.rmax.value()),
        }
        frequency = self.freq_combo.currentData()
        if frequency is not None:
            body['frequency_hz'] = float(frequency)
        return body

    def on_preview(self) -> None:
        try:
            data = handlers.studio_plan_targets(self.state, self._body())
        except Exception as exc:  # noqa: BLE001
            self.fail(exc, '预览失败')
            return
        targets = np.asarray([complex(re, im) for re, im in data['targets']], complex)
        self.smith.points = targets
        self.smith.selected = None
        self.smith.update()
        self.result.setPlainText(f"目标 Γ 点数：{data['count']}")

    def on_derive(self) -> None:
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.information(self, '派生新库', '请先选择输出路径')
            return
        if self.freq_combo.currentData() is None:
            QMessageBox.information(self, '派生新库', '请先从频率列表选择频点再派生')
            return
        body = dict(self._body(), path=path)
        try:
            res = handlers.studio_library_plan_derive(self.state, body)
        except Exception as exc:  # noqa: BLE001
            self.fail(exc, '派生失败')
            return
        self.result.setPlainText(
            f"派生完成 ✓\n路径：{res['path']}\n点数：{res['points']}（全部实测真值）\n"
            f"布点：{res['plan']} · {res['density']}\n"
            f"频率：{res['frequency_hz'] / 1e9:.4f} GHz\n"
            f"父版本：{res['parent_version']}\n"
            f"最近邻 Γ 距离：最大 {res['max_nearest_distance']:.4f} / "
            f"平均 {res['mean_nearest_distance']:.4f}\n"
            '说明：复用源库真值，未重新测试。')
        self.report(f"派生新库完成：{res['points']} 点")

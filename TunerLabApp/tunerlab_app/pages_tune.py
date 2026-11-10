"""Smith chart, impedance tuning and validation pages.

The math lives in :mod:`tuner.search`, :mod:`tuner.interp` and
:mod:`tuner.tuner_validation`; these pages only present it and forward the
operator's decisions.  Every position the software proposes is a *prediction*
until a measurement confirms it, and the UI says so in the same words.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
)

from tuner.rf import gamma_to_z
from tuner.ui import SmithChart
from tuner.ui_theme import numeric_font
from tuner.web import handlers
from . import paths
from .bg import run_in_background
from .pages_base import _StudioPage, _page_title
from .pages_p1 import DeviationCard
from .smith_spec import SmithSpec
from .ui import (Card, field_grid, field_label, hint_label, page_header,
                   smith_control_row)


class StudioSmithPage(_StudioPage):
    """Pick a Γ on the chart, inspect it, then move there and verify.

    The chart is no longer a passive viewer: a left click inside the unit
    circle selects the target impedance, the information card shows every
    quantity derived from it, and one button drives the whole
    search → move → VNA measurement → judgement loop.
    """

    def __init__(self, app_state, parent=None):
        super().__init__(app_state, parent)
        self._picked = None
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)
        root.addLayout(_page_title(
            'Smith 图',
            '青色为校准库实测点，琥珀色为目标，粉色为当前候选，紫色十字为左键点选目标。'
            '点选后可搜索对应机械位置并一键「移动并实测验证」。'
            '图上任意一点都是「测过的位置」，不是可达范围边界。'))

        controls = QHBoxLayout()
        self.frequency = QDoubleSpinBox()
        self.frequency.setRange(0.001, 200.0)
        self.frequency.setDecimals(6)
        self.frequency.setSuffix(' GHz')
        self.frequency.setValue(10.0)
        self.sync_frequency_from_library(self.frequency)
        self.tolerance = self._num(0.08, 0.00001, 1.0, '', 5)
        self.refresh_button = QPushButton('刷新')
        self.refresh_button.setProperty('accent', True)
        self.summary = QLabel('—')
        self.summary.setObjectName('PageHint')
        for label, widget in (('频率', self.frequency),
                              ('容差 |ΔΓ|', self.tolerance)):
            controls.addWidget(QLabel(label))
            controls.addWidget(widget)
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.summary, 1)
        root.addLayout(controls)

        body = QHBoxLayout()
        self.chart = SmithChart()
        chart_holder = QVBoxLayout()
        chart_holder.addLayout(smith_control_row(self.chart))
        chart_holder.addWidget(self.chart, 1)
        chart_holder.addWidget(hint_label(
            '「缩放」模式下左键拖框放大局部、单击圆图内点选目标 Γ；'
            '「移动」模式左键拖拽平移；滚轮以光标为中心缩放；'
            '中键或 Ctrl+左键随时平移；双击复位。'))
        body.addLayout(chart_holder, 3)

        side = QVBoxLayout()
        side.setSpacing(10)
        pick_box = QGroupBox('点选目标（在圆图内单击左键）')
        grid = QGridLayout(pick_box)
        grid.setVerticalSpacing(5)
        grid.setHorizontalSpacing(10)
        self.values: dict[str, QLabel] = {}
        rows = (
            ('gamma', '反射系数 Γ'), ('polar', '|Γ| / 相位'),
            ('impedance', '阻抗 Z (Ω)'), ('normalized', '归一化 z = Z / Z₀'),
            ('vswr', 'VSWR'), ('return_loss', '回波损耗'),
            ('coords', 'Γ 坐标 (Re, Im)'), ('smith_coords', 'Smith 坐标 (r, x)'),
        )
        for row, (key, label) in enumerate(rows):
            caption = QLabel(label)
            caption.setStyleSheet('color:#5b6b7c;')
            grid.addWidget(caption, row, 0)
            self.values[key] = self._value_label()
            grid.addWidget(self.values[key], row, 1)
        self.cursor_label = QLabel('光标：—')
        self.cursor_label.setObjectName('PageHint')
        grid.addWidget(self.cursor_label, len(rows), 0, 1, 2)
        side.addWidget(pick_box)

        match_box = QGroupBox('对应机械位置（搜索后）')
        match = QVBoxLayout(match_box)
        self.match_label = self._value_label()
        self.match_label.setText('—')
        match.addWidget(self.match_label)
        side.addWidget(match_box)

        buttons = QHBoxLayout()
        self.search_button = QPushButton('搜索对应点位')
        self.move_button = QPushButton('移动并实测验证')
        self.move_button.setProperty('accent', True)
        self.search_button.setEnabled(False)
        self.move_button.setEnabled(False)
        buttons.addWidget(self.search_button)
        buttons.addWidget(self.move_button)
        self.reset_fault_button = QPushButton('复位运动故障')
        self.reset_fault_button.setToolTip(
            '清除软件层的运动故障锁存（不影响控制器本身）。\n'
            '若控制器已无故障且各轴 idle，下次移动时也会自动解除。')
        buttons.addWidget(self.reset_fault_button)
        side.addLayout(buttons)

        verdict_box = QGroupBox('到位实测判定')
        verdict_layout = QVBoxLayout(verdict_box)
        self.verdict = QTextEdit()
        self.verdict.setReadOnly(True)
        self.verdict.setFont(numeric_font(9))
        self.verdict.setPlaceholderText(
            '点选目标 → 点「移动并实测验证」，这里给出到位后的实测 Z / Γ / VSWR 与偏差判定')
        self.verdict.setMinimumHeight(180)
        verdict_layout.addWidget(self.verdict)
        side.addWidget(verdict_box, 1)
        body.addLayout(side, 2)
        root.addLayout(body, 1)

        self.refresh_button.clicked.connect(self.refresh)
        self.chart.picked.connect(self.on_pick)
        self.chart.hovered.connect(self.on_hover)
        self.search_button.clicked.connect(self.on_search)
        self.move_button.clicked.connect(self.on_move_verify)
        self.reset_fault_button.clicked.connect(self.on_reset_fault)
        self.refresh()

    # -- small helpers -------------------------------------------------
    def showEvent(self, event):  # noqa: N802 - Qt override
        """Adopt the loaded library's frequency band when the page is shown."""
        super().showEvent(event)
        if self.sync_frequency_from_library(self.frequency):
            self.refresh()

    def on_library_changed(self, path: str) -> None:
        """Re-base the chart on the newly active calibration (standard rule).

        Two things must happen together: the frequency spin box has to move
        into the new library's band *before* querying it (otherwise a 5 GHz
        library is asked for 10 GHz points and the chart stays blank), and the
        target / verdict picked against the previous library must be cleared.
        """
        self.sync_frequency_from_library(self.frequency)
        self._picked = None
        self.match_label.setText('—')
        self.verdict.clear()
        self.verdict.setStyleSheet('')
        for label in self.values.values():
            label.setText('—')
        self.refresh()

    @staticmethod
    def _num(value, low, high, suffix, decimals):
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(decimals)
        spin.setSuffix(suffix)
        spin.setValue(value)
        return spin

    @staticmethod
    def _value_label():
        label = QLabel('—')
        label.setFont(numeric_font(10, bold=True))
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    @staticmethod
    def _fmt(value, pattern='{:.4f}'):
        return '—' if value is None else pattern.format(value)

    @staticmethod
    def _zfmt(real, imag):
        return '—' if real is None or imag is None else f'{real:+.3f}{imag:+.3f}j Ω'

    @staticmethod
    def _vswr(value):
        return '∞ : 1' if value is None else f'{value:.2f} : 1'

    def _z0(self) -> float:
        engine = self.state.studio.get('search_engine')
        return float(getattr(engine, 'z0', 50.0) or 50.0)

    def _body(self, extra=None) -> dict:
        body = {
            'frequency': self.frequency.value() * 1e9,
            'tolerance': self.tolerance.value(),
            'method': 'two-stage',
            'level_policy': 'closest',
        }
        if extra:
            body.update(extra)
        return body

    def _restore_buttons(self) -> None:
        enabled = self._picked is not None
        self.search_button.setEnabled(enabled)
        self.move_button.setEnabled(enabled)
        controller = self._motion()
        self.reset_fault_button.setEnabled(
            bool(controller and getattr(controller, 'faulted', False)))

    def _motion(self):
        """Return the live motion controller (the planner's own object)."""
        motion = self.state.studio.get('motion')
        if motion is not None:
            return motion
        hw = getattr(self.state, 'hw', None)
        return getattr(hw, 'controller', None)

    def on_reset_fault(self) -> None:
        """Clear a latched motion fault so the operator is not stuck here.

        The latch is a software safety flag raised by our own move timeout or
        encoder check -- not the controller's fault word.  After clearing it the
        operator must re-home, because a latch invalidates the homing state by
        design.
        """
        controller = self._motion()
        if controller is None:
            self.warn('复位运动故障', '控制器尚未连接或尚未初始化，无法复位。')
            return
        if not getattr(controller, 'faulted', False):
            self.report('当前没有锁存的运动故障')
            return
        try:
            controller.reset_fault()
        except Exception as exc:  # noqa: BLE001
            self.fail(exc, '复位运动故障失败')
            return
        self.report('运动故障已复位；请到「轴移动控制」重新回零后再运动')
        self.refresh()

    # -- chart / data --------------------------------------------------
    def refresh(self) -> None:
        try:
            data = handlers.studio_smith(
                self.state, {'frequency': self.frequency.value() * 1e9})
        except Exception as exc:  # noqa: BLE001
            self.summary.setText(f'无法绘制: {exc}')
            return
        gammas = [complex(item['re'], item['im']) for item in data.get('gammas', [])]
        self.chart.points = np.asarray(gammas, dtype=complex)
        target = data.get('target')
        selected = data.get('selected')
        self.chart.target = (complex(target['re'], target['im'])
                             if target else None)
        self.chart.selected = (complex(selected['re'], selected['im'])
                               if selected else None)
        self.chart.update()
        if gammas:
            self.summary.setText(f'{len(gammas)} 个实测点 · '
                                 f'{data.get("frequency_hz", 0) / 1e9:.4f} GHz')
        else:
            self.summary.setText('尚未加载校准库，或该频点没有有效点')
        controller = self._motion()
        self.reset_fault_button.setEnabled(
            bool(controller and getattr(controller, 'faulted', False)))

    def on_pick(self, gamma) -> None:
        self._picked = complex(gamma) if gamma is not None else None
        self._restore_buttons()
        self.match_label.setText('—')
        self.verdict.clear()
        self.verdict.setStyleSheet('')
        if self._picked is None:
            for label in self.values.values():
                label.setText('—')
            return
        # F-24: every quantity on this page is formatted by the shared spec, so
        # the card here reads exactly like the card on the tuning page.
        z0 = self._z0()
        spec = SmithSpec(z0=z0, tolerance=self.tolerance.value())
        metrics = spec.metrics(self._picked)
        self.values['gamma'].setText(metrics['gamma_rect'])
        self.values['polar'].setText(
            f"{metrics['gamma_mag']:.4f}   ∠ {metrics['gamma_phase_deg']:+.2f}°")
        self.values['impedance'].setText(metrics['z_label'])
        self.values['normalized'].setText(
            f"{metrics['r_norm']:.4f}{metrics['x_norm']:+.4f}j   (Z₀ = {z0:g} Ω)")
        self.values['vswr'].setText(metrics['vswr'])
        self.values['return_loss'].setText(metrics['return_loss'])
        self.values['coords'].setText(
            f"({self._picked.real:+.5f}, {self._picked.imag:+.5f})")
        self.values['smith_coords'].setText(metrics['smith_coords'])

    def on_hover(self, gamma) -> None:
        if gamma is None:
            self.cursor_label.setText('光标：单位圆外（不会选中）')
        else:
            g = complex(gamma)
            self.cursor_label.setText(
                f'光标 Γ {g.real:+.4f}{g.imag:+.4f}j · |Γ| {abs(g):.3f}'
                f' · ∠{np.degrees(np.angle(g)):+.1f}°')

    # -- actions -------------------------------------------------------
    def on_search(self) -> None:
        if self._picked is None:
            return
        target_z = complex(gamma_to_z(self._picked, self._z0()))
        body = self._body({'R': target_z.real, 'X': target_z.imag})
        self.run(lambda: handlers.studio_tune(self.state, body),
                 self._searched, title='搜索失败', description='smith pick search')

    def _searched(self, data: dict) -> None:
        candidates = data.get('candidates', [])
        tuned = data.get('tuned')
        best = candidates[0] if candidates else None
        if tuned and 'position' in tuned and (best is None or
                                              tuned['error'] <= best['error']):
            best = tuned
        if best is None:
            self.match_label.setText('无可达点位')
            return
        position = ' | '.join(f'{v:.4f}' for v in best['position'])
        gamma = best['gamma']
        lines = [f'X / Z1 / Z2 / Z3   {position}',
                 f"预测 Γ   {gamma['re']:+.5f}{gamma['im']:+.5f}j",
                 f'|ΔΓ|   {best["error"]:.6g}']
        impedance = best.get('impedance')
        if impedance:
            lines.append(f"预测 Z   {impedance['re']:+.3f}{impedance['im']:+.3f}j Ω")
        lines.append('来源   ' + ('插值位置' if best is tuned else '已标定点'))
        self.match_label.setText('\n'.join(lines))
        self.report('已找到点选阻抗对应的机械位置')
        self.refresh()

    def on_move_verify(self) -> None:
        if self._picked is None:
            return
        body = self._body({'gamma_re': self._picked.real,
                           'gamma_im': self._picked.imag})
        self.search_button.setEnabled(False)
        self.move_button.setEnabled(False)

        def done(report):
            self._restore_buttons()
            self._verified(report)

        def failed(exc):
            self._restore_buttons()
            self.fail(exc, '移动或验证失败')

        run_in_background(
            lambda: handlers.studio_move_to_gamma(self.state, body),
            on_done=done, on_error=failed,
            description='smith pick move verify')

    def _verified(self, report: dict) -> None:
        target_g = complex(report.get('target_gamma_real', 0.0),
                           report.get('target_gamma_imag', 0.0))
        measured_g = complex(report.get('measured_gamma_real', 0.0),
                             report.get('measured_gamma_imag', 0.0))
        tolerance = report.get('tolerance', 0.05)
        error = report.get('vector_error', 0.0)
        passed = bool(report.get('within_tolerance', False))
        position = ' | '.join(f'{v:.4f}' for v in report.get('position', []))
        kind = {'calibrated point': '已标定点', 'interpolated': '插值位置'}.get(
            report.get('prediction_kind'), report.get('prediction_kind', '—'))
        pct_r = report.get('impedance_delta_r_percent')
        pct_x = report.get('impedance_delta_x_percent')
        # F-24: the same spec that formats the tuning page formats this card.
        spec = SmithSpec(z0=self._z0(), tolerance=float(tolerance))
        target_card = spec.metrics(target_g)
        measured_card = spec.metrics(measured_g)
        lines = [
            '目标',
            f"  Z  {target_card['z_label']}",
            f"  Γ  {target_card['gamma_rect']}",
            f"  |Γ| {target_card['gamma_mag']:.4f}   VSWR {target_card['vswr']}",
            '到位',
            f"  机械位置 X|Z1|Z2|Z3   {position or '—'}",
            f"  预测来源 {kind}   "
            f"位置最大偏差 {report.get('position_error_max', 0):.4f} mm",
            '实测（到位后重新扫矢网）',
            f"  Γ  {measured_card['gamma_rect']}   "
            f"|Γ| {measured_card['gamma_mag']:.4f}   "
            f"∠ {measured_card['gamma_phase_deg']:+.2f}°",
            f"  Z  {measured_card['z_label']}",
            f"  VSWR {measured_card['vswr']}   "
            f"回波损耗 {measured_card['return_loss']}",
            '偏差与判定',
            f"  |ΔΓ| {error:.6f}   {'≤' if passed else '>'} 容差 {tolerance:.5f}"
            f"   →   {'通过' if passed else '超出容差'}",
            f"  ΔR {report.get('impedance_delta_r', 0):+.3f} Ω "
            f"({self._fmt(pct_r, '{:+.2f}%')})   "
            f"ΔX {report.get('impedance_delta_x', 0):+.3f} Ω "
            f"({self._fmt(pct_x, '{:+.2f}%')})",
            f"  ΔVSWR {self._fmt(report.get('vswr_delta'), '{:+.2f}')}",
            f"  可达带 {target_card['band_label']}",
            f"  来源 {'实机 VNA（非模拟）' if not report.get('simulated', True) else '模拟数据'}"
            f"   RF 硬件认证 = {report.get('rf_hardware_verified')}",
        ]
        self.verdict.setPlainText('\n'.join(lines))
        self.verdict.setStyleSheet('color:#1f7a4d;' if passed else 'color:#b03a2e;')
        self.report(f"Smith 点选闭环：|ΔΓ| = {error:.6f} · "
                    f"{'通过' if passed else '超差'}")
        self.refresh()


class StudioTuningPage(_StudioPage):
    """Search, move and verify one target impedance."""

    def __init__(self, app_state, parent=None):
        super().__init__(app_state, parent)
        self._candidates: list[dict] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)
        root.addLayout(_page_title(
            '阻抗调谐',
            '最近邻给出「已标定点」里最接近的一个；插值调谐在其邻域用二阶模型细化。'
            '插值结果是预测值，机械到位不等于阻抗达标，必须实测验证。'))

        form_card = Card('目标与算法')
        form = field_grid()
        self.frequency = self._num(10.0, 0.001, 200.0, ' GHz', 6)
        self.sync_frequency_from_library(self.frequency)
        self.resistance = self._num(50.0, 0.0, 1e6, ' Ω', 3)
        self.reactance = self._num(0.0, -1e6, 1e6, ' Ω', 3)
        self.tolerance = self._num(0.08, 0.00001, 1.0, '', 5)
        target_row = QHBoxLayout()
        target_row.setSpacing(10)
        for label, widget in (('频率', self.frequency), ('R', self.resistance),
                              ('X', self.reactance), ('容差 |ΔΓ|', self.tolerance)):
            target_row.addWidget(field_label(label))
            target_row.addWidget(widget)
        form.addWidget(field_label('目标'), 0, 0)
        form.addLayout(target_row, 0, 1)

        self.method = QComboBox()
        for key, label in (('nearest', '最近标定点'), ('general', '两段式 · 一般搜索'),
                           ('gradient', '两段式 · 梯度搜索'),
                           ('two-stage', '两段式 · 完整（推荐）')):
            self.method.addItem(label, key)
        self.method.setCurrentIndex(3)
        self.level_policy = QComboBox()
        self.level_policy.addItem('就近三层（推荐）', 'closest')
        self.level_policy.addItem('原始规则（下 1 层 + 上 2 层）', 'classic')
        algo = QHBoxLayout()
        algo.setSpacing(10)
        algo.addWidget(field_label('算法'))
        algo.addWidget(self.method)
        algo.addWidget(field_label('层选择'))
        algo.addWidget(self.level_policy)
        algo.addStretch()
        form.addWidget(field_label(''), 1, 0)
        form.addLayout(algo, 1, 1)

        # F-21: Tune-To dual mode.  ``closest`` snaps to the nearest calibrated
        # point; ``cursor`` interpolates at the Smith-chart cursor, which is
        # the "Tune to cursor" mode.  Picking a mode overrides the algorithm box
        # above, so the two never fight over the same setting.
        mode_row = QHBoxLayout()
        mode_row.setSpacing(10)
        self.tune_to_mode = QComboBox()
        self.tune_to_mode.addItem('跟随算法选择（默认）', '')
        self.tune_to_mode.addItem('贴近目标 (closest · 不插值)', 'closest')
        self.tune_to_mode.addItem('跟随光标 (cursor · 插值)', 'cursor')
        self.tune_to_mode.setCurrentIndex(0)
        self.tune_to_mode.currentIndexChanged.connect(self._sync_mode)
        mode_row.addWidget(field_label('Tune To 模式'))
        mode_row.addWidget(self.tune_to_mode)
        mode_row.addStretch()
        form.addWidget(field_label(''), 2, 0)
        form.addLayout(mode_row, 2, 1)
        form_card.body.addLayout(form)
        mode_note = QLabel('「贴近目标」直接给出最近的已标定点；'
                           '「跟随光标」在 Smith 图光标处做插值。'
                           '选定后上方「算法」由模式决定。')
        mode_note.setObjectName('PageHint')
        mode_note.setWordWrap(True)
        form_card.body.addWidget(mode_note)
        root.addWidget(form_card)

        actions = QHBoxLayout()
        self.search_button = QPushButton('搜索候选')
        self.search_button.setProperty('accent', True)
        self.move_candidate_button = QPushButton('移动到选中候选')
        self.move_tuned_button = QPushButton('移动到插值结果')
        self.verify_button = QPushButton('实测验证当前位置')
        for widget in (self.search_button, self.move_candidate_button,
                       self.move_tuned_button, self.verify_button):
            widget.setEnabled(False)
            actions.addWidget(widget)
        actions.addStretch()
        root.addLayout(actions)
        self.search_button.setEnabled(True)

        # Results area — side by side instead of one tall vertical stack.
        # The old stack gave ``self.result`` stretch=1 so the log swallowed every
        # spare pixel and squeezed the deviation card down to its header, which
        # read as overlapping text on a short window.  Give the candidate table
        # and the process log a left column (split vertically between them) and
        # the target-vs-measured card its own full-height right column.  Every
        # block keeps a minimum height so nothing collapses even on a 1140 px
        # window; the two columns stay readable at every supported width.
        results = QHBoxLayout()
        results.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(10)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ['#', '机械位置', '预测 Γ', '预测 Z (Ω)', '|ΔΓ|', '可达'])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(150)
        left.addWidget(self.table, 3)

        self.result = QTextEdit()
        self.result.setReadOnly(True)
        self.result.setFont(numeric_font(9))
        self.result.setPlaceholderText('候选、插值各阶段误差与实测验证结果会显示在这里')
        self.result.setMinimumHeight(110)
        left.addWidget(self.result, 2)
        results.addLayout(left, 3)

        # F-19: the target-vs-measured card, in the canonical Smith format.
        # Let it own the full column height; its own table/detail caps keep the
        # content compact without ever colliding with the log on the left.
        self.deviation = DeviationCard()
        self.deviation.setMinimumHeight(260)
        results.addWidget(self.deviation, 2)
        root.addLayout(results, 1)

        self.search_button.clicked.connect(self.on_search)
        self.move_candidate_button.clicked.connect(self.on_move_candidate)
        self.move_tuned_button.clicked.connect(self.on_move_tuned)
        self.verify_button.clicked.connect(self.on_verify)

    def showEvent(self, event):  # noqa: N802 - Qt override
        """Adopt the loaded library's frequency band when the page is shown."""
        super().showEvent(event)
        self.sync_frequency_from_library(self.frequency)

    def on_library_changed(self, path: str) -> None:
        """Discard candidates produced against the previous calibration.

        The backend already refuses a stale ``last_tuned`` by fingerprint, but
        the table, the result text and the deviation card are plain widgets:
        they would keep showing the old library's numbers.  Clear them and
        re-enable only the search action, so the operator cannot move the
        machine to a position that belongs to another calibration.
        """
        self.sync_frequency_from_library(self.frequency)
        self._candidates = []
        self.table.setRowCount(0)
        self.result.clear()
        self.deviation.clear()
        for button in (self.move_candidate_button, self.move_tuned_button,
                       self.verify_button):
            button.setEnabled(False)
        self.search_button.setEnabled(True)
        if path:
            self.result.setPlaceholderText(
                f'当前校准库已切换为 {Path(path).name}，请重新搜索候选。')

    @staticmethod
    def _num(value: float, low: float, high: float, suffix: str,
             decimals: int) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(decimals)
        spin.setSuffix(suffix)
        spin.setValue(value)
        return spin

    def _sync_mode(self) -> None:
        """F-21: a Tune-To mode decides the algorithm, so show which one is live."""
        mode = self.tune_to_mode.currentData()
        if mode == 'closest':
            self.method.setCurrentIndex(self.method.findData('nearest'))
            self.method.setEnabled(False)
        elif mode == 'cursor':
            self.method.setCurrentIndex(self.method.findData('two-stage'))
            self.method.setEnabled(False)
        else:
            self.method.setEnabled(True)

    def _body(self) -> dict:
        body = {
            'frequency': self.frequency.value() * 1e9,
            'R': self.resistance.value(),
            'X': self.reactance.value(),
            'tolerance': self.tolerance.value(),
            'method': self.method.currentData(),
            'level_policy': self.level_policy.currentData(),
        }
        mode = self.tune_to_mode.currentData()
        if mode:
            body['tune_to_mode'] = mode
        return body

    # -- actions ---------------------------------------------------------
    def on_search(self) -> None:
        self.search_button.setEnabled(False)
        self.run(lambda: handlers.studio_tune(self.state, self._body()),
                 self._searched, title='搜索失败', description='studio tune')

    def _searched(self, data: dict) -> None:
        self.search_button.setEnabled(True)
        self._candidates = data.get('candidates', [])
        spec = SmithSpec(tolerance=self.tolerance.value())
        self.table.setRowCount(len(self._candidates))
        for row, item in enumerate(self._candidates):
            position = ' | '.join(f'{v:.4f}' for v in item['position'])
            gamma = complex(item['gamma']['re'], item['gamma']['im'])
            metrics = spec.metrics(gamma)
            values = (
                str(item['index']), position,
                metrics['gamma_rect'],
                metrics['z_label'],
                f"{item['error']:.6g}", '是' if item['reachable'] else '否',
            )
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setFont(numeric_font(9))
                self.table.setItem(row, column, cell)
        if self._candidates:
            self.table.selectRow(0)
        self.move_candidate_button.setEnabled(bool(self._candidates))
        self.verify_button.setEnabled(True)

        lines = []
        tuned = data.get('tuned')
        if tuned:
            if 'position' in tuned:
                self.move_tuned_button.setEnabled(True)
                tuned_gamma = complex(tuned['gamma']['re'], tuned['gamma']['im'])
                tuned_card = spec.metrics(tuned_gamma)
                lines.append('插值结果')
                lines.append('  位置  ' + ' | '.join(f'{v:.4f}' for v in tuned['position']))
                lines.append(f"  预测 Γ {tuned_card['gamma_rect']}")
                lines.append(f"  预测 Z {tuned_card['z_label']}")
                lines.append(f"  |ΔΓ| {tuned['error']:.6g}")
                for stage in tuned.get('stages', []):
                    lines.append(f"  {stage['stage']:<10} |ΔΓ|={stage['error']:.6g}")
            else:
                self.move_tuned_button.setEnabled(False)
                lines.append(f"插值调谐失败: {tuned.get('error')}")
        elif data.get('method') == 'nearest':
            lines.append('仅搜索了最近标定点；如需插值请选择两段式算法。')
        mode = data.get('tune_to_mode')
        if mode:
            lines.append(f"Tune To 模式: {'贴近目标 (closest)' if mode == 'closest' else '跟随光标 (cursor)'}")
        self.result.setPlainText('\n'.join(lines) if lines else '无候选')
        self.report(f"搜索完成: {len(self._candidates)} 个候选")

    def on_move_candidate(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, '移动', '请先在表格中选择一个候选')
            return
        body = {'index': row}
        self.run(lambda: handlers.studio_move_candidate(self.state, body),
                 lambda result: self._moved(result, '已移动到选中候选'),
                 title='移动失败', description='studio move candidate')

    def on_move_tuned(self) -> None:
        self.run(lambda: handlers.studio_move_tuned(self.state, {}),
                 lambda result: self._moved(result, '已移动到插值位置'),
                 title='移动失败', description='studio move tuned')

    def _moved(self, result: dict, message: str) -> None:
        position = ' | '.join(f'{v:.4f}' for v in result.get('position', []))
        self.result.append(f'{message}\n  实际位置 {position}\n'
                           f"  预测 Γ {result.get('predicted_gamma')}")
        self.report(message)

    def on_verify(self) -> None:
        self.run(lambda: handlers.studio_verify(self.state, self._body()),
                 self._verified, title='验证失败', description='studio verify')

    def _verified(self, report: dict) -> None:
        z_real = report.get('measured_impedance_real')
        z_imag = report.get('measured_impedance_imag')
        vswr = report.get('measured_vswr')
        spec = SmithSpec(tolerance=float(report.get('tolerance', 0.05)))
        measured = complex(report.get('measured_gamma_real', 0.0),
                           report.get('measured_gamma_imag', 0.0))
        card = spec.metrics(measured)
        z_text = ('—' if z_real is None or z_imag is None
                  else f'{z_real:+.3f}{z_imag:+.3f}j Ω')
        vswr_text = '∞ : 1' if vswr is None else f'{vswr:.2f} : 1'
        self.result.append(
            '实测验证\n'
            f"  频率 {report['frequency_hz'] / 1e9:.6f} GHz\n"
            f"  实测 Γ {card['gamma_rect']}  "
            f"|Γ| {card['gamma_mag']:.4f}  "
            f"∠ {card['gamma_phase_deg']:+.2f}°\n"
            f"  实测 Z {z_text}  VSWR {vswr_text}\n"
            f"  |ΔΓ| {report['vector_error']:.6g}  "
            f"{'在容差内' if report['within_tolerance'] else '超出容差'}"
            f"  · ΔR {report.get('impedance_delta_r', 0):+.3f} Ω"
            f"  · ΔX {report.get('impedance_delta_x', 0):+.3f} Ω\n"
            f"  来源 {'模拟' if report['simulated'] else '实机'}；"
            f"RF 硬件认证 = {report['rf_hardware_verified']}")
        self._show_deviation(report)
        self.report(f"实测 |ΔΓ| = {report['vector_error']:.6g}")

    def _show_deviation(self, report: dict) -> None:
        """F-19: fill the target-vs-measured card from a verification report."""
        target = complex(report.get('target_gamma_real', 0.0),
                         report.get('target_gamma_imag', 0.0))
        measured = complex(report.get('measured_gamma_real', 0.0),
                           report.get('measured_gamma_imag', 0.0))
        spec = SmithSpec(tolerance=float(report.get('tolerance', 0.05)))
        self.deviation.show_report(spec.comparison_card(target, measured))


class StudioValidationPage(_StudioPage):
    """Interpolation validation and A→B→A repeatability."""

    def __init__(self, app_state, parent=None):
        super().__init__(app_state, parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)
        root.addLayout(_page_title(
            '验证报告',
            '插值验证在标定点「之间」取点实测，误差矢量超出容差就说明模型不可信，应当重新标定；'
            '重复性验证做 A→B→A 往返，衡量机械重复定位。'))

        form_box = QGroupBox('插值验证')
        form = QFormLayout(form_box)
        row = QHBoxLayout()
        self.count = QSpinBox()
        self.count.setRange(1, 50)
        self.count.setValue(5)
        self.tolerance = QDoubleSpinBox()
        self.tolerance.setRange(0.0001, 1.0)
        self.tolerance.setDecimals(4)
        self.tolerance.setSingleStep(0.005)
        self.tolerance.setValue(0.08)
        self.strategy = QComboBox()
        self.strategy.addItem('覆盖优先（最难的点）', 'coverage')
        self.strategy.addItem('随机均匀', 'random')
        row.addWidget(QLabel('点数'))
        row.addWidget(self.count)
        row.addWidget(QLabel('容差'))
        row.addWidget(self.tolerance)
        row.addWidget(QLabel('选点策略'))
        row.addWidget(self.strategy)
        row.addStretch()
        form.addRow('参数', row)

        buttons = QHBoxLayout()
        self.interp_button = QPushButton('运行插值验证')
        self.interp_button.setProperty('accent', True)
        self.repeat_button = QPushButton('运行重复性验证 (A→B→A ×3)')
        buttons.addWidget(self.interp_button)
        buttons.addWidget(self.repeat_button)
        buttons.addStretch()
        form.addRow('', buttons)
        root.addWidget(form_box)

        self.verdict = QLabel('—')
        self.verdict.setWordWrap(True)
        self.verdict.setFont(numeric_font(10, bold=True))
        root.addWidget(self.verdict)

        # F-20: manual repeatability panel.  The automatic A→B→A run above
        # drives the machine; this panel analyses measurements the operator
        # already has (from a log, a CSV, or a manual run).
        repeat_box = Card('重复性统计 (F-20)')
        repeat_layout = repeat_box.body
        repeat_params = QHBoxLayout()
        repeat_params.setSpacing(10)
        self.repeat_target = QLineEdit('')
        self.repeat_target.setPlaceholderText('目标 Γ (re, im)，可留空')
        self.repeat_freq = QDoubleSpinBox()
        self.repeat_freq.setRange(0.001, 200.0)
        self.repeat_freq.setDecimals(6)
        self.repeat_freq.setSuffix(' GHz')
        self.repeat_freq.setValue(5.0)
        self.repeat_repeats = QSpinBox()
        self.repeat_repeats.setRange(1, 20)
        self.repeat_repeats.setValue(3)
        repeat_params.addWidget(field_label('目标 Γ'))
        repeat_params.addWidget(self.repeat_target, 1)
        repeat_params.addWidget(field_label('频率'))
        repeat_params.addWidget(self.repeat_freq)
        repeat_params.addWidget(field_label('循环次数'))
        repeat_params.addWidget(self.repeat_repeats)
        repeat_layout.addLayout(repeat_params)

        # The automatic variant drives the machine: it is a separate, clearly
        # marked button with its own confirmation, never a silent extra step.
        auto_row = QHBoxLayout()
        auto_row.setSpacing(8)
        self.repeat_auto_button = QPushButton('自动 A→B→A 采集（会驱动机器）')
        self.repeat_auto_button.setObjectName('DangerButton')
        self.repeat_auto_button.clicked.connect(self.on_auto_repeatability)
        auto_row.addWidget(self.repeat_auto_button)
        auto_row.addWidget(hint_label(
            '用库里首末两个有效点做 A→B→A，每循环测 3 段（A / B / 返回 A）。'
            '会实际移动机械，请在确认回零与行程后使用。'))
        auto_row.addStretch()
        repeat_layout.addLayout(auto_row)

        self.repeat_input = QTextEdit()
        self.repeat_input.setPlaceholderText(
            '每行一次复测的 Γ，格式 "re, im" 或仅 "re"：\n'
            '0.12, -0.34\n0.13, -0.35\n0.11, -0.33')
        self.repeat_input.setMaximumHeight(110)
        repeat_layout.addWidget(self.repeat_input)

        repeat_actions = QHBoxLayout()
        self.repeat_analyse_button = QPushButton('分析重复性')
        self.repeat_analyse_button.setProperty('accent', True)
        self.repeat_analyse_button.clicked.connect(self.on_analyse_repeatability)
        repeat_actions.addWidget(self.repeat_analyse_button)
        repeat_actions.addStretch()
        repeat_layout.addLayout(repeat_actions)

        self.repeat_result = QTextEdit()
        self.repeat_result.setReadOnly(True)
        self.repeat_result.setFont(numeric_font(9))
        self.repeat_result.setMaximumHeight(150)
        self.repeat_result.setPlaceholderText(
            '均值 / σ / 最大 |ΔΓ| / 复位 dB / 高 |Γ| 区分级会显示在这里')
        repeat_layout.addWidget(self.repeat_result)
        root.addWidget(repeat_box)

        self.report_view = QTextEdit()
        self.report_view.setReadOnly(True)
        self.report_view.setFont(numeric_font(9))
        root.addWidget(self.report_view, 1)

        save_row = QHBoxLayout()
        self.save_button = QPushButton('保存报告 JSON…')
        save_row.addWidget(self.save_button)
        save_row.addStretch()
        root.addLayout(save_row)

        self.interp_button.clicked.connect(self.on_validate_interpolation)
        self.repeat_button.clicked.connect(self.on_validate_repeatability)
        self.save_button.clicked.connect(self.on_save)

        self.timer = QTimer(self)
        self.timer.setInterval(1200)
        self.timer.timeout.connect(self.refresh_report)
        self.timer.start()

    def on_library_changed(self, path: str) -> None:
        """A validation verdict belongs to exactly one calibration library.

        ``LibraryContext`` clears the shared report, but the verdict label and
        the JSON text view are widgets: without this hook the previous library's
        "通过" would stay on screen until the next run.  The repeatability
        frequency is aligned to the new band at the same time.
        """
        self.sync_frequency_from_library(self.repeat_freq)
        self.verdict.setText('—')
        self.verdict.setStyleSheet('')
        self.report_view.clear()
        if path:
            self.report_view.setPlaceholderText(
                f'当前校准库：{Path(path).name} · 请重新运行插值验证。')

    # -- actions ---------------------------------------------------------
    def on_validate_interpolation(self) -> None:
        body = {'count': self.count.value(), 'tolerance': self.tolerance.value(),
                'strategy': self.strategy.currentData()}
        try:
            handlers.studio_validate_interpolation(self.state, body)
        except Exception as exc:  # noqa: BLE001
            self.fail(exc, '启动插值验证失败')
            return
        self.verdict.setText('插值验证已在后台启动，完成后自动刷新…')
        self.report('插值验证已启动')

    def on_validate_repeatability(self) -> None:
        try:
            handlers.studio_validate(self.state, {})
        except Exception as exc:  # noqa: BLE001
            self.fail(exc, '启动重复性验证失败')
            return
        self.verdict.setText('重复性验证已在后台启动…')
        self.report('重复性验证已启动')

    def refresh_report(self) -> None:
        report = None
        try:
            report = (handlers.studio_overview(self.state, {}).get('validation_report'))
        except Exception:  # noqa: BLE001
            return
        if not report:
            return
        # The automatic A→B→A run writes its own report; show its numbers in
        # the repeatability panel, where the operator is looking for them.
        if report.get('kind') == 'repeatability_auto':
            self._show_auto_repeatability(report)
        passed = report.get('passed')
        if passed is None:
            passed = report.get('within_tolerance')
        if passed is None:
            self.verdict.setText(report.get('kind', '报告'))
        else:
            self.verdict.setText(('通过' if passed else '未通过') +
                                 ' · ' + str(report.get('message', report.get('kind', ''))))
            self.verdict.setStyleSheet(
                'color:#1f7a4d;' if passed else 'color:#b03a2e;')
        self.report_view.setPlainText(json.dumps(report, indent=2, ensure_ascii=False))

    # -- F-20: manual repeatability analysis --------------------------------
    def _parse_measurements(self) -> list[dict]:
        """Parse the ``re, im`` lines the operator typed in."""
        measurements = []
        for number, line in enumerate(self.repeat_input.toPlainText().splitlines(),
                                      start=1):
            text = line.strip()
            if not text or text.startswith('#'):
                continue
            parts = [piece for piece in text.replace('，', ',').split(',')
                     if piece.strip()]
            try:
                values = [float(piece) for piece in parts]
            except ValueError:
                raise ValueError(f'第 {number} 行不是数字: {text!r}')
            if len(values) == 1:
                values.append(0.0)
            if len(values) != 2:
                raise ValueError(f'第 {number} 行应有两个数字 (re, im): {text!r}')
            measurements.append({'gamma_re': values[0], 'gamma_im': values[1]})
        return measurements

    def on_analyse_repeatability(self) -> None:
        try:
            measurements = self._parse_measurements()
        except ValueError as exc:
            self.repeat_result.setPlainText(f'输入有误：{exc}')
            return
        if not measurements:
            self.repeat_result.setPlainText('没有可分析的复测数据')
            return
        body = {'measurements': measurements,
                'tolerance': self.tolerance.value(),
                'frequency_hz': self.repeat_freq.value() * 1e9}
        target = self.repeat_target.text().strip()
        if target:
            parts = [piece for piece in target.replace('，', ',').split(',')
                     if piece.strip()]
            try:
                values = [float(piece) for piece in parts]
            except ValueError:
                self.repeat_result.setPlainText(f'目标 Γ 格式有误: {target!r}')
                return
            if len(values) == 1:
                values.append(0.0)
            if len(values) != 2:
                self.repeat_result.setPlainText('目标 Γ 应为 "re, im"')
                return
            body['target_gamma_re'] = values[0]
            body['target_gamma_im'] = values[1]
        self.run(lambda: handlers.studio_repeatability(self.state, body),
                 self._repeatability_done, title='重复性分析失败',
                 description='repeatability')

    def _repeatability_done(self, report: dict) -> None:
        reset_db = report.get('reset_dB')
        reset_text = '∞' if reset_db in (None, float('inf')) else f'{reset_db:.1f} dB'
        high = report.get('high_gamma')
        lines = [
            f"样本数 N = {report.get('n')}",
            f"平均 |ΔΓ| = {report.get('mean_delta_gamma', 0):.6f}",
            f"标准差 σ   = {report.get('std_delta_gamma', 0):.6f}",
            f"最大 |ΔΓ| = {report.get('max_delta_gamma', 0):.6f}",
            f"复位指标    = {reset_text}",
            f"容差 {report.get('tolerance', 0):g} 内: "
            f"{'全部通过' if report.get('all_within_tolerance') else '存在超差'}",
        ]
        if high:
            lines.append(
                f"高 |Γ| 区 (n={high.get('n')}): 平均 {high.get('mean_delta_gamma', 0):.6f}"
                f" · 最大 {high.get('max_delta_gamma', 0):.6f}")
        else:
            lines.append('高 |Γ| 区: 本次样本未落入该区')
        self.repeat_result.setPlainText('\n'.join(lines))
        self.report(f"重复性分析完成: N={report.get('n')}")

    # -- F-20: automatic A→B→A acquisition (moves the machine) --------------
    def _show_auto_repeatability(self, report: dict) -> None:
        """Render the automatic A→B→A report in the repeatability panel."""
        reset_db = report.get('reset_dB')
        reset_text = '∞' if reset_db in (None, float('inf')) else f'{reset_db:.1f} dB'
        lines = [
            f"自动 A→B→A 采集完成 · 循环 {report.get('n', 0)} 次测量",
            f"平均 |ΔΓ| = {report.get('mean_delta_gamma', 0):.6f}",
            f"标准差 σ   = {report.get('std_delta_gamma', 0):.6f}",
            f"最大 |ΔΓ| = {report.get('max_delta_gamma', 0):.6f}",
            f"复位指标    = {reset_text}",
        ]
        hysteresis = report.get('hysteresis') or []
        if hysteresis:
            lines.append('每个循环的返回偏差（A→B→回到 A）:')
            for item in hysteresis:
                lines.append(f"  {item.get('label', '')}: "
                             f"|ΔΓ| = {item.get('delta_gamma', 0):.6f}")
            lines.append(f"最大返回偏差 = {report.get('hysteresis_max', 0):.6f}")
        high = report.get('high_gamma')
        if high:
            lines.append(
                f"高 |Γ| 区 (n={high.get('n')}): 平均 {high.get('mean_delta_gamma', 0):.6f}"
                f" · 最大 {high.get('max_delta_gamma', 0):.6f}")
        self.repeat_result.setPlainText('\n'.join(lines))
        self.repeat_auto_button.setEnabled(True)
        self.repeat_auto_button.setText('自动 A→B→A 采集（会驱动机器）')
        self.verdict.setText(
            f"自动重复性采集完成 · 最大 |ΔΓ| = {report.get('max_delta_gamma', 0):.6f}"
            f" · 最大返回偏差 = {report.get('hysteresis_max', 0):.6f}")

    def on_auto_repeatability(self) -> None:
        """Start the automatic A→B→A run.

        This one *moves the machine*, so it asks twice: once for the motion
        itself and once more for the exact travel.  The historical mistake this
        guards against is an unattended run starting because a button was
        clicked twice.
        """
        body = {
            'repeats': self.repeat_repeats.value(),
            'tolerance': self.tolerance.value(),
            'frequency_hz': self.repeat_freq.value() * 1e9,
        }
        target = self.repeat_target.text().strip()
        if target:
            parts = [piece for piece in target.replace('，', ',').split(',')
                     if piece.strip()]
            try:
                values = [float(piece) for piece in parts]
            except ValueError:
                self.repeat_result.setPlainText(f'目标 Γ 格式有误: {target!r}')
                return
            if len(values) == 1:
                values.append(0.0)
            if len(values) != 2:
                self.repeat_result.setPlainText('目标 Γ 应为 "re, im"')
                return
            body['target_gamma'] = {'re': values[0], 'im': values[1]}

        answer = QMessageBox.warning(
            self,
            '自动重复性采集确认',
            f'即将驱动机器做 A→B→A 往返，共 {body["repeats"]} 个循环、'
            f'{body["repeats"] * 3} 次测量。\n\n'
            'A 点取库里第一个有效点，B 点取最后一个有效点，因此机械行程较长。\n\n'
            '请确认：\n'
            '· 四轴已回零\n'
            '· 急停可用、行程内无干涉\n'
            '· 探针处于安全高度\n\n'
            '是否继续？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            self.repeat_result.setPlainText('已取消自动重复性采集')
            self.report('已取消自动重复性采集')
            return

        self.repeat_auto_button.setEnabled(False)
        self.repeat_auto_button.setText('采集中…')
        self.repeat_result.setPlainText(
            '正在后台驱动机器做 A→B→A…\n'
            '完成后这里会显示均值 / σ / 最大 |ΔΓ| / 复位 dB 与每个循环的返回偏差。')
        self.run(lambda: handlers.studio_repeatability_auto(self.state, body),
                 self._auto_repeatability_started,
                 title='自动重复性采集失败', description='repeatability auto')

    def _auto_repeatability_started(self, result: dict) -> None:
        self.repeat_auto_button.setEnabled(True)
        self.repeat_auto_button.setText('自动 A→B→A 采集（会驱动机器）')
        self.verdict.setText('自动重复性采集已启动，完成后自动刷新…')
        self.report('自动重复性采集已启动')

    def on_save(self) -> None:
        try:
            report = handlers.studio_overview(self.state, {}).get('validation_report')
        except Exception:  # noqa: BLE001
            pass
        if not report:
            QMessageBox.information(self, '保存报告', '还没有可保存的验证报告')
            return
        target, _ = QFileDialog.getSaveFileName(
            self, '保存验证报告', str(paths.default_report_path('validation')),
            'JSON (*.json)')
        if not target:
            return
        Path(target).write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
        self.report(f'验证报告已保存: {target}')


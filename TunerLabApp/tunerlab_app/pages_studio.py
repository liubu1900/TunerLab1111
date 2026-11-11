"""Calibration workbench pages: overview, point planning, acquisition, database.

Like the VNA page these are thin clients over :mod:`tuner.web.handlers`; the
calibration algorithm, the database format and the motion safety rules stay in
exactly one place.  Anything that moves the machine or talks to an instrument
runs on the thread pool.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

from tuner.ui import SmithChart
from tuner.ui_theme import numeric_font, text_font
from tuner.web import handlers
from . import paths, studio_bridge
from .bg import run_in_background
from .pages_base import _page_title, _StudioPage
from .pages_p1 import ReferencePlanePanel
from .point_cloud import ProgressCloudWidget
from .ui import (Card, SmithPreview, field_grid, field_label, hint_label,
                   page_header, smith_control_row)
from .wizard import WIZARD_STEPS, Wizard, WizardStep
from .widgets import StatusPill


class StudioOverviewPage(_StudioPage):
    """One glance at the machine, the instrument, the database and the task."""

    def __init__(self, app_state, parent=None):
        super().__init__(app_state, parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)
        root.addLayout(_page_title(
            '校准概览',
            '这里只汇总状态，不改任何东西。第一次使用请按「布点与设置」里的 5 个步骤走。'))

        card = QGroupBox('当前状态')
        grid = QGridLayout(card)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        self.values: dict[str, QLabel] = {}
        rows = [
            ('controller', '控制器'), ('motion', '运动来源'),
            ('vna', '矢网来源'), ('database', '校准库'),
            ('points', '有效点 / 总点'), ('task', '任务状态'),
        ]
        for row, (key, label) in enumerate(rows):
            title = QLabel(label)
            title.setStyleSheet('color:#5b6b7c;')
            value = QLabel('—')
            value.setFont(numeric_font(10, bold=True))
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(title, row, 0)
            grid.addWidget(value, row, 1)
            self.values[key] = value
        grid.setColumnStretch(1, 1)
        root.addWidget(card)

        steps = QGroupBox('推荐流程')
        steps_layout = QVBoxLayout(steps)
        for index, text in enumerate((
            '① 连接控制器（真实或虚拟），读取固件与逐轴回零状态',
            '② 回零 → 标度自检；比值不为 1 先修 UNITS，再谈校准',
            '③ 申报行程、选择布点方式、建立校准库（会显示点数与预估耗时）',
            '④ 采集（可暂停/取消/断点续扫），完成后自动加载',
            '⑤ Smith 图 / 阻抗调谐 / 插值验证，导出报告',
        ), start=1):
            label = QLabel(text)
            label.setWordWrap(True)
            steps_layout.addWidget(label)
        root.addWidget(steps)
        root.addStretch()

        buttons = QHBoxLayout()
        refresh = QPushButton('刷新')
        goto_setup = QPushButton('去布点与设置')
        goto_setup.setProperty('accent', True)
        goto_run = QPushButton('去采集运行')
        buttons.addWidget(refresh)
        buttons.addWidget(goto_setup)
        buttons.addWidget(goto_run)
        buttons.addStretch()
        root.addLayout(buttons)

        refresh.clicked.connect(self.refresh)
        goto_setup.clicked.connect(lambda: self.navigate.emit('studio-setup'))
        goto_run.clicked.connect(lambda: self.navigate.emit('studio-run'))

        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self) -> None:
        try:
            overview = handlers.studio_overview(self.state, {})
        except Exception:  # noqa: BLE001 - polling
            return
        hw = self.state.hw_status()
        if hw['connected']:
            mode = '虚拟' if hw['mode'] == 'virtual' else '真实'
            self.values['controller'].setText(
                f"{mode} · {hw['address'] or hw['ip']} · 固件 {hw['firmware'] or '未知'}")
        else:
            self.values['controller'].setText('未连接')
        self.values['motion'].setText(
            {'mock': '模拟运动', 'real': '实机运动'}.get(
                overview.get('motion_source'), '未初始化'))
        self.values['vna'].setText(
            {'mock': '模拟矢网', 'real': '真实矢网'}.get(
                overview.get('vna_source'), '未初始化'))
        self.values['database'].setText(overview.get('database_path') or '未建立')
        self.values['points'].setText(
            f"{overview.get('valid', 0)} / {overview.get('total', 0)}")
        self.values['task'].setText(str(overview.get('calibration_state', 'idle')))


class StudioSetupPage(_StudioPage):
    """Point planning and database creation."""

    created = Signal(str)

    def __init__(self, app_state, parent=None):
        super().__init__(app_state, parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 0, 22, 18)
        root.setSpacing(14)
        root.addWidget(page_header(
            '布点与设置',
            '五步建库向导：参考面 → 运动行程 → 频率方案 → 布点计划 → 检查建库。'
            '每一步只问该问的参数，建库前会先过前提条件硬门禁。'))

        self.wizard = Wizard()
        root.addWidget(self.wizard, 1)

        # ---- step 1: reference plane and instrument --------------------
        step = WizardStep(0, *WIZARD_STEPS[0][1:])
        self.reference_panel = ReferencePlanePanel(self.state)
        step.add_card(self.reference_panel)

        vna_card = Card('矢网')
        vna_grid = field_grid()
        self.vna_vendor = QComboBox()
        for key, label in handlers.vna_vendors(self.state, {}).get('vendors', {}).items():
            self.vna_vendor.addItem(label, key)
        self.vna_vendor.insertItem(0, '自动识别', 'auto')
        self.vna_ip = QLineEdit('192.168.2.111')
        self.vna_interface = QComboBox()
        for key, label in (handlers.vna_interfaces(self.state, {})
                           .get('interfaces', {}).items()):
            self.vna_interface.addItem(label, key)
        self.vna_resource = QLineEdit('GPIB0::17::INSTR')
        self.vna_resource.setToolTip(
            'GPIB 主地址（如 17）或 VISA 资源名（如 GPIB0::17::INSTR）；'
            '82357B 需要安装 Keysight IO Libraries Suite。')
        self.vna_combo = QComboBox()
        self.vna_combo.addItem('模拟矢网', 'mock')
        self.vna_combo.addItem('真实矢网 (TCP/IP / GPIB)', 'real')
        self.vna_ip_label = field_label('IP 地址')
        self.vna_resource_label = field_label('VISA 资源')
        vna_grid.addWidget(field_label('矢网来源'), 0, 0)
        vna_grid.addWidget(self.vna_combo, 0, 1)
        vna_grid.addWidget(field_label('型号'), 1, 0)
        vna_grid.addWidget(self.vna_vendor, 1, 1)
        vna_grid.addWidget(field_label('接口'), 2, 0)
        vna_grid.addWidget(self.vna_interface, 2, 1)
        vna_grid.addWidget(self.vna_ip_label, 3, 0)
        vna_grid.addWidget(self.vna_ip, 3, 1)
        vna_grid.addWidget(self.vna_resource_label, 4, 0)
        vna_grid.addWidget(self.vna_resource, 4, 1)
        vna_card.body.addLayout(vna_grid)
        vna_card.body.addWidget(hint_label(
            '真实矢网可选 TCP/IP（默认 192.168.2.111:5025）或 GPIB（默认 '
            'GPIB0::17::INSTR，需 Keysight IO Libraries Suite）；'
            '用模拟矢网可在无仪器时完整走一遍流程。'))
        step.add_card(vna_card)
        self.wizard.add_step(step)

        # ---- step 2: motion and travel ---------------------------------
        step = WizardStep(1, *WIZARD_STEPS[1][1:])
        motion_card = Card('运动来源')
        motion_grid = field_grid()
        self.motion_combo = QComboBox()
        self.motion_combo.addItem('模拟运动（无需硬件）', 'mock')
        self.motion_combo.addItem('实机运动（Zmotion 控制器）', 'real')
        self.probes_combo = QComboBox()
        self.probes_combo.addItem('2 探针 (X/Z1/Z2)', 2)
        self.probes_combo.addItem('3 探针 (X/Z1/Z2/Z3)', 3)
        self.probes_combo.setCurrentIndex(1)
        motion_grid.addWidget(field_label('运动来源'), 0, 0)
        motion_grid.addWidget(self.motion_combo, 0, 1)
        motion_grid.addWidget(field_label('探针数'), 1, 0)
        motion_grid.addWidget(self.probes_combo, 1, 1)
        motion_card.body.addLayout(motion_grid)
        step.add_card(motion_card)

        travel_card = Card('申报行程 (mm)')
        travel = QGridLayout()
        travel.setHorizontalSpacing(10)
        travel.setVerticalSpacing(6)
        self.x_min = self._num(0.0)
        self.x_max = self._num(10.0)
        self.z_min = self._num(0.0)
        self.z_max = self._num(1.0)
        self.safe_z = self._num(0.0)
        travel_pairs = ((('X 下限', self.x_min), ('X 上限', self.x_max)),
                        (('Z 下限', self.z_min), ('Z 上限', self.z_max)),
                        (('安全 Z', self.safe_z), None))
        for row, pair in enumerate(travel_pairs):
            column = 0
            for entry in pair:
                if entry is None:
                    continue
                text, widget = entry
                travel.addWidget(field_label(text), row * 2, column)
                travel.addWidget(widget, row * 2 + 1, column)
                column += 2
        travel.setColumnStretch(1, 1)
        travel.setColumnStretch(3, 1)
        travel_card.body.addLayout(travel)
        travel_card.body.addWidget(hint_label(
            '实机运动时使用操作员申报的行程；控制器自身的 FS/RS_LIMIT 仍是最外层保护。'))
        step.add_card(travel_card)
        self.wizard.add_step(step)

        # ---- step 3: frequency plan -------------------------------------
        step = WizardStep(2, *WIZARD_STEPS[2][1:])
        # F-05: four frequency-plan modes so a single spot frequency (the
        # default load-pull case) and harmonic / arbitrary lists are first-class.
        self.freq_mode = QComboBox()
        for key, label in (('fo_list', '单频点 (默认 5 GHz)'),
                           ('freq_sweep', '频带扫描'),
                           ('harmonic_list', '谐波列表'),
                           ('arbitrary_list', '任意频率列表')):
            self.freq_mode.addItem(label, key)
        self.freq_mode.setCurrentIndex(0)
        freq_card = Card('频率模式')
        freq_grid = field_grid()
        freq_grid.addWidget(field_label('频率模式'), 0, 0)
        freq_grid.addWidget(self.freq_mode, 0, 1)
        freq_card.body.addLayout(freq_grid)
        step.add_card(freq_card)

        vna_card = Card('矢网频率')
        vna_grid = QGridLayout()
        vna_grid.setHorizontalSpacing(10)
        vna_grid.setVerticalSpacing(6)
        self.vna_start = self._num(1.0)
        self.vna_stop = self._num(20.0)
        self.vna_points = QSpinBox()
        # A single spot frequency is a normal load-pull calibration; the
        # database, the interpolator and np.interp all handle a length-1 grid.
        self.vna_points.setRange(1, 20001)
        self.vna_points.setValue(401)
        self.fo_ghz = self._num(5.0)
        self.harmonics_edit = QLineEdit('1,2,3')
        # A usable default: the arbitrary-list mode starts with the three
        # harmonics of 5 GHz, so the preview is meaningful before the operator
        # types anything (an empty list is an invalid plan).
        self.arbitrary_edit = QLineEdit('5,10,15')
        # Each frequency field gets its own row so a mode switch can hide the
        # whole row (label *and* input) — a greyed-out field still reads as
        # "this is a setting you forgot", which is exactly the confusion the
        # screenshot showed.
        self._freq_rows: dict[str, tuple[QLabel, QWidget]] = {}
        freq_fields = (
            ('start', '起始 GHz', self.vna_start),
            ('stop', '终止 GHz', self.vna_stop),
            ('points', '频点', self.vna_points),
            ('fo', '基频 GHz', self.fo_ghz),
            ('harmonics', '谐波 n', self.harmonics_edit),
            ('arbitrary', '频率列表 GHz', self.arbitrary_edit),
        )
        for row, (key, text, widget) in enumerate(freq_fields):
            label = field_label(text)
            vna_grid.addWidget(label, row, 0)
            vna_grid.addWidget(widget, row, 1)
            self._freq_rows[key] = (label, widget)
        vna_grid.setColumnStretch(1, 1)
        vna_card.body.addLayout(vna_grid)
        self.freq_preview = QLabel('—')
        self.freq_preview.setFont(numeric_font(10, bold=True))
        self.freq_preview.setWordWrap(True)
        vna_card.body.addWidget(self.freq_preview)
        vna_card.body.addWidget(hint_label(
            '「实际频率方案」随上面的设置实时刷新，显示真正会被测量的频点。'))
        step.add_card(vna_card)
        self.wizard.add_step(step)

        # ---- step 4: point plan -----------------------------------------
        step = WizardStep(3, *WIZARD_STEPS[3][1:])
        plan_card = Card('布点计划')
        plan_grid = field_grid()
        self.plan_combo = QComboBox()
        self.plan_combo.addItem('笛卡尔网格（张量插值）', 'cartesian')
        self.plan_combo.addItem('等 Γ 圆分层（冷启动，免实测）', 'gamma_rings')
        self.plan_combo.addItem('Smith 图均匀布点（需已有库）', 'smith')
        plan_grid.addWidget(field_label('布点方式'), 0, 0)
        plan_grid.addWidget(self.plan_combo, 0, 1)

        self.x_count = QSpinBox()
        self.x_count.setRange(2, 51)
        self.x_count.setValue(9)
        self.z_count = QSpinBox()
        self.z_count.setRange(2, 15)
        self.z_count.setValue(4)
        counts = QHBoxLayout()
        counts.setSpacing(8)
        counts.addWidget(field_label('X 层数'))
        counts.addWidget(self.x_count)
        counts.addWidget(field_label('每轴 Z 层数'))
        counts.addWidget(self.z_count)
        counts.addStretch()
        plan_grid.addWidget(field_label('网格 (笛卡尔)'), 1, 0)
        plan_grid.addLayout(counts, 1, 1)

        self.density_combo = QComboBox()
        for key, label in (('low', '95 点'), ('medium', '181 点'), ('high', '361 点')):
            self.density_combo.addItem(label, key)
        self.density_combo.setCurrentIndex(1)
        self.density_combo.setEnabled(False)
        plan_grid.addWidget(field_label('点密度'), 2, 0)
        plan_grid.addWidget(self.density_combo, 2, 1)

        # F-06/F-07: equal-|Gamma| layered plan with a cold-start prior.
        self.gamma_rmin = self._num(0.0)
        self.gamma_rmax = self._num(0.95)
        self.enh_center = QCheckBox('中心加密')
        self.enh_50 = QCheckBox('50Ω 区加密')
        gamma_row = QHBoxLayout()
        gamma_row.setSpacing(8)
        gamma_row.addWidget(field_label('|Γ| 范围'))
        gamma_row.addWidget(self.gamma_rmin)
        gamma_row.addWidget(QLabel('–'))
        gamma_row.addWidget(self.gamma_rmax)
        gamma_row.addWidget(self.enh_center)
        gamma_row.addWidget(self.enh_50)
        gamma_row.addStretch()
        plan_grid.addWidget(field_label('等 Γ 圆参数'), 3, 0)
        plan_grid.addLayout(gamma_row, 3, 1)

        self.path_edit = QLineEdit(str(paths.default_calibration_path('cal')))
        # The full path is longer than the box; the field scrolls it.
        self.path_edit.setProperty('allowClip', True)
        browse = QPushButton('浏览…')
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        plan_grid.addWidget(field_label('校准文件'), 4, 0)
        plan_grid.addLayout(path_row, 4, 1)

        self.estimate = QLabel('—')
        self.estimate.setFont(numeric_font(10, bold=True))
        # 0.3.9 mock-up: the point-plan step is two columns — the form on the
        # left, a live Smith point-cloud preview on the right.
        plan_split = QHBoxLayout()
        plan_split.setSpacing(14)
        plan_left = QWidget()
        plan_left.setLayout(plan_grid)
        plan_split.addWidget(plan_left, 1)
        plan_right = QVBoxLayout()
        plan_right.setSpacing(8)
        self.smith_preview = SmithPreview(gamma_max=self.gamma_rmax.value(),
                                           gamma_min=self.gamma_rmin.value())
        self.smith_preview.setMinimumSize(220, 220)
        plan_right.addLayout(smith_control_row(self.smith_preview))
        plan_right.addWidget(self.smith_preview)
        plan_right.addWidget(self.estimate)
        plan_right.addWidget(hint_label(
            '内联 Smith 点云：青→品红按 |Γ| 着色；橙圈为「目标 VSWR」圆'
            '（由 |Γ| 上限决定），圆周 12 个点为候选目标点。'))
        plan_right.addStretch()
        plan_split.addLayout(plan_right, 0)
        plan_card.body.addLayout(plan_split)
        step.add_card(plan_card)
        self.wizard.add_step(step)

        # ---- step 5: premise gate and create ---------------------------
        step = WizardStep(4, *WIZARD_STEPS[4][1:])
        # F-03/F-04: the 6-check engine still runs (its verdict is written into
        # the library header and the operation log), but it no longer blocks
        # the build — the operator dropped this gate from the flow while the
        # software is being commissioned.  What stays on screen is the
        # *criteria*: how each check decides, and what counts as a hard block.
        pre_card = Card('前提条件检查')
        pre_btn_row = QHBoxLayout()
        pre_btn_row.setSpacing(8)
        self.pre_check_btn = QPushButton('运行检查（仅记录，不阻断建库）')
        self.pre_cal_btn = QPushButton('录入 VNA 校准')
        self.pre_criteria_btn = QPushButton('判定方法与标准…')
        pre_btn_row.addWidget(self.pre_check_btn)
        pre_btn_row.addWidget(self.pre_cal_btn)
        pre_btn_row.addWidget(self.pre_criteria_btn)
        pre_btn_row.addStretch()
        pre_card.body.addLayout(pre_btn_row)
        self.pre_report = QTextEdit()
        self.pre_report.setReadOnly(True)
        self.pre_report.setFont(numeric_font(9))
        self.pre_report.setMinimumHeight(110)
        self.pre_report.setPlainText('尚未运行。检查结果会记入校准库头与操作日志，'
                                     '但不再阻止建库；点击「判定方法与标准…」可查看'
                                     '每项检查的判定依据。')
        pre_card.body.addWidget(self.pre_report)
        step.add_card(pre_card)

        action_card = Card('建立校准库')
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.start_button = QPushButton('初始化并建立校准库')
        self.start_button.setProperty('accent', True)
        actions.addWidget(self.start_button)
        actions.addStretch()
        action_card.body.addLayout(actions)
        self.hint = QLabel('')
        self.hint.setObjectName('PageHint')
        self.hint.setWordWrap(True)
        action_card.body.addWidget(self.hint)
        step.add_card(action_card)
        self.wizard.add_step(step)

        self.motion_combo.currentIndexChanged.connect(self._sync_sources)
        self.vna_combo.currentIndexChanged.connect(self._sync_sources)
        self.vna_interface.currentIndexChanged.connect(self._sync_vna_interface)
        self.freq_mode.currentIndexChanged.connect(self._sync_freq_mode)
        self.plan_combo.currentIndexChanged.connect(self._sync_plan)
        for widget in (self.x_count, self.z_count):
            widget.valueChanged.connect(self._update_estimate)
        self.density_combo.currentIndexChanged.connect(self._update_estimate)
        self.probes_combo.currentIndexChanged.connect(self._update_estimate)
        # Every frequency input refreshes the "actual frequency plan" preview.
        for widget in (self.vna_start, self.vna_stop, self.fo_ghz):
            widget.valueChanged.connect(self._refresh_freq_preview)
        self.vna_points.valueChanged.connect(self._refresh_freq_preview)
        self.harmonics_edit.textChanged.connect(self._refresh_freq_preview)
        self.arbitrary_edit.textChanged.connect(self._refresh_freq_preview)
        browse.clicked.connect(self.on_browse)
        self.start_button.clicked.connect(self.on_start)
        self.pre_check_btn.clicked.connect(self._run_prerequisites)
        self.pre_cal_btn.clicked.connect(self._enter_vna_cal)
        self.pre_criteria_btn.clicked.connect(self._show_criteria)
        self.gamma_rmax.valueChanged.connect(self._refresh_smith_target)
        self.gamma_rmin.valueChanged.connect(self._refresh_smith_target)
        self.density_combo.currentIndexChanged.connect(self._update_smith_points)
        self.plan_combo.currentIndexChanged.connect(self._update_smith_points)
        self._sync_sources()
        self._sync_freq_mode()
        self._sync_plan()
        self._update_estimate()
        self._update_smith_points()
        self.wizard.go(0)

    @staticmethod
    def _num(value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(-1e6, 1e6)
        spin.setDecimals(4)
        spin.setValue(value)
        return spin

    def _sync_sources(self) -> None:
        real = self.motion_combo.currentData() == 'real'
        self.probes_combo.setEnabled(not real)
        for widget in (self.x_min, self.x_max, self.z_min, self.z_max, self.safe_z):
            widget.setEnabled(real)
        if real:
            probes = self.state.hw.probe_count
            index = self.probes_combo.findData(probes)
            if index >= 0:
                self.probes_combo.setCurrentIndex(index)
        # VNA frequency fields depend on both source and mode.
        self._sync_freq_mode()
        self._update_estimate()

    def _sync_freq_mode(self) -> None:
        """Adapt the VNA-frequency panel to the selected frequency mode.

        Each mode uses a different subset of the frequency fields, so the
        fields that do not apply are hidden outright — a greyed-out input
        still reads as a setting the operator forgot to fill in.
        """
        real = self.vna_combo.currentData() == 'real'
        mode = self.freq_mode.currentData()
        # Which fields each mode actually uses.
        used = {
            'fo_list': ('fo',),
            'freq_sweep': ('start', 'stop', 'points'),
            'harmonic_list': ('fo', 'harmonics'),
            'arbitrary_list': ('arbitrary',),
        }.get(mode, ('fo',))
        for key, (label, widget) in self._freq_rows.items():
            visible = key in used
            label.setVisible(visible)
            widget.setVisible(visible)
            widget.setEnabled(visible and (real or key not in ('start', 'stop', 'points')))
        self.vna_vendor.setEnabled(real)
        self._sync_vna_interface()
        self._refresh_freq_preview()

    def _sync_vna_interface(self) -> None:
        """Only one addressing scheme is meaningful per interface."""
        real = self.vna_combo.currentData() == 'real'
        gpib = self.vna_interface.currentData() == 'gpib'
        self.vna_interface.setEnabled(real)
        for label, widget in ((self.vna_ip_label, self.vna_ip),
                              (self.vna_resource_label, self.vna_resource)):
            label.setEnabled(real)
            widget.setEnabled(real)
        self.vna_ip_label.setVisible(real and not gpib)
        self.vna_ip.setVisible(real and not gpib)
        self.vna_resource_label.setVisible(real and gpib)
        self.vna_resource.setVisible(real and gpib)

    def _refresh_freq_preview(self) -> None:
        """Show the frequency grid the current settings would really measure."""
        body = self._init_body()
        try:
            preview = handlers.studio_frequency_preview(self.state, body)
        except Exception as exc:  # noqa: BLE001 - a preview must never crash the UI
            self.freq_preview.setText(f'频率方案无法生成：{exc}')
            self.freq_preview.setStyleSheet('color:#b03a2e;')
            return
        if not preview.get('ok'):
            self.freq_preview.setText('频率方案无效：' + str(preview.get('error', '')))
            self.freq_preview.setStyleSheet('color:#b03a2e;')
            return
        count = int(preview.get('count', 0))
        start = float(preview.get('start_hz') or 0.0)
        stop = float(preview.get('stop_hz') or 0.0)
        text = f'实际频率方案：{count} 个频点 · {start / 1e9:.4f} – {stop / 1e9:.4f} GHz'
        step = preview.get('step_hz')
        if step:
            text += f' · 步长 {step / 1e6:.3f} MHz'
        if not preview.get('uniform'):
            text += ' · 非等间隔（列表模式，仪器侧需逐点设置）'
        for warning in preview.get('warnings', []):
            text += f'\n注意：{warning}'
        self.freq_preview.setText(text)
        self.freq_preview.setStyleSheet('')

    def _sync_plan(self) -> None:
        plan = self.plan_combo.currentData()
        self.x_count.setEnabled(plan == 'cartesian')
        self.z_count.setEnabled(plan == 'cartesian')
        self.density_combo.setEnabled(plan in ('smith', 'gamma_rings'))
        self.gamma_rmin.setEnabled(plan == 'gamma_rings')
        self.gamma_rmax.setEnabled(plan == 'gamma_rings')
        self.enh_center.setEnabled(plan == 'gamma_rings')
        self.enh_50.setEnabled(plan == 'gamma_rings')
        self._update_estimate()

    def _update_estimate(self) -> None:
        plan = self.plan_combo.currentData()
        density = self.density_combo.currentData()
        if plan == 'smith':
            count = {'low': 95, 'medium': 181, 'high': 361}[density]
            self.estimate.setText(f'{count} 点（Smith 均匀布点）')
            return
        if plan == 'gamma_rings':
            count = {'low': 95, 'medium': 181, 'high': 361}[density]
            self.estimate.setText(f'{count} 点（等 Γ 圆分层，冷启动）')
            return
        probes = (self.state.hw.probe_count
                  if self.motion_combo.currentData() == 'real'
                  else int(self.probes_combo.currentData()))
        count = self.x_count.value() * self.z_count.value() ** probes
        self.estimate.setText(f'{count:,} 点（X {self.x_count.value()} × '
                              f'Z {self.z_count.value()}^{probes}）')

    def _refresh_smith_target(self) -> None:
        """Keep the wizard's Smith preview ring in sync with |Γ| max/min."""
        if hasattr(self, 'smith_preview'):
            self.smith_preview.set_target(self.gamma_rmax.value(),
                                          self.gamma_rmin.value())

    def _update_smith_points(self) -> None:
        """Push the planned point count into the preview's corner badge."""
        if not hasattr(self, 'smith_preview'):
            return
        plan = self.plan_combo.currentData()
        if plan in ('gamma_rings', 'smith'):
            density = self.density_combo.currentData()
            self.smith_preview.set_points(
                {'low': 95, 'medium': 181, 'high': 361}.get(density, 0))
        else:
            self.smith_preview.set_points(0)
        self._refresh_smith_target()

    def on_browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, '选择校准库文件名', self.path_edit.text(), 'HDF5 (*.h5 *.hdf5)')
        if path:
            self.path_edit.setText(path)

    # -- action ----------------------------------------------------------
    def _check_vna_sweep(self, body: dict) -> bool:
        """Refuse an invalid sweep before any SCPI reaches the instrument.

        The old failure surfaced as ``database.create`` complaining about a
        non-increasing frequency grid -- long after the invalid sweep had been
        written to the real analyzer.  Give the operator the actual fix here.
        """
        if body.get('vna_source') != 'real':
            return True
        start = float(body['vna_start_hz']) / 1e9
        stop = float(body['vna_stop_hz']) / 1e9
        points = int(body['vna_points'])
        if start <= 0 or stop <= 0:
            QMessageBox.warning(self, '矢网频率设置无效',
                                '起始 / 终止 GHz 必须大于 0。')
            return False
        if points > 1 and start >= stop:
            QMessageBox.warning(
                self, '矢网频率设置无效',
                f'当前起始 GHz = {start:g}、终止 GHz = {stop:g}，'
                '多频点扫描必须满足 起始 < 终止。\n\n'
                f'若只在 {start:g} GHz 单频点校准，请把「频点」设为 1，'
                '此时以起始频率为准，终止频率自动对齐。')
            return False
        return True

    def _check_frequency_plan(self, body: dict) -> bool:
        """Reject an invalid frequency plan *before* any hardware is touched.

        The preview endpoint is the single source of truth for what will be
        measured, so the check and the on-screen preview can never disagree.
        """
        try:
            preview = handlers.studio_frequency_preview(self.state, body)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, '频率方案无效', f'{type(exc).__name__}: {exc}')
            return False
        if not preview.get('ok'):
            QMessageBox.warning(
                self, '频率方案无效',
                f'当前设置无法生成频率网格：\n{preview.get("error", "")}\n\n'
                '请回到第 3 步「频率方案」修正后重试。')
            return False
        return True

    def on_start(self) -> None:
        body = self._init_body()
        if not self._check_vna_sweep(body):
            return
        if not self._check_frequency_plan(body):
            return
        # F-03: the 6 checks still run before any hardware init / DB creation,
        # but they no longer block the build.  The verdict is written into the
        # library header and the operation log, so a library built while a
        # hard check was failing is still traceable later.
        try:
            report = handlers.studio_prerequisites(self.state, body)
        except Exception as exc:  # noqa: BLE001 - show the engine error to the user
            self.warn('前提条件检查失败', f'{type(exc).__name__}: {exc}')
            return
        if not report.get('passed'):
            self.pre_report.setPlainText(report.get('text', ''))
            self.report('前提检查存在未通过项（已记录，不阻断建库）')
        # F-23 (danger double-confirm): real motion drives the controller.
        if body.get('motion_source') == 'real' and not self._confirm_danger(
                '真实运动建库确认',
                '即将以【真实运动】方式建立校准库并驱动 Zmotion 控制器。\n'
                '请确认机械行程与急停可用。'):
            self.report('已取消真实运动建库')
            return
        plan_body = self._plan_body()
        self.start_button.setEnabled(False)
        self.start_button.setText('初始化中…')

        def work():
            # The bridge only replaces the real-hardware branch: its scale gate
            # asked for feedback_scale == 1.0, which this controller (MPOS in
            # 10 µm counts) can never satisfy after a correct self-check.
            studio_bridge.init_hardware(self.state, body)
            result = handlers.studio_setup(self.state, plan_body)
            return result

        if body.get('motion_source') == 'real':
            try:
                work = self.state.hw.reserve_task(work, motion=True, expected=self.state.hw.context())
            except Exception as exc:
                self.start_button.setEnabled(True)
                self.start_button.setText('建立校准库')
                self.fail(exc, '建立校准库失败')
                return
        self.run(work, self._created, title='建立校准库失败', description='studio setup')

    def _init_body(self) -> dict:
        probes = (self.state.hw.probe_count
                  if self.motion_combo.currentData() == 'real'
                  else int(self.probes_combo.currentData()))
        mode = self.freq_mode.currentData()
        fo = self.fo_ghz.value()
        is_sweep = mode == 'freq_sweep'
        body = {
            'motion_source': self.motion_combo.currentData(),
            'probes': probes,
            'x_min': self.x_min.value(), 'x_max': self.x_max.value(),
            'z_min': self.z_min.value(), 'z_max': self.z_max.value(),
            'safe_z': self.safe_z.value(),
            'vna_source': self.vna_combo.currentData(),
            'vna_vendor': self.vna_vendor.currentData(),
            'vna_interface': self.vna_interface.currentData(),
            'vna_resource': self.vna_resource.text().strip(),
            'vna_ip': self.vna_ip.text().strip(),
            'frequency_mode': mode,
            'fo_hz': fo * 1e9,
            'vna_start_hz': (self.vna_start.value() if is_sweep else fo) * 1e9,
            'vna_stop_hz': (self.vna_stop.value() if is_sweep else fo) * 1e9,
            'vna_points': self.vna_points.value() if is_sweep else 1,
        }
        if mode == 'harmonic_list':
            # A malformed entry must not fall back to a default: the operator
            # would then measure a frequency they never asked for.  The raw
            # text is handed to the backend, which rejects it with a clear
            # message (and the preview shows the error).
            raw = self.harmonics_edit.text().replace('，', ',')
            try:
                body['harmonics'] = [int(x) for x in raw.split(',') if x.strip()]
            except ValueError:
                body['harmonics'] = raw
        if mode == 'arbitrary_list':
            raw = self.arbitrary_edit.text().replace('，', ',')
            try:
                body['frequency_list_ghz'] = [float(x) for x in raw.split(',') if x.strip()]
            except ValueError:
                body['frequency_list_ghz'] = raw
        return body

    def _plan_body(self) -> dict:
        target = Path(self.path_edit.text().strip())
        if not target.parent.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
        plan = self.plan_combo.currentData()
        body = {
            'plan': plan,
            'x_count': self.x_count.value(),
            'z_count': self.z_count.value(),
            'density': self.density_combo.currentData(),
            'path': str(target),
        }
        if plan == 'gamma_rings':
            body['r_min'] = self.gamma_rmin.value()
            body['r_max'] = self.gamma_rmax.value()
            body['enhanced_center'] = self.enh_center.isChecked()
            body['enhanced_50ohm'] = self.enh_50.isChecked()
        return body

    def _run_prerequisites(self) -> None:
        body = self._init_body()
        try:
            report = handlers.studio_prerequisites(self.state, body)
        except Exception as exc:  # noqa: BLE001
            self.pre_report.setPlainText(f'检查失败：{type(exc).__name__}: {exc}')
            return
        self.pre_report.setPlainText(report.get('text', ''))
        self.report('前提检查: ' + ('通过' if report.get('passed') else '未通过（已记录，不阻断建库）'))

    def _show_criteria(self) -> None:
        """Show how each prerequisite check decides, and what counts as a block."""
        try:
            data = handlers.studio_prerequisite_criteria(self.state, {})
        except Exception as exc:  # noqa: BLE001
            self.warn('无法读取判定标准', f'{type(exc).__name__}: {exc}')
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle('前提条件检查 · 判定方法与标准')
        box.setText('每项检查怎么判、什么算硬阻止、什么只是提醒。\n'
                    '当前版本：检查结果会记入库头与操作日志，但不再阻止建库。')
        box.setDetailedText(data.get('text', ''))
        box.exec()

    def _enter_vna_cal(self) -> None:
        method, ok = QInputDialog.getText(self, '录入 VNA 校准',
                                          '校准方法 (如 TRL / SOLT):')
        if not ok or not method.strip():
            return
        kit, ok = QInputDialog.getText(self, '录入 VNA 校准',
                                       '校准件型号 (可选，可留空):')
        if not ok:
            return
        try:
            handlers.vna_calibration_set(
                self.state,
                {'method': method.strip(), 'cal_kit': kit.strip(), 'verified': True})
        except Exception as exc:  # noqa: BLE001
            self.warn('录入 VNA 校准失败', f'{type(exc).__name__}: {exc}')
            return
        self.report(f'已录入 VNA 校准: {method.strip()}')
        self._run_prerequisites()

    @staticmethod
    def _confirm_danger(title: str, message: str) -> bool:
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(message)
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _created(self, result: dict) -> None:
        self.start_button.setEnabled(True)
        self.start_button.setText('初始化并建立校准库')
        self.hint.setStyleSheet('color:#1f7a4d;')
        if result.get('resumed'):
            self.hint.setText(
                f"已加载校准库（续扫）：{result['path']} · 已有 "
                f"{result['valid']} / {result['positions']} 点")
            self.report(f"校准库已加载（续扫）: {result['valid']}/{result['positions']} 点")
        else:
            self.hint.setText(f"已建立校准库：{result['path']} · {result['positions']} 点")
            self.report(f"校准库已建立: {result['positions']} 点")
        self.created.emit(str(result['path']))


class StudioRunPage(_StudioPage):
    """Run, pause, resume and cancel the acquisition."""

    def __init__(self, app_state, parent=None):
        super().__init__(app_state, parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)
        root.addLayout(_page_title(
            '采集运行',
            '每个点完成后才写入有效标记，所以暂停、取消、断电都不会污染已完成的数据；'
            '重新打开同一个库即可续扫。'))

        status = QGroupBox('任务')
        form = QFormLayout(status)
        self.state_pill = StatusPill('idle', 'idle')
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.counter = QLabel('0 / 0')
        self.counter.setFont(numeric_font(10, bold=True))
        form.addRow('状态', self.state_pill)
        form.addRow('进度', self.progress)
        form.addRow('点数', self.counter)
        root.addWidget(status)

        # F-11: the acquisition grid as a point cloud — the operator sees which
        # corner is still empty instead of only a percentage.
        cloud_box = QGroupBox('采集点云 (F-11)')
        cloud_layout = QVBoxLayout(cloud_box)
        self.cloud = ProgressCloudWidget()
        cloud_layout.addWidget(self.cloud)
        self.cloud_caption = QLabel('建库后显示')
        self.cloud_caption.setObjectName('PageHint')
        cloud_layout.addWidget(self.cloud_caption)

        # Live Smith chart: every committed Γ is plotted as the sweep runs, so
        # coverage on the impedance plane is visible point by point.  The same
        # zoom/pan widget as the rest of the app (wheel, drag, double click),
        # plus explicit buttons for touch/trackpad-free workshop PCs.
        smith_box = QGroupBox('实时 Smith 图（已采 Γ）')
        smith_layout = QVBoxLayout(smith_box)
        tools = QHBoxLayout()
        tools.addWidget(QLabel('频率'))
        self.smith_frequency = QComboBox()
        self.smith_frequency.setMinimumWidth(118)
        tools.addWidget(self.smith_frequency)
        tools.addSpacing(8)
        self.live_smith = SmithChart()
        tools.addLayout(smith_control_row(self.live_smith))
        smith_layout.addLayout(tools)
        self.live_smith.setMinimumSize(240, 220)
        smith_layout.addWidget(self.live_smith, 1)
        self.smith_caption = QLabel('尚未采集数据')
        self.smith_caption.setObjectName('PageHint')
        self.smith_caption.setWordWrap(True)
        smith_layout.addWidget(self.smith_caption)

        visuals = QHBoxLayout()
        visuals.setSpacing(12)
        visuals.addWidget(cloud_box, 1)
        visuals.addWidget(smith_box, 1)
        root.addLayout(visuals, 1)

        buttons = QHBoxLayout()
        self.run_button = QPushButton('开始 / 继续')
        self.run_button.setProperty('accent', True)
        self.pause_button = QPushButton('暂停')
        self.cancel_button = QPushButton('取消')
        for widget in (self.run_button, self.pause_button, self.cancel_button):
            buttons.addWidget(widget)
        buttons.addStretch()
        root.addLayout(buttons)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(numeric_font(9))
        self.log.setPlaceholderText('采集过程中的提示会出现在这里')
        self.log.setMaximumHeight(110)
        root.addWidget(self.log)

        self._smith_freqs: list[float] = []
        self._smith_signature = None
        self.run_button.clicked.connect(self.on_run)
        self.pause_button.clicked.connect(self.on_pause)
        self.cancel_button.clicked.connect(self.on_cancel)
        self.smith_frequency.currentIndexChanged.connect(
            self._on_smith_frequency_changed)
        self.timer = QTimer(self)
        self.timer.setInterval(700)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def append(self, text: str) -> None:
        self.log.append(text)

    #: Human-readable labels for the bookkeeping states the poll can report.
    _STATE_LABELS = {
        'idle': '待机', 'ready': '就绪', 'running': '采集中',
        'paused': '已暂停', 'cancelled': '已取消', 'failed': '失败',
        'emergency': '急停', 'loaded': '已加载', 'completed': '已完成',
        'completed_with_errors': '部分完成（有失败点）',
        'validated': '已完成', 'validating': '验证中',
        'validating_interpolation': '插值验证中',
    }

    def refresh(self) -> None:
        try:
            task = handlers.studio_task(self.state, {})
        except Exception:  # noqa: BLE001
            return
        state = str(task.get('state') or 'idle')
        # Authoritative counts from the library's committed flags.  The legacy
        # ``progress`` value is the 0-based index of the last written point, so
        # using it as a count made a finished 1152-point sweep read "1151/1152"
        # at 99% while the state pill already said 已完成.
        done = int(task.get('completed') or 0)
        total = int(task.get('total_points') or 0)
        all_done = total > 0 and done >= total
        # A finished run is auto-loaded into the studio, which rewrites the
        # state to 'loaded'.  Only a sweep with *every* point committed counts
        # as complete; a partially failed one keeps its warning state.
        finished = (state in ('loaded', 'completed', 'validated') and all_done)
        if state == 'completed_with_errors':
            label = '部分完成（有失败点）'
        else:
            label = '已完成' if finished else self._STATE_LABELS.get(state, state)
        pill = ('error' if state in ('failed', 'emergency') else
                'ok' if finished else
                'warn' if (state in ('running', 'paused', 'validating',
                                     'validating_interpolation',
                                     'completed_with_errors', 'cancelled')
                           or (total > 0 and done > 0 and not all_done))
                else 'idle')
        self.state_pill.set_state(pill, label)
        self.counter.setText(f'{done} / {total}')
        self.progress.setValue(min(100, int(100 * done / total)) if total else 0)
        self._refresh_cloud()
        self._refresh_live_smith(done, total)

    def _on_smith_frequency_changed(self) -> None:
        self._smith_signature = None
        self._refresh_live_smith()

    def _sync_smith_frequencies(self, frequencies: list[float]) -> None:
        """Populate the frequency picker once the library is known."""
        values = [float(f) for f in frequencies]
        if values == self._smith_freqs:
            return
        previous = self.smith_frequency.currentData()
        self._smith_freqs = values
        self.smith_frequency.blockSignals(True)
        self.smith_frequency.clear()
        for hz in values:
            self.smith_frequency.addItem(f'{hz / 1e9:.4f} GHz', hz)
        restore = self.smith_frequency.findData(previous) if previous else -1
        if restore >= 0:
            self.smith_frequency.setCurrentIndex(restore)
        self.smith_frequency.blockSignals(False)

    def _refresh_live_smith(self, done=None, total=None) -> None:
        """Pull the committed Γ cloud and repaint when it actually changes.

        The HDF5 ``s`` cube is chunked per point, so reading a frequency slice
        still touches every committed row.  Guard on the cheap committed count
        (from ``studio_task``) first: during a long sweep nothing changes
        between two points, so the 700 ms poll costs nothing.
        """
        frequency = self.smith_frequency.currentData()
        # When the poll already knows the committed count, skip the HDF5 read
        # entirely unless a point was added or the frequency changed.
        if self._smith_signature is not None and done is not None:
            if (done, total, frequency) == self._smith_signature:
                return
        try:
            snap = handlers.studio_live_smith(
                self.state, {'frequency': frequency})
        except Exception:  # noqa: BLE001 - no library yet is the normal case
            return
        self._sync_smith_frequencies(snap.get('frequencies') or [])
        # Sign on the frequency actually used by the snapshot (the picker may
        # have just been populated, in which case ``frequency`` was None).
        signature = (snap.get('completed'), snap.get('total_points'),
                     snap.get('frequency_hz'))
        if signature == self._smith_signature:
            return
        self._smith_signature = signature
        points = snap.get('points') or []
        self.live_smith.points = np.asarray(
            [complex(p['re'], p['im']) for p in points], dtype=complex)
        latest = snap.get('latest')
        self.live_smith.selected = (complex(latest['re'], latest['im'])
                                    if latest else None)
        self.live_smith.update()
        done = int(snap.get('completed') or 0)
        total = int(snap.get('total_points') or 0)
        hz = float(snap.get('frequency_hz') or 0.0)
        self.smith_caption.setText(
            f'{done} / {total} 点 · {hz / 1e9:.4f} GHz · '
            '青色=已采 Γ，粉色=最新点；滚轮缩放 / 中键拖拽 / 双击复位')

    def _refresh_cloud(self) -> None:
        """F-11: pull the point cloud from the live library."""
        try:
            cloud = handlers.studio_progress_cloud(self.state, {})
        except Exception:  # noqa: BLE001 - no library yet is the normal case
            return
        self.cloud.set_cloud(cloud)
        failed = cloud.get('failed') or []
        parts = [f"总层数 {cloud.get('layer_count', 0)}",
                 f"已完成层 {cloud.get('complete_layers', 0)}"]
        if failed:
            parts.append(f"失败点 {len(failed)}")
        self.cloud_caption.setText(' · '.join(parts))

    def on_run(self) -> None:
        try:
            handlers.studio_run(self.state, {})
        except Exception as exc:  # noqa: BLE001
            self.fail(exc, '启动采集失败')
            return
        self.append('采集已启动（后台线程）')
        self.report('采集已启动')

    def on_pause(self) -> None:
        try:
            handlers.studio_pause(self.state, {})
        except Exception as exc:  # noqa: BLE001
            self.fail(exc, '暂停失败')
            return
        self.append('已请求暂停，当前点完成后生效')

    def on_cancel(self) -> None:
        try:
            handlers.studio_cancel(self.state, {})
        except Exception as exc:  # noqa: BLE001
            self.fail(exc, '取消失败')
            return
        self.append('已请求取消，已完成点保留')


class StudioDatabasePage(_StudioPage):
    """Inspect an existing calibration database."""

    loaded = Signal(str)

    def __init__(self, app_state, parent=None):
        super().__init__(app_state, parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)
        root.addLayout(_page_title(
            '校准库',
            'HDF5 主库与同名 SQLite 边车必须一起保存：前者存数据，后者存任务状态与失败原因。'))

        buttons = QHBoxLayout()
        self.open_button = QPushButton('打开校准库…')
        self.open_button.setProperty('accent', True)
        self.reveal_button = QPushButton('打开所在文件夹')
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.reveal_button)
        buttons.addStretch()
        root.addLayout(buttons)

        self.summary = QLabel('尚未加载校准库')
        self.summary.setWordWrap(True)
        self.summary.setFont(numeric_font(10, bold=True))
        root.addWidget(self.summary)

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setFont(numeric_font(9))
        root.addWidget(self.details, 1)

        self.open_button.clicked.connect(self.on_open)
        self.reveal_button.clicked.connect(self.on_reveal)

    def on_open(self) -> Path | None:
        start = str(paths.calibration_dir())
        path, _ = QFileDialog.getOpenFileName(
            self, '打开校准库', start, 'HDF5 (*.h5 *.hdf5);;所有文件 (*)')
        if not path:
            return None
        self.load(path)
        return Path(path)

    def load(self, path: str) -> None:
        try:
            handlers.studio_open_db(self.state, {'path': path})
            info = handlers.studio_database(self.state, {})
        except Exception as exc:  # noqa: BLE001
            self.fail(exc, '打开校准库失败')
            return
        self._render(info)
        self.report(f"已加载校准库: {info['valid']}/{info['total']} 点")
        # The shell rebroadcasts this to *every* dependent view.
        self.loaded.emit(str(info['path']))

    def on_library_changed(self, path: str) -> None:
        """Show the library made active elsewhere (e.g. the Calibration Browser).

        Deliberately does **not** call :meth:`load`: that would reopen the file
        and emit ``loaded`` again, causing an endless refresh loop.  Only the
        display is updated.
        """
        if not path:
            self.summary.setText('尚未加载校准库')
            self.details.setPlainText('')
            return
        try:
            info = handlers.studio_database(self.state, {})
        except Exception:  # noqa: BLE001 - another face is still loading
            return
        self._render(info)

    def _render(self, info: dict) -> None:
        import json
        self.summary.setText(
            f"{info['path']}\n有效点 {info['valid']} / {info['total']} · "
            f"频率 {info['frequencies'][0] / 1e9:.3f}–{info['frequencies'][1] / 1e9:.3f} GHz")
        self.details.setPlainText(
            json.dumps(info['metadata'], indent=2, ensure_ascii=False))

    def on_reveal(self) -> None:
        import os
        target = paths.calibration_dir()
        target.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(target))  # noqa: S606 - Windows shell open
        except Exception as exc:  # noqa: BLE001
            self.fail(exc, '打开文件夹失败')

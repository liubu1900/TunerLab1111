"""Axis motion page: working timeouts, travel guards, fault recovery, readable UI.

Why this subclass exists
------------------------
The stock :class:`tuner.ui_hardware.AxisMotionPage` reports two failures that were
reproduced on the real machine (ZMC 2.9600, 4-axis tuner):

1. ``_home()`` passes ``TunerConfig.motion_timeout`` — 5 s by default — to
   ``home_axis_safe()``.  A real ``DATUM`` cycle takes longer, so the page popped
   *"Z1 homing did not finish within the timeout"* at exactly 5.0 s **while the
   axis was still travelling**.  On the machine it kept going from 6.8 mm to
   18.0 mm and hit the positive hard limit (``AXISSTATUS`` bit 4 + bit 22).
2. After that, every positioning request popped *"home the axes before motion;
   not homed: Z1"*, because the timed-out axis had been dropped from the homed
   set while it was still moving.

It also latched a motion fault that nothing in the UI could clear (「复位急停」 only
clears the emergency latch), and its nine-column grid of ``QLabel`` cells squeezed
the status text until it was unreadable.

This page keeps the original controls and behaviour, and fixes:

* the homing cycle — supervised, with the documented switch-search mode, and
  ``DPOS`` zeroed at the end;
* the travel guard — X 0–400 mm, Z 0–17.5 mm;
* fault recovery — an explicit 「复位运动故障」 button;
* the layout — a real table with grid lines, and scrollable per-axis panels.
"""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDoubleSpinBox, QFrame, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QMessageBox, QProgressBar, QPushButton, QScrollArea, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from tuner.axis_params import axis_index
from tuner.motion import MotionError
from tuner.operation_log import operation_log
from tuner.ui_hardware import AxisMotionPage
from tuner.ui_theme import numeric_font

from tuner.zmotion_errors import describe_axis_status, has_fault

from .bg import run_in_background
from .fonts import px_font
from .ui import KpiRow, KpiTile, add_card_shadow
from .widgets import AxisBar

__all__ = ['SafeAxisMotionPage', 'DEFAULT_TRAVEL']

#: Declared mechanical travel per axis, in user units (mm).
#:
#: Z is 17.5 mm on this tuner, X is 400 mm.  Used only when the stored axis
#: parameters still carry the factory soft limits.
DEFAULT_TRAVEL: dict[str, tuple[float, float]] = {
    'X': (0.0, 400.0),
    'Z1': (0.0, 17.5),
    'Z2': (0.0, 17.5),
    'Z3': (0.0, 17.5),
}

#: Anything wider than this is treated as "soft limit never configured".
_FACTORY_LIMIT_SPAN = 1e7

#: Column headers of the live status table.
_TABLE_COLUMNS = ('轴', '指令 DPOS (mm)', '反馈 MPOS (mm)', '跟随误差 (mm)', '空闲',
                  '回零 / 原点', 'AXISSTATUS', '状态 / 停止原因')


class SafeAxisMotionPage(AxisMotionPage):
    """Manual axis control that never lies about a move in progress."""

    #: Posted when a worker thread wants the status widgets refreshed.
    #:
    #: Moves run on a worker thread; Qt widgets may only be touched from the GUI
    #: thread.  Writing into ``QTableWidget`` / ``QLabel`` / ``QProgressBar`` from
    #: the worker killed the process with a segfault and no traceback, which is
    #: exactly what "clicking quickly closes the window" looked like.
    _refresh_requested = Signal()

    #: Carries a finished scale self-check back to the GUI thread.
    _scale_probe_done = Signal(object)

    #: How long one homing cycle may take before it is considered abnormal.
    HOME_TIMEOUT = 90.0

    #: Travel used by the scale self-check.
    #:
    #: One encoder count is 10 µm, so the stock 5 µm probe moved half a count and
    #: reported a meaningless ratio.  0.5 mm is 50 counts: quantisation is 2 %.
    PROBE_DISTANCE_MM = 0.5

    def __init__(self, session, parent=None):
        super().__init__(session, parent)
        self.layout().setContentsMargins(22, 0, 22, 18)
        self.layout().setSpacing(14)
        self._gui_thread_id = threading.get_ident()
        self._refresh_requested.connect(
            self._refresh_status, Qt.ConnectionType.QueuedConnection)
        self._scale_probe_done.connect(
            self._apply_scale_probe, Qt.ConnectionType.QueuedConnection)
        self._build_extra_controls()
        self._build_notice_card()
        self._replace_status_grid_with_table()
        self._wrap_panels_in_scroll()
        session.settings_changed.connect(self._update_travel_label)
        self._update_travel_label()
        self._rebuild_page_layout()

    # -- layout ----------------------------------------------------------
    def _rebuild_page_layout(self) -> None:
        """Put the four blocks in a known order and drop everything else.

        The base page builds into the same layout and leaves spacers, hidden
        buttons and a status grid behind; taking them out of the layout without
        reparenting them left those widgets painted on top of the table (the
        smeared text under the origin-input line).  Everything not re-added is
        detached and deleted here.
        """
        layout = self.layout()
        if layout is None:
            return
        keep = [getattr(self, '_kpi_row', None), getattr(self, 'status_card', None),
                getattr(self, 'panels_scroll', None), getattr(self, '_ops_card', None),
                getattr(self, '_notice_card', None)]
        keep = [w for w in keep if w is not None]
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None and widget not in keep:
                widget.setParent(None)
                widget.deleteLater()
        for widget in keep:
            layout.addWidget(widget)
        layout.addStretch(1)

    # -- KPI strip -------------------------------------------------------
    def _add_kpi_row(self) -> None:
        self.kpi_homed = KpiTile('已回零轴数', '0 / 0', '原点输入 IN16–IN19')
        self.kpi_busy = KpiTile('运动状态', '空闲', '无进行中的运动')
        self.kpi_travel = KpiTile('申报行程', '—', 'X / Z 分开限制')
        self.kpi_mode = KpiTile('控制模式', '—', '来自通讯设置')
        self._kpi_row = KpiRow([self.kpi_homed, self.kpi_busy,
                                self.kpi_travel, self.kpi_mode])
        layout = self.layout()
        if layout is not None:
            layout.insertWidget(0, self._kpi_row)

    def _sync_kpis(self, snapshot=None) -> None:
        if not hasattr(self, 'kpi_homed'):
            return
        axes = list(self.session.axes)
        homed = 0
        if snapshot:
            homed = sum(1 for e in snapshot if e.get('home_status') == 1)
        self.kpi_homed.set_value(f'{homed} / {len(axes)}',
                                 '全部轴已回零' if homed == len(axes) else '仍有轴未回零',
                                 'ok' if homed == len(axes) else 'warn')
        busy = bool(getattr(self, '_motion_busy', False))
        self.kpi_busy.set_value('运动中' if busy else '空闲',
                                '完成前不接受新指令' if busy else '可接受指令',
                                'warn' if busy else '')
        boxes = [self._travel_box(name) for name in axes]
        if boxes:
            span = ' · '.join(f'{name} {low:g}–{high:g}' for name, (low, high)
                              in zip(axes, boxes))
        else:
            span = '—'
        self.kpi_travel.set_value(
            f'{boxes[1][1]:g} mm' if len(boxes) > 1 else '—', span)
        mode = self.session.settings.mode
        self.kpi_mode.set_value('真实控制器' if mode == 'real' else '虚拟控制器',
                                self.session.settings.connection.ip)

    # -- extra controls --------------------------------------------------
    def _build_extra_controls(self) -> None:
        """One row of actions plus the declared travel, as in the mock-up.

        The stock page stacks the stop buttons and lets the emergency stop stretch
        across the card.  The buttons are moved into a single row instead of being
        recreated -- and the old layout is *emptied*, not detached: detaching it
        hands the widgets to a temporary parent that then deletes them.
        """
        box = self.stop_button.parentWidget()
        self._ops_card = box
        if box is None:
            return
        # Scheme 5 draws this as a plain card, not a captioned group box: the
        # reserved title strip made the bar 18 px taller than the mock-up.
        if isinstance(box, QGroupBox):
            box.setTitle('')
        box.setObjectName('OpsBar')
        box.style().unpolish(box)
        box.style().polish(box)

        old = box.layout()
        if old is not None:
            while old.count():
                old.takeAt(0)
            old.setContentsMargins(14, 12, 14, 12)

        row_widget = QWidget()
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        # The mock-up's danger buttons: pale red face, soft red border.
        danger = ('background:#fff5f4; border:1px solid #f3cdc8; color:#c0392b;'
                  'border-radius:8px; padding:6px 14px; font-weight:500;')
        self.stop_button.setStyleSheet(danger)
        self.stop_button.setMinimumWidth(76)

        # Emergency stop leads the row and is deliberately wider than the rest,
        # with the English legend the machine's safety plate uses.
        self.estop_button.setText('急停  EMERGENCY STOP')
        self.estop_button.setStyleSheet(danger)
        self.estop_button.setMinimumWidth(196)
        self.estop_button.setMaximumWidth(196)
        self.estop_button.setToolTip('立即停止所有轴并锁存急停状态（软件急停）')

        self.reset_button.setMinimumWidth(86)
        self.errors_button.hide()

        self.reset_fault_button = QPushButton('复位运动故障')
        self.reset_fault_button.setMinimumWidth(118)
        self.reset_fault_button.setToolTip(
            '清除因运动超时、限位或编码器反馈不符而锁存的故障。\n'
            '故障清除后回零状态失效，必须重新回零才能运动。')
        self.reset_fault_button.clicked.connect(self.on_reset_fault)

        row.addWidget(self.estop_button)
        row.addWidget(self.stop_button)
        row.addWidget(self.reset_button)
        row.addWidget(self.reset_fault_button)

        self.travel_label = QLabel('')
        self.travel_label.setObjectName('PageHint')
        self.travel_label.setFont(px_font(11.5, 'light'))
        self.travel_label.setStyleSheet('color:#7d8ea6;')
        row.addStretch()
        row.addWidget(self.travel_label)

        if old is not None:
            old.addWidget(row_widget)
        else:
            QVBoxLayout(box).addWidget(row_widget)

    def _build_notice_card(self) -> None:
        """Re-home the base page's scale self-check output.

        ``_rebuild_page_layout`` deletes every widget it does not put back.  The
        scale label and the 「采纳并写入 UNITS」 button are created by the base
        page, so they were deleted while ``_apply_scale_probe`` still held a
        reference -- pressing 标度自检 raised
        ``Internal C++ object (QLabel) already deleted`` after the axis had
        already moved.  They live in their own card now, hidden until there is a
        result to show (and it sits below the action bar, outside the mock-up's
        four fixed blocks, so the resting page is unchanged).
        """
        card = QWidget()
        card.setObjectName('NoticeBar')
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card.setVisible(False)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        self.scale_label.setParent(card)
        self.scale_label.setWordWrap(True)
        self.scale_label.setFont(px_font(12, 'medium'))

        self.apply_scale_button.setParent(card)
        self.apply_scale_button.setMaximumWidth(380)
        self.apply_scale_button.setStyleSheet(
            'background:#fff5f4; border:1px solid #f3cdc8; color:#c0392b;'
            'border-radius:8px; padding:6px 14px; font-weight:500;')

        # Kept alive for the base page's error reader; it has no button on this
        # page, but a dangling reference would crash the same way.
        self.error_label.setParent(card)
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)

        layout.addWidget(self.scale_label)
        layout.addWidget(self.apply_scale_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.error_label)
        self._notice_card = card

    def _update_travel_label(self) -> None:
        if not hasattr(self, 'travel_label'):
            return
        parts = []
        for name in self.session.axes:
            low, high = self._travel_box(name)
            parts.append(f'{name} {low:g}–{high:g}')
        self.travel_label.setText('申报行程 (mm)：' + ' · '.join(parts))

    # -- layout: status table --------------------------------------------
    def _replace_status_grid_with_table(self) -> None:
        """Replace the nine-label grid with a table that keeps its columns."""
        grid = self.status_grid
        box = grid.parentWidget()
        while grid.count():
            item = grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.position_labels.clear()
        self.status_flags.clear()

        if box is not None and box.layout() is not None:
            # Detach the old grid so the table and the label can stack vertically
            # without sharing grid rows (which made the label overlap the table).
            QWidget().setLayout(box.layout())
        container = QVBoxLayout(box) if box is not None else QVBoxLayout()
        container.setContentsMargins(8, 6, 8, 6)
        container.setSpacing(4)

        table = QTableWidget(0, len(_TABLE_COLUMNS))
        table.setHorizontalHeaderLabels(_TABLE_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setAlternatingRowColors(True)
        table.setShowGrid(True)
        table.setWordWrap(False)
        table.setColumnWidth(0, 52)
        header = table.horizontalHeader()
        for column in range(0, 7):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(True)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        table.setStyleSheet(
            'QTableWidget {gridline-color:#c9d3de;}'
            'QTableWidget::item {padding:3px 6px;}')
        self.status_table = table
        self.input_label = QLabel('原点输入：—')
        self.input_label.setStyleSheet('color:#5b6b7c;')

        # The base page filled this card with a grid of labels; takeAt() detached
        # them from the layout but they stayed children and kept painting on top
        # of the table.  Anything that is not ours is removed here.
        if box is not None:
            for child in box.findChildren(QWidget):
                if child is table or child is self.input_label:
                    continue
                if child.parentWidget() is box:
                    child.setParent(None)
                    child.deleteLater()
        self.input_label.setFont(numeric_font(9))
        container.addWidget(table)
        container.addWidget(self.input_label, 0, Qt.AlignmentFlag.AlignLeft)
        self._last_input_poll = 0.0
        self._rebuild_table()
        if box is not None:
            # Let the card grow with its table; a fixed minimum clipped the last
            # row once the theme added cell padding.
            box.setMinimumHeight(0)
            add_card_shadow(box)
            self.status_card = box

    def _rebuild_table(self) -> None:
        table = self.status_table
        table.setRowCount(len(self.session.axes))
        for row, name in enumerate(self.session.axes):
            axis_item = QTableWidgetItem(name)
            axis_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            font = axis_item.font()
            font.setBold(True)
            axis_item.setFont(font)
            table.setItem(row, 0, axis_item)
            for column in range(1, len(_TABLE_COLUMNS)):
                cell = QTableWidgetItem('—')
                cell.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                      | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row, column, cell)
        # Uniform 30 px rows: resizeRowsToContents() measured 40 px once the theme
        # added cell padding, which pushed the action bar out of the window.
        header_view = table.verticalHeader()
        header_view.setDefaultSectionSize(28)
        header_view.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self._fit_table_height()

    def _fit_table_height(self) -> None:
        """Size the status table to exactly its rows.

        The height has to be measured *after* the stylesheet has been applied:
        the new theme adds padding to every cell, so a height computed at build
        time left the last row clipped and pushed the origin-input line on top of
        the table.
        """
        table = getattr(self, 'status_table', None)
        if table is None:
            return
        header = table.horizontalHeader().height() or 30
        rows = sum(max(table.rowHeight(r), 24) for r in range(table.rowCount()))
        frame = 2 * table.frameWidth()
        table.setFixedHeight(header + rows + frame + 4)

    # -- layout: scrollable panels ---------------------------------------
    def _wrap_panels_in_scroll(self) -> None:
        """Put the per-axis panels in a horizontal scroll area.

        Four panels side by side in a narrow window squeezed every label.  With a
        scroll area each panel keeps a usable minimum width and the row scrolls
        instead of collapsing.
        """
        old_layout = self.axis_panel_layout
        box = old_layout.parentWidget()
        scroll = QScrollArea(box)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Without an explicit height the scroll area collapses to its minimum and
        # clips the jog/target/home buttons inside every panel.
        scroll.setMinimumHeight(402)

        container = QWidget()
        self.panel_layout = QHBoxLayout(container)
        self.panel_layout.setContentsMargins(0, 0, 0, 0)
        self.panel_layout.setSpacing(10)
        for entry in self.axis_panels.values():
            panel = entry['panel']
            old_layout.removeWidget(panel)
            panel.setMinimumWidth(180)
            panel.setMaximumWidth(420)
            self.panel_layout.addWidget(panel, 1)
        scroll.setWidget(container)
        old_layout.addWidget(scroll)
        self.panels_scroll = scroll

    def _build_axis_panel(self, axis: str):
        """One axis card, pixel-matched to the approved mock-up.

        Card 12 px padding · name 13.5/600 · lamp 9 px · bar 16×146 · form gap 6 ·
        kv label 11.5 #8496ad · kv value 11.5/500 #22304a · inputs and buttons 12.5.
        """
        panel = QGroupBox()
        vbox = QVBoxLayout(panel)
        vbox.setSpacing(6)
        vbox.setContentsMargins(12, 12, 12, 12)

        head = QHBoxLayout()
        head.setSpacing(8)
        name = QLabel(axis)
        name.setFont(px_font(13.5, 'demibold'))
        name.setStyleSheet('color:#16233a;')
        head.addWidget(name)
        head.addStretch()

        light = QLabel()
        light.setFixedSize(9, 9)
        light.setStyleSheet('background:#e05e52; border-radius:4px;')
        head.addWidget(light, 0, Qt.AlignmentFlag.AlignVCenter)

        home_text = QLabel('未回零')
        home_text.setFont(px_font(11, 'regular'))
        home_text.setStyleSheet('color:#8496ad;')
        head.addWidget(home_text)
        vbox.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(10)
        bar = AxisBar()
        body.addWidget(bar, 0, Qt.AlignmentFlag.AlignTop)

        form = QVBoxLayout()
        form.setSpacing(6)

        def kv(caption: str, value: str) -> tuple[QHBoxLayout, QLabel]:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            left = QLabel(caption)
            left.setFont(px_font(11.5, 'regular'))
            left.setStyleSheet('color:#8496ad;')
            right = QLabel(value)
            right.setFont(px_font(11.5, 'medium'))
            right.setStyleSheet('color:#22304a;')
            row.addWidget(left)
            row.addStretch()
            row.addWidget(right)
            return row, right

        dpos_row, dpos_label = kv('指令', '0.0000')
        mpos_row, mpos_label = kv('反馈', '0.0000')
        form.addLayout(dpos_row)
        form.addLayout(mpos_row)

        step = QDoubleSpinBox()
        step.setRange(0.0001, 100.)
        step.setDecimals(4)
        step.setValue(0.1)
        step.setSuffix(' mm')
        step.setFont(px_font(12.5, 'medium'))
        step.setFixedHeight(30)
        step.setToolTip('点动步长（指令单位）')
        form.addWidget(step)

        jog_row = QHBoxLayout()
        jog_row.setSpacing(6)
        jog_neg = QPushButton('点动 −')
        jog_pos = QPushButton('点动 +')
        for button in (jog_neg, jog_pos):
            button.setFont(px_font(12.5, 'medium'))
            button.setFixedHeight(30)
            jog_row.addWidget(button)
        form.addLayout(jog_row)

        target = QDoubleSpinBox()
        target.setRange(-1e8, 1e8)
        target.setDecimals(4)
        target.setValue(0.)
        target.setSuffix(' mm')
        target.setFont(px_font(12.5, 'medium'))
        target.setFixedHeight(30)
        target.setToolTip('绝对目标位置（指令单位）')
        form.addWidget(target)

        abs_btn = QPushButton('定位')
        abs_btn.setProperty('accent', True)
        home_btn = QPushButton('回零')
        probe_btn = QPushButton('标度自检')
        probe_btn.setToolTip(
            '让该轴走一个极小步长再回到原位，测量「编码器位移 ÷ 指令位移」。'
            '比值不为 1 说明 UNITS 与反馈标度不匹配，轴的实际行程远大于指令数字。')
        for button in (abs_btn, home_btn, probe_btn):
            button.setFont(px_font(12.5, 'medium'))
            button.setFixedHeight(30)
            form.addWidget(button)

        body.addLayout(form, 1)
        vbox.addLayout(body)

        self.axis_panels[axis] = {
            'panel': panel, 'light': light, 'home_text': home_text,
            'bar': bar, 'dpos_label': dpos_label, 'mpos_label': mpos_label,
            'step': step, 'jog_neg': jog_neg, 'jog_pos': jog_pos,
            'target': target, 'abs_btn': abs_btn, 'home_btn': home_btn,
            'probe_btn': probe_btn,
        }
        jog_neg.clicked.connect(lambda _=False, a=axis: self.on_jog(a, -1))
        jog_pos.clicked.connect(lambda _=False, a=axis: self.on_jog(a, +1))
        abs_btn.clicked.connect(lambda _=False, a=axis: self.on_absolute(a))
        home_btn.clicked.connect(lambda _=False, a=axis: self.on_home(a))
        probe_btn.clicked.connect(lambda _=False, a=axis: self.on_probe_scale(a))
        add_card_shadow(panel, blur=12)
        return panel

    def _build_axis_panels(self) -> None:
        """Create one panel per active axis, into whichever layout exists yet."""
        target = getattr(self, 'panel_layout', None) or self.axis_panel_layout
        for name in self.session.axes:
            panel = self._build_axis_panel(name)
            panel.setMinimumWidth(196)
            target.addWidget(panel)

    def rebuild_for_model(self) -> None:
        """Recreate the table rows and the panels after a tuner-model switch."""
        for name in list(self.axis_panels):
            entry = self.axis_panels.pop(name)
            entry['panel'].deleteLater()
        self._build_axis_panels()
        self._rebuild_table()
        self._update_travel_label()

    # -- travel box ------------------------------------------------------
    def _travel_box(self, axis: str) -> tuple[float, float]:
        params = self.session.settings.axes.get(axis)
        if params is not None:
            low, high = float(params.rs_limit), float(params.fs_limit)
            if high > low and (high - low) < _FACTORY_LIMIT_SPAN:
                return low, high
        return DEFAULT_TRAVEL.get(axis, (-1e9, 1e9))

    def _limit_state_note(self, axis: str) -> str:
        """Describe the controller's own soft limits for this axis.

        The refusal above is the software guard; the controller's FS/RS_LIMIT
        is the second, independent protection.  Saying "they are still the
        factory values" is only true when they are -- measured on the bench
        (2026-11-08) they are written to the declared travel, so a warning that
        ignores that tells the operator to distrust a limit that is actually in
        place.
        """
        try:
            controller = self.session.require_controller()
            index = axis_index(axis)
            fs_limit = float(controller.link.get_fs_limit(index))
            rs_limit = float(controller.link.get_rs_limit(index))
        except Exception:  # noqa: BLE001 - the note must never mask the refusal
            return '无法读取控制器软限位，请人工确认。'
        if abs(fs_limit - rs_limit) >= _FACTORY_LIMIT_SPAN:
            return (f'控制器 FS/RS_LIMIT 仍是出厂值（{rs_limit:g}–{fs_limit:g}），'
                    '不能当保护用，请在轴参数页写入行程。')
        return f'控制器 FS/RS_LIMIT 已写入 {rs_limit:g}–{fs_limit:g} mm，与申报行程一致。'

    def _check_travel(self, axis: str, target: float) -> None:
        low, high = self._travel_box(axis)
        if not low <= target <= high:
            raise MotionError(
                f'{axis} 目标 {target:.4f} mm 超出申报行程 {low:g}–{high:g} mm，已拒绝下发。\n'
                + self._limit_state_note(axis))

    # -- motion overrides ------------------------------------------------
    def _absolute(self, axis: str, target: float) -> None:
        self._check_travel(axis, float(target))
        super()._absolute(axis, target)

    def _jog(self, axis: str, delta: float) -> None:
        controller = self.session.require_controller()
        current = float(controller.link.get_dpos(axis_index(axis)))
        self._check_travel(axis, current + float(delta))
        super()._jog(axis, delta)

    def _home(self, axis: str) -> None:
        """Home one axis: output pulse on the real machine, DATUM on the mock.

        The drive homes itself.  ``OP0…OP3`` map to ``X…Z3`` and the operator
        presses the same button twice, which is exactly an output on/off pulse.
        The controller's own ``DATUM`` is kept only as the fallback used by the
        virtual backend.
        """
        controller = self.session.require_controller()
        home_via_output = getattr(controller, 'home_via_output', None)
        if callable(home_via_output):
            self._home_via_output(controller, axis, home_via_output)
            return
        link = controller.link
        if axis not in controller.axes:
            raise ValueError(f'unknown axis: {axis}')
        controller._require_ready(require_home=False)
        if controller.stopped:
            raise MotionError('急停已锁存，请先复位急停')
        if controller.faulted:
            raise MotionError('运动故障已锁存，请先点「复位运动故障」')
        if not controller.config.independent_axes and axis == 'X':
            unreferenced = [name for name in controller.axes
                            if name != 'X' and name not in controller.homed_axes]
            if unreferenced:
                raise MotionError(
                    f'先把探针轴回零，再回零 X：未回零 {", ".join(unreferenced)}')

        index = controller.axes.index(axis)
        params = controller.axis_params.get(axis)
        mode = params.datum_mode if params else 4
        low, high = self._travel_box(axis)
        margin = 0.5

        link.home_axis(index, mode)
        deadline = time.monotonic() + self.HOME_TIMEOUT
        reason = ''
        while time.monotonic() < deadline:
            status = self._link_status(link, index)
            if has_fault(status):
                if status & 32:
                    reason = '轴已到反向硬限位仍未找到原点'
                elif status & 16:
                    reason = '轴已到正向硬限位仍未找到原点'
                else:
                    reason = f'触发 {describe_axis_status(status)}（AXISSTATUS={status}）'
                break
            position = float(link.get_dpos(index))
            if position < low - margin or position > high + margin:
                reason = f'位置 {position:.4f} mm 越出申报行程 {low:g}–{high:g} mm'
                break
            if self._link_idle(link, index):
                break
            time.sleep(0.05)
        else:
            reason = f'超过 {self.HOME_TIMEOUT:g}s 仍未结束'

        if reason:
            controller.stop_axis(axis)
            try:
                link.wait_idle([index], 3.0)
            except Exception:  # noqa: BLE001 - best effort on the way out
                pass
            datum_in = self._datum_input(axis)
            raise MotionError(
                f'{axis} 回零中止：{reason}。\n'
                f'  位置 {float(link.get_dpos(index)):.4f} mm，'
                f'home_status = {int(link.home_status(index))}，'
                f'原点输入 IN({datum_in}) = {self._input_state(datum_in)}\n'
                '检查 DATUM 方向是否与原点开关位置一致，以及行程限位是否已配置。')

        status = int(link.home_status(index))
        if status != 1:
            raise MotionError(
                f'{axis} 回零结束但未成功：home_status = {status}，'
                f'AXISSTATUS = {self._link_status(link, index)}')

        # DATUM modes 3/4 already clear DPOS; do it explicitly for any other
        # mode so the operator always starts from a zeroed coordinate.
        dpos = float(link.get_dpos(index))
        if abs(dpos) > 1e-3:
            setter = getattr(link, 'set_dpos', None)
            if callable(setter):
                setter(index, 0.0)
                operation_log.info('回零', '回零清零',
                                   f'{axis} DPOS {dpos:.4f} → 0', axis=axis)

        controller.homed_axes.add(axis)
        controller.homed = len(controller.homed_axes) == len(controller.axes)
        operation_log.log('回零', '单轴回零', f'{axis} 回零完成', 'info',
                          axis=axis, home_status=1)
        self.log(f'{axis} 回零完成，指令位置已清零')
        self.refresh_positions()

    def _home_via_output(self, controller, axis: str, home) -> None:
        """Pulse the axis' output and report what the encoder did."""
        try:
            result = home(axis)
        except Exception as exc:  # noqa: BLE001 - surfaced with the raw reason
            operation_log.exception('回零', '单轴回零', exc, axis=axis)
            raise
        travel = float(result.get('travel_mm', 0.0))
        elapsed = float(result.get('elapsed_s', 0.0))
        output = result.get('output')
        if abs(travel) < 0.005:
            operation_log.warn(
                '回零', '单轴回零',
                f'{axis} OP{output} 脉冲已发出，编码器位移仅 {travel * 1000:.1f} µm',
                axis=axis)
            self.log(f'{axis} 回零：OP{output} 脉冲已发出，编码器几乎无位移 '
                     f'（{travel * 1000:.1f} µm）——可能已在原点，或 OP 未接到驱动')
        else:
            operation_log.info(
                '回零', '单轴回零',
                f'{axis} OP{output} 脉冲，位移 {travel * 1000:.0f} µm，'
                f'耗时 {elapsed:.1f}s，DPOS 已清零，'
                f'IN{result.get("datum_input")}='
                f'{"ON" if result.get("origin_on") else "OFF"}', axis=axis)
            channel = result.get('datum_input')
            origin = result.get('origin_on')
            confirmed = ('，原点输入 IN%s=ON 已确认' % channel) if origin else ''
            self.log(f'{axis} 回零完成：OP{output} 输出脉冲，位移 '
                     f'{travel * 1000:.0f} µm，指令位置已清零{confirmed}')
        self.refresh_positions()

    @staticmethod
    def _link_idle(link, index: int) -> bool:
        reader = getattr(link, 'if_idle', None)
        return bool(reader(index)) if callable(reader) else True

    @staticmethod
    def _link_status(link, index: int) -> int:
        reader = getattr(link, 'axis_status', None)
        return int(reader(index)) if callable(reader) else 0

    # -- live status -----------------------------------------------------
    def refresh_positions(self) -> None:
        """Refresh the status widgets — from any thread.

        The base page calls this at the end of every move, and moves run on a
        worker thread.  Off the GUI thread the request is posted to the GUI
        thread instead of touching widgets directly.
        """
        current = getattr(self, '_gui_thread_id', None)
        if current is not None and current != threading.get_ident():
            self._refresh_requested.emit()
            return
        self._refresh_status()

    def _refresh_status(self) -> None:
        controller = self.session.controller
        if controller is None or not self.session.connected:
            return
        try:
            snapshot = controller.axis_status_snapshot()
        except Exception:  # noqa: BLE001 - polling must never raise
            return
        table = getattr(self, 'status_table', None)
        for row, entry in enumerate(snapshot):
            if table is None or row >= table.rowCount():
                break
            name = entry['axis']
            dpos = float(entry['dpos'])
            mpos = float(entry['mpos'])
            status_text = entry.get('status_text', '正常')
            # The stop reason is latched history; on a healthy axis it would only
            # add noise such as "正常 · CANCEL 执行中".
            stop_text = entry.get('stop_reason_text', '—')
            if int(entry['status']) and stop_text not in ('—', '', '无锁存停止原因'):
                summary = f'{status_text} · {stop_text}'
            else:
                summary = status_text
            # The origin input is the machine's own definition of "on zero":
            # IN16…IN19 for X…Z3, ON (1) when the axis sits on its switch.  Show
            # it next to the latched homing flag -- the switch opens again as
            # soon as the axis leaves zero, so it must not overwrite that flag.
            channel = int(entry.get('datum_input', -1))
            origin = entry.get('origin_on')
            if entry['home_status'] != 1:
                homed_text = '未回零'
            elif channel < 0:
                # Virtual controller: no wired origin inputs to report.
                homed_text = '已回零'
            elif origin is None:
                homed_text = f'已回零 · IN{channel}=?'
            else:
                homed_text = f'已回零 · IN{channel}={"ON" if origin else "OFF"}'
            values = (
                name,
                f'{dpos:.4f}',
                f'{mpos:.4f}',
                f'{dpos - mpos:.4f}',
                '是' if entry['idle'] else '否',
                homed_text,
                str(int(entry['status'])),
                summary,
            )
            fault = bool(entry.get('status_fault'))
            colour = QColor('#c0392b' if fault else
                            ('#8a5a00' if status_text != '正常' else '#1b2733'))
            origin_colour = (None if entry.get('origin_on') is None
                             else QColor('#1f7a4d' if entry['origin_on'] else '#8a5a00'))
            for column, text in enumerate(values):
                cell = table.item(row, column)
                if cell is None:
                    cell = QTableWidgetItem()
                    table.setItem(row, column, cell)
                cell.setText(text)
                if column == 5 and origin_colour is not None:
                    cell.setForeground(origin_colour)
                elif column in (6, 7):
                    cell.setForeground(colour)
                cell.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    if column else Qt.AlignmentFlag.AlignCenter)

            panel = self.axis_panels.get(name)
            if panel:
                span = self._BAR_MAX - self._BAR_MIN
                frac = (dpos - self._BAR_MIN) / span if span else 0.0
                panel['bar'].set_ratio(max(0.0, min(1.0, frac)))
                panel['dpos_label'].setText(f'{dpos:.4f}')
                panel['mpos_label'].setText(f'{mpos:.4f}')
                homed = entry['home_status'] == 1
                if homed:
                    panel['light'].setStyleSheet(
                        'background:#20b57f; border-radius:4px;')
                else:
                    panel['light'].setStyleSheet(
                        'background:#e05e52; border-radius:4px;')
                panel['home_text'].setText('已回零' if homed else '未回零')
        if not getattr(self, '_table_fitted', False) and table is not None \
                and table.isVisible():
            self._fit_table_height()
            self._table_fitted = True
        self._sync_kpis(snapshot)
        self._refresh_inputs()

    def _refresh_inputs(self) -> None:
        """Show the origin-input level of every axis once per second.

        A dead or mis-wired origin switch is invisible in the position readout:
        the axis simply runs to a limit.  Seeing ``IN`` stay at one level while
        the axis travels is what distinguishes "wrong direction" from "no signal".
        """
        now = time.monotonic()
        if now - getattr(self, '_last_input_poll', 0.0) < 1.0:
            return
        self._last_input_poll = now
        parts = []
        for name in self.session.axes:
            channel = self._datum_input(name)
            parts.append(f'{name}=IN{channel}:{self._input_state(channel)}')
        if hasattr(self, 'input_label'):
            self.input_label.setText('原点输入：' + '  '.join(parts))

    def _probe_scale(self, axis: str) -> None:
        """Scale self-check over a distance the encoder can actually resolve."""
        controller = self.session.require_controller()
        try:
            result = controller.probe_axis_scale(axis, distance=self.PROBE_DISTANCE_MM)
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            operation_log.exception('标度', '标度自检', exc, axis=axis)
            raise
        # This runs on a worker thread: hand the numbers to the GUI thread
        # instead of writing into the labels here.
        self._scale_probe_done.emit(result)

    def _apply_scale_probe(self, result: dict) -> None:
        """Show a finished scale self-check (GUI thread)."""
        axis = str(result.get('axis', '?'))
        ratio = float(result['ratio'])
        commanded = float(result['commanded'])
        feedback = float(result['feedback'])
        units = float(result.get('units', 0.0))
        self.refresh_positions()
        if abs(ratio - 1.0) <= 0.05:
            message = (f'{axis} 标度自检：指令 {commanded:.3f} mm → 编码器 '
                       f'{feedback:+.4f} mm，比值 {ratio:.4f}（一致）。'
                       f'UNITS={units:g} 脉冲/mm 正确。')
            self.scale_label.setStyleSheet('color:#1f7a4d;')
            self.apply_scale_button.setVisible(False)
            self._pending_scale = None
            operation_log.info('标度', '标度自检', message, axis=axis, ratio=ratio)
        else:
            suggested = float(result.get('suggested_units', units))
            message = (f'{axis} 标度自检：指令 {commanded:.3f} mm → 编码器 '
                       f'{feedback:+.4f} mm，比值 {ratio:.3f}。'
                       f'当前 UNITS={units:g}；若比值确实偏离 1，才需要调整。')
            self.scale_label.setStyleSheet('color:#c0392b; font-weight:600;')
            self._pending_scale = (axis, suggested)
            self.apply_scale_button.setText(
                f'采纳并写入修正后的 UNITS（{axis} → {suggested:g}）')
            self.apply_scale_button.setVisible(True)
            operation_log.warn('标度', '标度自检', message, axis=axis, ratio=ratio)
        self.scale_label.setText(message)
        self.log(message)
        self._reveal_notice()

    def _reveal_notice(self) -> None:
        """Show the scale result and scroll it into view.

        The page is taller than a 880 px window once the result card and its
        「采纳」 button appear, so without this the button was clipped by the
        bottom edge and the operator had to guess that there was something to
        scroll to.
        """
        card = getattr(self, '_notice_card', None)
        if card is None:
            return
        card.setVisible(True)
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        if parent is not None:
            # One event-loop turn later: the newly shown button changes the
            # card's height, and scrolling before the layout settles misses it.
            QTimer.singleShot(0, lambda: parent.ensureWidgetVisible(card, 0, 16))

    # -- diagnostics -----------------------------------------------------
    def _datum_input(self, axis: str) -> int:
        params = self.session.settings.axes.get(axis)
        if params is not None:
            return int(params.datum_in)
        return {'X': 16, 'Z1': 17, 'Z2': 18, 'Z3': 19}.get(axis, -1)

    def _input_state(self, channel: int) -> str:
        if channel < 0:
            return '未配置'
        controller = self.session.controller
        if controller is None:
            return '未知'
        try:
            return controller.link._basic_query(f'?IN({channel})').strip()
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            return '未知'

    def on_reset_fault(self) -> None:
        """Clear a latched motion fault so the operator is not stuck in a loop."""
        def action() -> None:
            controller = self.session.require_controller()
            controller.reset_fault()
            controller.refresh_home_state()
            operation_log.warn('系统', '复位运动故障',
                               '已清除运动故障锁存，需重新回零')
            self.log('运动故障已复位，请重新回零后再运动')
            self.refresh_positions()

        self.run_action(action)

    # -- motion submission -----------------------------------------------
    def _run_motion(self, action) -> None:
        """Submit one blocking move to a worker thread.

        Same contract as the base page (one move at a time, controls disabled
        while it runs), but through :func:`tunerlab_app.bg.run_in_background`,
        which keeps a strong reference to the task and delivers both outcomes on
        the GUI thread.  ``QRunnable`` objects started directly on the thread pool
        can be deleted by C++ while Python still holds the wrapper, which is
        another way this page used to disappear without a message.
        """
        if self._motion_busy:
            self.log('已有运动任务在执行，忽略本次请求')
            return
        self._motion_busy = True
        self._set_motion_controls_enabled(False)
        run_in_background(
            action,
            on_done=lambda _result: self._on_motion_done(),
            on_error=self._on_motion_error,
            description='motion')

    # -- error reporting -------------------------------------------------
    def report_error(self, exc: Exception) -> None:
        message = str(exc)
        hints = []
        if 'motion fault is latched' in message:
            hints.append('点「复位运动故障」清除锁存，然后重新回零。')
        if 'home the axes before motion' in message:
            hints.append('先点该轴的「回零」；若轴还在运动，等它停下来再回零。')
        if 'does not match the commanded displacement' in message:
            hints.append('这是标度保护：先做「标度自检」并采纳修正后的 UNITS，再运动。')
        if 'outside the configured limits' in message:
            hints.append('目标超出限位；检查轴参数里的 FS_LIMIT / RS_LIMIT。')
        text = message
        if hints:
            text += '\n\n建议：\n' + '\n'.join(f'· {hint}' for hint in hints)
        self.log(f'{type(exc).__name__}: {message}')
        QMessageBox.warning(self, '操作失败', text)

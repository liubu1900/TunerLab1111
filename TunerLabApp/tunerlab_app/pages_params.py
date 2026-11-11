"""Axis parameter page in the dashboard language.

The stock page is a toolbar plus a wide editable table.  That structure is right
— an engineer wants to see all twelve parameters for all axes at once — so this
subclass keeps it and only adds the summary strip that the other device pages
have, plus the card styling that comes from the shell stylesheet.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QSizePolicy,
)

from tuner.ui_hardware import AxisParameterPage

from .fonts import px_font
from .ui import Card

__all__ = ['SafeAxisParameterPage']


class SafeAxisParameterPage(AxisParameterPage):
    """``AxisParameterPage`` with a KPI strip above the table."""

    def __init__(self, session, parent=None):
        super().__init__(session, parent)
        self.layout().setContentsMargins(22, 0, 22, 18)
        self.layout().setSpacing(14)
        # Twelve parameters per axis do not fit a stretched column set: the
        # headers were truncated to "脉冲当量 UNI…".  Size to content and let the
        # table scroll horizontally instead.
        header = self.table.horizontalHeader()
        for column in range(self.table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.table.columnCount() - 1,
                                    QHeaderView.ResizeMode.Stretch)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self._add_toolbar_status()
        self._add_info_cards()
        self._sync_kpis()

    def _add_toolbar_status(self) -> None:
        """Right-aligned "4 轴 · 12 项 · 未修改" like the mock-up."""
        self.toolbar_status = QLabel('')
        self.toolbar_status.setObjectName('PageHint')
        self.toolbar_status.setFont(px_font(11.5, 'light'))
        self.layout().insertWidget(1, self.toolbar_status)
        self._refresh_toolbar_status()

    def _refresh_toolbar_status(self) -> None:
        if not hasattr(self, 'toolbar_status'):
            return
        self.toolbar_status.setText(
            f'{len(self.session.axes)} 轴 · {self.table.columnCount() - 1} 项 · '
            + ('未保存修改' if self.dirty else '已保存配置'))

    def _add_info_cards(self) -> None:
        """The three reference cards under the table, as in the mock-up."""
        row = QHBoxLayout()
        row.setSpacing(14)
        notes = (
            ('坐标基准', 'UNITS 为脉冲/mm；MPOS 单位为 10 µm，DPOS 为 mm。'),
            ('回零策略', '按每轴设置的模式、原点输入和回零输出执行；请核对实际接线。'),
            ('保护', '软限位按每轴保存并读回核对；写入不一致时停止该轴操作。'),
        )
        for title, text in notes:
            card = Card(title)
            label = QLabel(text)
            label.setObjectName('PageHint')
            label.setWordWrap(True)
            label.setFont(px_font(11.5, 'light'))
            card.body.addWidget(label)
            card.setSizePolicy(QSizePolicy.Policy.Expanding,
                               QSizePolicy.Policy.Maximum)
            row.addWidget(card, 1)
        layout = self.layout()
        if layout is not None:
            layout.addLayout(row)
            layout.addStretch(1)

    def _add_kpi_row(self) -> None:
        self.kpi_axes = KpiTile('轴数', '—', '当前 tuner 型号')
        self.kpi_items = KpiTile('参数项', '—', '每轴可写字段')
        self.kpi_datum = KpiTile('回零模式', '—', 'DATUM_MODE')
        self.kpi_scale = KpiTile('反馈标度', '—', 'MPOS 计数 / mm')
        layout = self.layout()
        if layout is not None:
            layout.insertWidget(0, KpiRow([self.kpi_axes, self.kpi_items,
                                           self.kpi_datum, self.kpi_scale]))

    def _sync_kpis(self) -> None:
        if not hasattr(self, 'kpi_axes'):
            return
        axes = list(self.session.axes)
        params = self.session.settings.axes
        self.kpi_axes.set_value(str(len(axes)), ' · '.join(axes))
        self.kpi_items.set_value(str(max(0, self.table.columnCount() - 1)),
                                 '轴 / 运动 / 回零 / 限位')
        modes = {name: int(getattr(params[name], 'datum_mode', 0))
                 for name in axes if name in params}
        if modes:
            groups: dict[int, list[str]] = {}
            for name, mode in modes.items():
                groups.setdefault(mode, []).append(name)
            text = ' · '.join(f'{"、".join(names)}={mode}'
                              for mode, names in sorted(groups.items()))
        else:
            text = '—'
        self.kpi_datum.set_value(text, '4 = 反向找原点开关')
        scales = {round(float(getattr(params[name], 'feedback_scale', 0.0)), 3)
                  for name in axes if name in params}
        if scales:
            value = ' / '.join(f'{s:g}' for s in sorted(scales))
            sub = '与 MPOS 单位一致' if scales == {100.0} else '需核对 MPOS 单位'
            self.kpi_scale.set_value(value, sub,
                                     'ok' if scales == {100.0} else 'warn')
        else:
            self.kpi_scale.set_value('—', '')

    # Keep the strip truthful after a read-from-controller or a model switch.
    def fill_table(self, axis_params) -> None:  # noqa: D102 - base override
        super().fill_table(axis_params)
        self._sync_kpis()
        self._refresh_toolbar_status()

    def rebuild_rows(self) -> None:
        super().rebuild_rows()
        self._sync_kpis()

    def update_status(self):
        super().update_status()
        self._refresh_toolbar_status()

    def _submit_io(self, write=False, selected=False):
        from .bg import run_in_background
        if getattr(self, '_io_busy', False):
            return
        try:
            self.session.check_context(self._owner)
            if write and self.dirty:
                raise ValueError('请先保存配置，再明确写入控制器')
            names = list(self.session.axes)
            if selected:
                row = self.table.currentRow()
                if row < 0:
                    raise ValueError('请先选中一根轴')
                names = [names[row]]
            work = (lambda: self.session.apply_parameters_to_controller(names)) if write else self.session.read_parameters_from_controller
            snapshot = dict(self._owner)
            work = self.session.reserve_task(work, expected=snapshot)
        except Exception as exc:
            self.report_error(exc)
            return
        self._io_busy = True
        for button in (self.read_button, self.write_button, self.write_axis_button, self.save_button):
            button.setEnabled(False)
        def restore():
            self._io_busy = False
            for button in (self.read_button, self.write_button, self.write_axis_button, self.save_button):
                button.setEnabled(True)
        def done(result):
            restore()
            try:
                self.session.check_context(snapshot)
            except Exception:
                self.update_status()
                self.log('旧目标的读写结果已丢弃，请读取当前 Tuner 参数')
                return
            if not write:
                self._live = result
            self.update_status()
            if write and any(value['status'] != 'matched' for value in result.values()):
                self.report_error(ValueError('部分轴写入或校验失败，详情见逐轴状态'))
        def failed(exc):
            restore()
            self.report_error(exc)
        run_in_background(work, on_done=done, on_error=failed, description='axis parameters')

    def on_read(self):
        self._submit_io()

    def on_write(self):
        self._submit_io(write=True)

    def on_write_axis(self):
        self._submit_io(write=True, selected=True)

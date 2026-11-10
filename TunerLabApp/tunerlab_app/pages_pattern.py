"""The Pattern Editor page (P2).

A standard pattern editor answers one question: **where should the tuner go?**
The operator either picks points on the Smith chart or asks for a shape — a
constant-|Γ| circle, a radial sweep — and the pattern is stored so the same
sequence can be run again later.

The page shows three things side by side:

* the Smith chart with the pattern's points on it;
* the generator controls and the stored pattern list;
* the **resolution table**: for every target Γ, the nearest *calibrated* point
  and how far off it is.  That table is what makes a pattern actionable — it
  says, before anything moves, whether the whole pattern is reachable.

Nothing on this page moves the machine.  Running a pattern is a separate,
explicit action on the tuning page, because a pattern is a plan and a plan
does not drive the hardware by itself.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
)

from tuner.patterns import (
    delete_pattern, generate_points, list_patterns, load_pattern, pattern_points,
    save_pattern,
)
from tuner.ui import SmithChart
from tuner.ui_theme import numeric_font
from tuner.web import handlers

from . import paths
from .pages_base import _StudioPage, _page_title
from .ui import (Card, field_grid, field_label, hint_label, page_header,
                   smith_control_row)

__all__ = ['StudioPatternPage']


class StudioPatternPage(_StudioPage):
    """Draw, store and resolve target patterns."""

    def __init__(self, app_state, parent=None):
        super().__init__(app_state, parent)
        self._points: list[complex] = []
        self._pattern_path = ''
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 0, 22, 18)
        root.setSpacing(14)
        root.addWidget(page_header(
            '目标图案',
            '在 Smith 图上点选或用生成器画出要走的路径，存成图案。'
            '下方表格把每个目标 Γ 对到最近的已标定点，运行之前就知道整条路径够不够得着。'))

        body = QHBoxLayout()
        body.setSpacing(14)
        root.addLayout(body, 1)

        # -- left: the chart --------------------------------------------
        chart_card = Card('图案 (缩放模式下左键加点 · 右键删点)')
        self.chart = SmithChart()
        chart_card.body.addLayout(smith_control_row(self.chart))
        chart_card.body.addWidget(self.chart, 1)
        self.chart_hint = QLabel('尚未添加点')
        self.chart_hint.setObjectName('PageHint')
        chart_card.body.addWidget(self.chart_hint)
        body.addWidget(chart_card, 3)

        # -- right: generator, list, resolution --------------------------
        right = QVBoxLayout()
        right.setSpacing(14)
        body.addLayout(right, 4)

        gen_card = Card('生成与保存')
        gen_grid = field_grid()
        self.kind = QComboBox()
        for key, label in (('points', '手动点选'),
                           ('circle', '等 |Γ| 圆'),
                           ('radial', '径向扫描')):
            self.kind.addItem(label, key)
        gen_grid.addWidget(field_label('类型'), 0, 0)
        gen_grid.addWidget(self.kind, 0, 1)

        self.radius = QDoubleSpinBox()
        self.radius.setRange(0.0, 1.0)
        self.radius.setDecimals(4)
        self.radius.setSingleStep(0.05)
        self.radius.setValue(0.8)
        self.phase_start = QDoubleSpinBox()
        self.phase_start.setRange(-360.0, 360.0)
        self.phase_start.setDecimals(2)
        self.phase_start.setValue(0.0)
        self.phase_stop = QDoubleSpinBox()
        self.phase_stop.setRange(-360.0, 360.0)
        self.phase_stop.setDecimals(2)
        self.phase_stop.setValue(360.0)
        self.count = QSpinBox()
        self.count.setRange(1, 200)
        self.count.setValue(8)
        gen_grid.addWidget(field_label('|Γ| 半径'), 1, 0)
        gen_grid.addWidget(self.radius, 1, 1)
        gen_grid.addWidget(field_label('起始相位 (°)'), 2, 0)
        gen_grid.addWidget(self.phase_start, 2, 1)
        gen_grid.addWidget(field_label('终止相位 (°)'), 3, 0)
        gen_grid.addWidget(self.phase_stop, 3, 1)
        gen_grid.addWidget(field_label('点数'), 4, 0)
        gen_grid.addWidget(self.count, 4, 1)
        gen_card.body.addLayout(gen_grid)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.generate_button = QPushButton('生成图案')
        self.generate_button.clicked.connect(self.on_generate)
        self.save_button = QPushButton('保存图案…')
        self.save_button.clicked.connect(self.on_save)
        self.clear_button = QPushButton('清空')
        self.clear_button.clicked.connect(self.on_clear)
        self.resolve_button = QPushButton('对照校准库')
        self.resolve_button.setProperty('accent', True)
        self.resolve_button.clicked.connect(self.on_resolve)
        for widget in (self.generate_button, self.save_button, self.clear_button,
                       self.resolve_button):
            buttons.addWidget(widget)
        buttons.addStretch()
        gen_card.body.addLayout(buttons)
        right.addWidget(gen_card)

        list_card = Card('已存图案')
        self.pattern_list = QListWidget()
        self.pattern_list.currentRowChanged.connect(self.on_select_pattern)
        list_card.body.addWidget(self.pattern_list)
        list_buttons = QHBoxLayout()
        list_buttons.setSpacing(8)
        self.load_button = QPushButton('载入')
        self.load_button.clicked.connect(self.on_load)
        self.delete_button = QPushButton('删除')
        self.delete_button.clicked.connect(self.on_delete)
        list_buttons.addWidget(self.load_button)
        list_buttons.addWidget(self.delete_button)
        list_buttons.addStretch()
        list_card.body.addLayout(list_buttons)
        right.addWidget(list_card)
        right.addStretch(1)

        # -- resolution table --------------------------------------------
        resolve_card = Card('目标 Γ 对照已标定点')
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ['#', '目标 Γ', '目标 Z (Ω)', '最近已标定点', '|ΔΓ|', '可达'])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.table.setMaximumHeight(230)
        resolve_card.body.addWidget(self.table)
        self.resolve_summary = QLabel('尚未对照。加载校准库后点「对照校准库」。')
        self.resolve_summary.setObjectName('PageHint')
        self.resolve_summary.setWordWrap(True)
        resolve_card.body.addWidget(self.resolve_summary)
        root.addWidget(resolve_card)

        self.chart.picked.connect(self.on_pick)
        self.chart.right_picked.connect(self.on_right_pick)
        self.kind.currentIndexChanged.connect(self._sync_kind)
        self._sync_kind()
        self.refresh_patterns()

    # -- helpers ---------------------------------------------------------
    def _sync_kind(self) -> None:
        circle = self.kind.currentData() == 'circle'
        radial = self.kind.currentData() == 'radial'
        self.radius.setEnabled(circle)
        self.phase_start.setEnabled(circle)
        self.phase_stop.setEnabled(circle)

    def _redraw(self) -> None:
        self.chart.points = (np.array(self._points, dtype=complex)
                             if self._points else np.array([], complex))
        self.chart.update()
        self.chart_hint.setText(
            f'{len(self._points)} 个点' + (' · 右键点击圆内可删除最近的点'
                                          if self._points else ''))

    def refresh_patterns(self) -> None:
        self.pattern_list.clear()
        for record in list_patterns():
            item = QListWidgetItem(
                f"{record.get('name', '')}  ({record.get('count', 0)} 点)")
            item.setData(Qt.ItemDataRole.UserRole, record.get('path', ''))
            self.pattern_list.addItem(item)

    def on_library_changed(self, path: str) -> None:
        """Re-resolve the target pattern against the newly active library.

        Reachability is a property of the *calibration*, not of the pattern:
        the same target Γ can be reachable in one library and not in another.
        The old resolution table is therefore meaningless after a switch.  When
        the operator already has a pattern, re-run the comparison immediately
        so the page shows the new library's verdict without an extra click.
        """
        self.refresh_patterns()
        self.table.setRowCount(0)
        self.resolve_summary.setStyleSheet('')
        if path and self._points:
            self.resolve_summary.setText('校准库已切换，正在重新对照…')
            self.on_resolve()
        elif path:
            self.resolve_summary.setText(
                '当前校准库已切换。加入图案点后点「对照校准库」核对可达性。')
        else:
            self.resolve_summary.setText(
                '尚未对照。加载校准库后点「对照校准库」。')

    def _parameters(self) -> dict:
        return {
            'radius': self.radius.value(),
            'phase_start': self.phase_start.value(),
            'phase_stop': self.phase_stop.value(),
            'count': self.count.value(),
        }

    # -- chart -----------------------------------------------------------
    def on_pick(self, gamma) -> None:
        if gamma is None:
            return
        value = complex(gamma)
        if abs(value) > 1.0:
            self.report('图案点必须在单位圆内')
            return
        self._points.append(value)
        self._redraw()

    def on_right_pick(self, gamma) -> None:
        """Remove the pattern point closest to the right-clicked spot."""
        if gamma is None or not self._points:
            return
        target = complex(gamma)
        index = min(range(len(self._points)),
                    key=lambda i: abs(self._points[i] - target))
        removed = self._points.pop(index)
        self._redraw()
        self.report(f'已删除图案点 {removed.real:+.3f}{removed.imag:+.3f}j')

    # -- actions ---------------------------------------------------------
    def on_generate(self) -> None:
        kind = self.kind.currentData()
        try:
            points = generate_points(kind, self._parameters())
        except Exception as exc:  # noqa: BLE001
            self.warn('生成失败', str(exc))
            return
        self._points = list(points)
        self._redraw()
        self.report(f'已生成 {len(self._points)} 个点')

    def on_clear(self) -> None:
        self._points = []
        self._pattern_path = ''
        self._redraw()
        self.report('图案已清空')

    def on_save(self) -> None:
        if not self._points:
            QMessageBox.information(self, '保存图案', '图案还没有点')
            return
        name, ok = QInputDialog.getText(self, '保存图案', '图案名称:')
        if not ok or not name.strip():
            return
        try:
            record = save_pattern(name, self.kind.currentData(),
                                 self._parameters(), self._points)
        except Exception as exc:  # noqa: BLE001
            self.warn('保存失败', str(exc))
            return
        self._pattern_path = record['path']
        self.refresh_patterns()
        self.report(f'图案已保存: {name}')

    def on_load(self) -> None:
        row = self.pattern_list.currentRow()
        if row < 0:
            QMessageBox.information(self, '载入图案', '请先在列表里选一个图案')
            return
        path = self.pattern_list.item(row).data(Qt.ItemDataRole.UserRole)
        try:
            record = load_pattern(path)
        except Exception as exc:  # noqa: BLE001
            self.warn('载入失败', str(exc))
            return
        self._points = pattern_points(record)
        self._pattern_path = path
        self._redraw()
        self.report(f'已载入图案: {record.get("name", "")}')

    def on_delete(self) -> None:
        row = self.pattern_list.currentRow()
        if row < 0:
            QMessageBox.information(self, '删除图案', '请先在列表里选一个图案')
            return
        item = self.pattern_list.item(row)
        name = item.text().strip()
        answer = QMessageBox.question(
            self, '删除图案', f'确定删除图案「{name}」？\n此操作不可撤销。',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        delete_pattern(path)
        self.refresh_patterns()
        self.report('图案已删除')

    def on_select_pattern(self, row: int) -> None:
        if row < 0:
            return
        item = self.pattern_list.item(row)
        path = item.data(Qt.ItemDataRole.UserRole)
        try:
            record = load_pattern(path)
        except Exception:  # noqa: BLE001
            return
        self._points = pattern_points(record)
        self._pattern_path = path
        self._redraw()

    def on_resolve(self) -> None:
        """Resolve every target Γ against the loaded calibration."""
        if not self._points:
            QMessageBox.information(self, '对照校准库', '图案还没有点')
            return
        body = {'points': [[value.real, value.imag] for value in self._points]}
        self.run(lambda: handlers.studio_pattern_resolve(self.state, body),
                 self._resolved, title='对照失败', description='pattern resolve')

    def _resolved(self, data: dict) -> None:
        rows = data.get('rows', [])
        self.table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            position = item.get('position')
            position_text = ('—' if not position
                             else ' | '.join(f'{v:.4f}' for v in position))
            values = (
                str(item.get('index', row)),
                item.get('gamma_label', '—'),
                f"{item.get('impedance_re', 0):.2f}"
                f"{item.get('impedance_imag', 0):+.2f}j",
                position_text,
                '—' if item.get('error') is None else f"{item['error']:.6f}",
                '是' if item.get('reachable') else '否',
            )
            for column, text in enumerate(values):
                cell = QTableWidgetItem(str(text))
                cell.setFont(numeric_font(9))
                self.table.setItem(row, column, cell)
        worst = data.get('worst_error')
        self.resolve_summary.setText(
            f"{data.get('reachable', 0)}/{data.get('count', 0)} 个目标可达"
            + (f" · 最大 |ΔΓ| = {worst:.6f}" if worst is not None else '')
            + ('' if data.get('all_reachable') else ' · 有目标不可达'))
        self.resolve_summary.setStyleSheet(
            '' if data.get('all_reachable') else 'color:#b03a2e;')
        self.report(f'图案对照完成: {data.get("reachable", 0)}/'
                    f'{data.get("count", 0)} 可达')

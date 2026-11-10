"""P1 desktop pages: Calibration Browser, reference planes, deviation card.

This module carries a standard *Calibration Browser* (F-12) to the desktop shell
and the two helper panels the other pages reuse:

* :class:`StudioLibraryBrowserPage` — list every ``*.h5`` library in a folder,
  show each one's 14-field header, diff two of them, export them to common
  file formats (F-13) and derive a new library from them (F-14).
* :class:`ReferencePlanePanel` — the six standard reference planes plus the S2P
  de-embedding gate (F-10); a missing frequency is an abort, never a silent
  interpolation.
* :class:`DeviationCard` — the target-vs-measured comparison card (F-19) in the
  one canonical format :mod:`tunerlab_app.smith_spec` defines.

Everything here is a thin client over :mod:`tuner.web.handlers`; the algorithms
stay in the backend modules.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

from tuner.ui_theme import numeric_font
from tuner.web import handlers

from . import paths
from .pages_base import _StudioPage, _page_title
from .smith_spec import SmithSpec
from .ui import Card, field_label, hint_label, page_header

__all__ = ['DeviationCard', 'ReferencePlanePanel', 'StudioLibraryBrowserPage']


class ReferencePlanePanel(Card):
    """F-10: pick one of the six standard reference planes and gate the S2P.

    The six planes follow the usual taxonomy.  Only the ``Connectorized`` and
    ``Waveguide`` families carry a de-embedding file in practice, but the panel
    stays open for every plane so an operator can record which reference the
    library was taken at — that field is written into the library header.

    The S2P gate is deliberately strict: the file must cover every frequency of
    the library.  A missing frequency is an *abort*, because interpolating a
    de-embedding network across a gap silently corrupts every point that
    depends on it.
    """

    def __init__(self, app_state, parent=None):
        super().__init__('参考面与去嵌入 (F-10)', parent)
        self.state = app_state
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)

        self.plane = QComboBox()
        self._planes: list[dict] = []
        grid.addWidget(field_label('参考面'), 0, 0)
        grid.addWidget(self.plane, 0, 1, 1, 2)

        self.s2p_path = QLineEdit('')
        self.s2p_path.setPlaceholderText('去嵌入 S2P 文件（可留空）')
        browse = QPushButton('浏览…')
        browse.clicked.connect(self._browse)
        grid.addWidget(field_label('去嵌入 S2P'), 1, 0)
        grid.addWidget(self.s2p_path, 1, 1)
        grid.addWidget(browse, 1, 2)

        self.check_button = QPushButton('校验 S2P 覆盖')
        self.check_button.setProperty('accent', True)
        self.check_button.clicked.connect(self.check)
        grid.addWidget(self.check_button, 2, 2)

        self.status = QLabel('未选择参考面')
        self.status.setObjectName('PageHint')
        self.status.setWordWrap(True)
        grid.addWidget(self.status, 3, 0, 1, 3)
        self.body.addLayout(grid)

        self._load_planes()

    # -- helpers ---------------------------------------------------------
    def _load_planes(self) -> None:
        try:
            data = handlers.studio_reference_planes(self.state, {})
        except Exception as exc:  # noqa: BLE001 - the panel must still render
            self.status.setText(f'参考面列表不可用: {exc}')
            return
        self._planes = data.get('planes', [])
        self.plane.blockSignals(True)
        self.plane.clear()
        for plane in self._planes:
            # Only the name goes into the box: the descriptions are long
            # sentences, and a combo box that cannot show its own item is
            # unreadable.  The description appears in the status line below.
            label = str(plane.get('name') or plane.get('id') or '')
            if plane.get('deembed'):
                label += '（可去嵌入）'
            self.plane.addItem(label, plane.get('id', ''))
        self.plane.blockSignals(False)
        self.plane.currentIndexChanged.connect(lambda _i: self._describe())

    def _describe(self) -> None:
        plane_id = self.plane.currentData()
        for plane in self._planes:
            if plane.get('id') == plane_id:
                notes = plane.get('notes') or plane.get('description') or ''
                self.status.setText(f"参考面: {plane.get('id')} · {notes}"
                                    if notes else f"参考面: {plane_id}")
                return
        self.status.setText(f'参考面: {plane_id}')

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, '选择去嵌入 S2P', str(paths.calibration_dir()),
            'Touchstone (*.s2p *.s2p.txt);;所有文件 (*)')
        if path:
            self.s2p_path.setText(path)
            self.check()

    # -- public API ------------------------------------------------------
    def reference_plane(self) -> str:
        return str(self.plane.currentData() or 'standard')

    def deembed_s2p(self) -> str:
        return self.s2p_path.text().strip()

    def setup_body(self) -> dict:
        """The reference-plane setup the handler expects.

        Named ``setup_body`` and not ``body``: :class:`Card` already owns a
        ``body`` attribute (the card's inner layout).
        """
        return {'reference_plane': self.reference_plane(),
                'deembed_s2p': self.deembed_s2p()}

    def check(self) -> dict | None:
        """Validate the setup; returns the setup dict or ``None`` on failure."""
        body = self.setup_body()
        try:
            data = handlers.studio_reference_planes(self.state, body)
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f'校验失败: {exc}')
            self.status.setStyleSheet('color:#b03a2e;')
            return None
        setup = data.get('setup')
        if not setup:
            self.status.setText('未提供 S2P，跳过去嵌入校验（仅登记参考面）')
            self.status.setStyleSheet('')
            return None
        coverage = setup.get('coverage')
        if not coverage:
            self.status.setText('未提供 S2P，跳过去嵌入校验（仅登记参考面）')
            self.status.setStyleSheet('')
            return setup
        if coverage.get('passed'):
            self.status.setText(
                f"参考面 {setup.get('reference_plane_name') or setup.get('reference_plane')}"
                f" · S2P 覆盖完整"
                f"（{coverage.get('s2p_count', 0)} 个夹具频点覆盖 "
                f"{coverage.get('covered', 0)}/{coverage.get('library_count', 0)} 个库频点）")
            self.status.setStyleSheet('color:#1f7a4d;')
        else:
            missing = coverage.get('missing') or []
            reason = coverage.get('reason') or ''
            if missing:
                text = (f"S2P 缺 {len(missing)} 个库频点，已中止："
                        + ', '.join(f'{v / 1e9:.4f} GHz' for v in missing[:8]))
                if len(missing) > 8:
                    text += ' …'
                text += '。缺频点必须补齐，不得插值。'
            else:
                text = f'无法校验 S2P 覆盖：{reason}'
            self.status.setText(text)
            self.status.setStyleSheet('color:#b03a2e;')
        return setup


class DeviationCard(QGroupBox):
    """F-19: the target-vs-measured card, in the canonical Smith format.

    Shows the predicted Γ and the measured Γ side by side, the impedance delta,
    the VSWR deviation, whether the point is inside the operator's tolerance and
    which standard reachability band it falls in — with the relaxed tolerance
    Standard practice suggests for the high-|Γ| region.
    """

    def __init__(self, title: str = '目标-实测对照卡 (F-19)', parent=None):
        super().__init__(title, parent)
        self.spec = SmithSpec()
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['量', '预测', '实测'])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        # The card now owns a full-height right column on the tuning page.
        # Stretch the table and detail inside that column instead of pinning
        # both to a hard max: a minimum keeps each readable when the window is
        # short, and the stretch lets them breathe on a tall one.
        self.table.setMinimumHeight(96)
        self.table.setMaximumHeight(240)
        layout.addWidget(self.table, 3)

        self.verdict = QLabel('—')
        self.verdict.setWordWrap(True)
        self.verdict.setFont(numeric_font(10, bold=True))
        layout.addWidget(self.verdict)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(numeric_font(9))
        self.detail.setMinimumHeight(72)
        self.detail.setMaximumHeight(150)
        layout.addWidget(self.detail, 2)

    def clear(self) -> None:
        self.table.setRowCount(0)
        self.verdict.setText('—')
        self.verdict.setStyleSheet('')
        self.detail.setPlainText('')

    def show_report(self, report: dict) -> None:
        """Render a deviation report.

        Accepts both shapes: the raw
        :func:`tuner.reporting.deviation_report` dictionary (``predicted`` /
        ``measured``) and the :meth:`SmithSpec.comparison_card` wrapper
        (``predicted_card`` / ``measured_card``), which carries the canonical
        formatting.  The card prefers the formatted one and falls back to the
        raw keys, so it works whichever the caller has.
        """
        predicted = (report.get('predicted_card')
                     or report.get('predicted') or {})
        measured = (report.get('measured_card')
                    or report.get('measured') or {})
        rows = (
            ('Γ（幅相）', predicted.get('gamma_label', '—'),
             measured.get('gamma_label', '—')),
            ('Γ（直角）', predicted.get('gamma_rect', '—'),
             measured.get('gamma_rect', '—')),
            ('Z (Ω)', predicted.get('z_label', '—'),
             measured.get('z_label', '—')),
            ('归一化 z', predicted.get('smith_coords', '—'),
             measured.get('smith_coords', '—')),
            ('VSWR', predicted.get('vswr', '—'), measured.get('vswr', '—')),
            ('回波损耗', predicted.get('return_loss', '—'),
             measured.get('return_loss', '—')),
        )
        self.table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, text in enumerate(values):
                cell = QTableWidgetItem(str(text))
                cell.setFont(numeric_font(9))
                self.table.setItem(row, column, cell)

        band_label = report.get('gamma_band_label') or report.get('gamma_band', '')
        within = bool(report.get('within_tolerance'))
        error_text = report.get('vector_error_text')
        if error_text is None:
            error_text = f"{float(report.get('vector_error', 0.0)):.6f}"
        self.verdict.setText(
            f"{'达标' if within else '超差'} · |ΔΓ| {error_text} "
            f"(容差 {report.get('tolerance', 0):g}) · ΔVSWR "
            f"{report.get('vswr_delta_text', '—')} · {band_label}")
        self.verdict.setStyleSheet('color:#1f7a4d;' if within else 'color:#b03a2e;')

        dr = report.get('impedance_delta_r')
        dx = report.get('impedance_delta_x')
        lines = [
            f"阻抗偏差: ΔR = {dr:+.3f} Ω" if dr is not None else '阻抗偏差: —',
            f"           ΔX = {dx:+.3f} Ω" if dx is not None else '',
            f"可达带: {band_label}",
            f"建议容差: {report.get('suggested_tolerance', report.get('tolerance', 0)):g}"
            '（高 |Γ| 区建议放宽）',
        ]
        self.detail.setPlainText('\n'.join(line for line in lines if line))


class StudioLibraryBrowserPage(_StudioPage):
    """F-12/F-13/F-14: the Calibration Browser.

    A standard calibration browser is where an operator finds an old library, sees
    what is inside it, compares it with a newer one and exports it for another
    tool.  Everything is read-only on the source: exports write new files and
    the derive actions write a *new* library that records its parent, so a
    576-point production library is never modified in place.

    It is also where an old library becomes the **active calibration**.  standard
    keeps one active calibration for the whole application: activating a library
    here re-bases the Smith chart, the target-pattern editor, the tuner, the
    validation report and the audit view on that file and its frequency grid.
    Selection alone used to do nothing, which is what broke the linkage.
    """

    #: An old library was made active (path).  The shell shows a status line.
    activated = Signal(str)

    def __init__(self, app_state, parent=None):
        super().__init__(app_state, parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)
        root.addLayout(_page_title(
            '校准库浏览器',
            '列出目录里的全部校准库，比较两个库，导出成通用格式，'
            '或从库派生一个新库（截断 / 反转 / 切分 / 夹具去嵌入）。'
            '所有后处理都写新文件，源库永不被改动。'))

        # -- scan row ---------------------------------------------------
        scan_box = QGroupBox('库目录')
        scan_row = QHBoxLayout(scan_box)
        self.directory = QLineEdit(str(paths.calibration_dir()))
        # The full path is longer than the box; the field scrolls it.
        self.directory.setProperty('allowClip', True)
        browse = QPushButton('浏览…')
        browse.clicked.connect(self._browse_dir)
        self.scan_button = QPushButton('扫描')
        self.scan_button.setProperty('accent', True)
        self.scan_button.clicked.connect(self.on_scan)
        self.recursive = QComboBox()
        self.recursive.addItem('仅当前目录', False)
        self.recursive.addItem('含子目录', True)
        scan_row.addWidget(QLabel('目录'))
        scan_row.addWidget(self.directory, 1)
        scan_row.addWidget(browse)
        scan_row.addWidget(self.recursive)
        scan_row.addWidget(self.scan_button)
        root.addWidget(scan_box)

        # -- library table ----------------------------------------------
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ['名称', '校准 ID', '有效点/总点', '层数', '频率 (GHz)', '|Γ|max',
             'VSWRmax'])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.table.setMaximumHeight(210)
        root.addWidget(self.table)

        self.status = QLabel('尚未扫描')
        self.status.setObjectName('PageHint')
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        # -- activate / compare / export / derive ------------------------
        # A double click on a row follows the usual convention: open that calibration.
        self.table.doubleClicked.connect(lambda _index: self.on_activate())
        actions = QHBoxLayout()
        self.activate_button = QPushButton('设为当前校准库')
        self.activate_button.setProperty('accent', True)
        self.activate_button.setToolTip(
            '把选中的库设为全局活动校准库；Smith 图、目标图案、阻抗调谐、'
            '验证报告与报告审计会一起切换到该库的频带与标定点。')
        self.activate_button.clicked.connect(self.on_activate)
        self.goto_smith_button = QPushButton('在 Smith 图查看')
        self.goto_smith_button.clicked.connect(
            lambda: self.navigate.emit('studio-smith'))
        self.compare_button = QPushButton('对比选中的两个库')
        self.compare_button.clicked.connect(self.on_compare)
        self.export_button = QPushButton('导出…')
        self.export_button.clicked.connect(self.on_export)
        self.derive_button = QPushButton('派生新库…')
        self.derive_button.clicked.connect(self.on_derive)
        for widget in (self.activate_button, self.goto_smith_button,
                       self.compare_button, self.export_button, self.derive_button):
            actions.addWidget(widget)
        actions.addStretch()
        root.addLayout(actions)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(numeric_font(9))
        self.output.setPlaceholderText(
            '对比结果、导出摘要与派生库信息会显示在这里')
        root.addWidget(self.output, 1)

        self._libraries: list[dict] = []

    # -- helpers ---------------------------------------------------------
    def _browse_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, '选择校准库目录', self.directory.text() or str(paths.calibration_dir()))
        if directory:
            self.directory.setText(directory)
            self.on_scan()

    def _selected(self) -> list[dict]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        return [self._libraries[row] for row in rows
                if 0 <= row < len(self._libraries)]

    # -- active calibration (standard) --------------------------------------
    def on_activate(self) -> None:
        """Make the selected old library the active calibration.

        The source file is never touched.  :class:`LibraryContext` opens it
        into the shared studio state, invalidates results derived from the
        previous library and broadcasts the change to every dependent page.
        """
        selected = self._selected()
        if len(selected) != 1:
            QMessageBox.information(
                self, '设为当前校准库',
                '请在表格中选中一个库（双击某一行也可以直接激活）。')
            return
        path = str(selected[0].get('path', ''))
        if not path:
            QMessageBox.warning(self, '设为当前校准库', '该行没有可用的库路径。')
            return
        context = getattr(self.state, 'library', None)
        if context is None:
            # Non-desktop state (e.g. a unit test with a bare AppState): open
            # the library directly and still report the action.
            try:
                handlers.studio_open_db(self.state, {'path': path})
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, '设为当前校准库',
                                    f'{type(exc).__name__}: {exc}')
                return
        elif not context.activate(path):
            QMessageBox.warning(
                self, '设为当前校准库',
                f'无法激活该库：\n{context.last_error or "未知原因"}')
            return
        name = Path(path).name
        self.status.setText(
            f'当前校准库：{name} · Smith 图 / 目标图案 / 阻抗调谐 / '
            f'验证报告 / 报告审计 已同步到该库')
        self.output.setPlainText(
            f'已设为当前校准库\n文件：{path}\n\n'
            '所有依赖标定数据的页面已切换到该库的频率网格与标定点；\n'
            '上一库的候选、调谐结果与验证结论已作废，请重新运行。')
        self.report(f'已设为当前校准库: {name}')
        self.activated.emit(path)
        self._mark_active()

    def _mark_active(self) -> None:
        """Mark the active library row, the way a workbench flags its loaded file."""
        context = getattr(self.state, 'library', None)
        active = str(getattr(context, 'path', '') or '')
        for row in range(self.table.rowCount()):
            cell = self.table.item(row, 0)
            if cell is None:
                continue
            path = str(cell.data(Qt.ItemDataRole.UserRole) or '')
            name = (self._libraries[row].get('name', '')
                    if 0 <= row < len(self._libraries) else cell.text())
            cell.setText(('● ' if path and path == active else '') + name)

    def on_library_changed(self, path: str) -> None:
        """Another face changed the active library: keep the marker in sync."""
        self._mark_active()
        if path:
            self.status.setText(f'当前校准库：{Path(path).name} · 各页面已同步')

    # -- actions ---------------------------------------------------------
    def on_scan(self) -> None:
        directory = self.directory.text().strip()
        if not directory:
            QMessageBox.information(self, '扫描', '请先选择目录')
            return
        body = {'directory': directory,
                'recursive': bool(self.recursive.currentData())}
        self.scan_button.setEnabled(False)
        self.run(lambda: handlers.studio_library_scan(self.state, body),
                 self._scanned, title='扫描失败', description='library scan')

    def _scanned(self, data: dict) -> None:
        self.scan_button.setEnabled(True)
        libraries = data.get('libraries', [])
        self._libraries = libraries
        self.table.setRowCount(len(libraries))
        for row, item in enumerate(libraries):
            freqs = item.get('frequency_ghz') or []
            freq_text = ('–'.join(f'{v:.4f}' for v in freqs)
                         if freqs else '—')
            gmax = item.get('gamma_max')
            vswr = item.get('gamma_max_vswr')
            values = (
                item.get('name', ''),
                item.get('cal_id', ''),
                f"{item.get('points_valid', 0)}/{item.get('points_total', 0)}",
                str(item.get('layers', 0)),
                freq_text,
                '—' if gmax is None else f'{gmax:.4f}',
                '—' if vswr is None else f'{vswr:.2f}',
            )
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setFont(numeric_font(9))
                cell.setData(Qt.ItemDataRole.UserRole, item.get('path', ''))
                self.table.setItem(row, column, cell)
        errors = data.get('errors') or []
        parts = [f'共 {len(libraries)} 个库']
        if errors:
            parts.append(f'{len(errors)} 个文件无法打开：'
                         + ', '.join(Path(e['path']).name for e in errors[:5]))
        self.status.setText(' · '.join(parts))
        self._mark_active()
        self.report(f'扫描完成: {len(libraries)} 个库')

    def on_compare(self) -> None:
        selected = self._selected()
        if len(selected) != 2:
            QMessageBox.information(self, '对比', '请在表格中用 Ctrl / Shift 选中两个库')
            return
        body = {'path_a': selected[0]['path'], 'path_b': selected[1]['path']}
        self.run(lambda: handlers.studio_library_compare(self.state, body),
                 self._compared, title='对比失败', description='library compare')

    def _compared(self, data: dict) -> None:
        lines = [
            f"A: {Path(data['path_a']).name}",
            f"B: {Path(data['path_b']).name}",
            f"频率网格: {'一致' if data.get('freq_match') else '不一致'}",
            f"有效点: {data.get('points_a')} → {data.get('points_b')}"
            f" (Δ {data.get('points_delta')})",
        ]
        ga = data.get('gamma_max_a')
        gb = data.get('gamma_max_b')
        lines.append(f"|Γ|max: {'—' if ga is None else f'{ga:.4f}'}"
                     f" → {'—' if gb is None else f'{gb:.4f}'}")
        if data.get('diffable'):
            lines.append(f"平均 |ΔΓ|: {data.get('mean_delta_gamma', 0):.6f}")
            lines.append(f"最大 |ΔΓ|: {data.get('max_delta_gamma', 0):.6f}")
        else:
            lines.append(f"逐点比较不可用: {data.get('diff_error', '')}")
        self.output.setPlainText('\n'.join(lines))
        self.report('对比完成')

    def on_export(self) -> None:
        selected = self._selected()
        if not selected:
            QMessageBox.information(self, '导出', '请先选中一个库')
            return
        dialog = _ExportDialog(self, selected[0]['path'])
        if not dialog.exec():
            return
        body = dict(dialog.body())
        self.run(lambda: handlers.studio_export(self.state, body),
                 self._exported, title='导出失败', description='library export')

    def _exported(self, data: dict) -> None:
        summary = {k: v for k, v in data.items() if k != 'path'}
        text = [f"导出完成: {data.get('path', '')}"]
        for key, value in summary.items():
            text.append(f'  {key}: {value}')
        self.output.setPlainText('\n'.join(text))
        self.report(f'已导出: {data.get("path", "")}')

    def on_derive(self) -> None:
        selected = self._selected()
        if not selected:
            QMessageBox.information(self, '派生', '请先选中一个库')
            return
        dialog = _DeriveDialog(self, selected[0]['path'])
        if not dialog.exec():
            return
        body = dict(dialog.body())
        if body.get('action') == 'reference_shift':
            # The reference shift is its own operation: it writes a library
            # that is referred to another plane, not a subset of the source.
            body.pop('action', None)
            body['source_path'] = selected[0]['path']
            self.run(lambda: handlers.studio_reference_shift(self.state, body),
                     self._derived, title='参考面平移失败',
                     description='reference shift')
            return
        self.run(lambda: handlers.studio_library_derive(self.state, body),
                 self._derived, title='派生失败', description='library derive')

    def _derived(self, data: dict) -> None:
        text = [f"派生完成: {data.get('path', '')}"]
        for key, value in data.items():
            if key == 'path':
                continue
            text.append(f'  {key}: {value}')
        self.output.setPlainText('\n'.join(text))
        self.report(f'已派生: {data.get("path", "")}')


class _ExportDialog(QDialog):
    """Pick the export format and the target file."""

    def __init__(self, parent, source: str):
        super().__init__(parent)
        self.source = source
        self.setWindowTitle('导出校准库')
        self.result_value = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.format_combo = QComboBox()
        for key, label in (('tef', 'TEF 表征文件 (频率 + 点位 + Γ)'),
                           ('csv', 'CSV 表格'),
                           ('s2p', '单点 Touchstone (.s2p)'),
                           ('s2p_library', '全库 Touchstone (逐点 .s2p)'),
                           ('hdf5', 'HDF5 副本')):
            self.format_combo.addItem(label, key)
        form.addRow('格式', self.format_combo)

        self.path_edit = QLineEdit(str(Path(source).with_suffix('.tef')))
        row = QHBoxLayout()
        browse = QPushButton('浏览…')
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        form.addRow('输出文件', row)

        self.point_index = QSpinBox()
        self.point_index.setRange(0, 100000)
        self.point_index.setValue(0)
        form.addRow('点位索引 (s2p)', self.point_index)

        self.valid_only = QComboBox()
        self.valid_only.addItem('仅导出有效点', True)
        self.valid_only.addItem('包含无效点', False)
        form.addRow('有效点', self.valid_only)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        ok = QPushButton('导出')
        ok.setProperty('accent', True)
        ok.clicked.connect(self._accept)
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.format_combo.currentIndexChanged.connect(self._sync_suffix)

    def _sync_suffix(self) -> None:
        fmt = self.format_combo.currentData()
        suffix = {'tef': '.tef', 'csv': '.csv', 's2p': '.s2p',
                  's2p_library': '_s2p', 'hdf5': '.h5'}[fmt]
        self.path_edit.setText(str(Path(self.source).with_suffix(suffix)))

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, '选择输出文件', self.path_edit.text())
        if path:
            self.path_edit.setText(path)

    def body(self) -> dict:
        return {'format': self.format_combo.currentData(),
                'path': self.path_edit.text().strip(),
                'point_index': self.point_index.value(),
                'valid_only': bool(self.valid_only.currentData())}

    def _accept(self) -> None:
        self.result_value = self.body()
        self.accept()


class _DeriveDialog(QDialog):
    """Pick the library derivation action."""

    def __init__(self, parent, source: str):
        super().__init__(parent)
        self.source = source
        self.setWindowTitle('从校准库派生新库')
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.action = QComboBox()
        for key, label in (('truncate_gamma', '按 |Γ| 截断（保留 ≤ 阈值）'),
                           ('truncate_travel', '按行程截断（X ≤ 上限）'),
                           ('reverse_probe', '反转某一轴探针（镜像）'),
                           ('split_frequency', '按频率切分（导出单一频点库）'),
                           ('apply_fixture', '夹具 embed / deembed (S2P)'),
                           ('reference_shift', '参考面平移 (T 矩阵)')):
            self.action.addItem(label, key)
        form.addRow('动作', self.action)

        # -- reference-plane shift (P2) --------------------------------
        self.shift_kind = QComboBox()
        self.shift_kind.addItem('线路长度', 'line')
        self.shift_kind.addItem('夹具 S2P', 'fixture')
        form.addRow('平移网络', self.shift_kind)

        self.shift_length = QDoubleSpinBox()
        self.shift_length.setRange(0.0, 1000.0)
        self.shift_length.setDecimals(4)
        self.shift_length.setValue(1.0)
        form.addRow('线路长度 (mm)', self.shift_length)

        self.shift_velocity = QDoubleSpinBox()
        self.shift_velocity.setRange(0.1, 1.0)
        self.shift_velocity.setDecimals(3)
        self.shift_velocity.setValue(0.66)
        form.addRow('速度系数', self.shift_velocity)

        self.shift_alpha = QDoubleSpinBox()
        self.shift_alpha.setRange(0.0, 100.0)
        self.shift_alpha.setDecimals(4)
        self.shift_alpha.setValue(0.0)
        form.addRow('损耗 (Np/m)', self.shift_alpha)

        self.shift_plane = QComboBox()
        for key, label in (('standard', 'Standard（独立 tuner）'),
                           ('on_wafer', 'On-Wafer（探针在片上）'),
                           ('pc_board', 'PC-board（电路板夹具）'),
                           ('connectorized', 'Connectorized（同轴连接器）'),
                           ('waveguide', 'Waveguide（波导法兰）'),
                           ('in_situ', 'In-Situ（在线测量路径）')):
            self.shift_plane.addItem(label, key)
        form.addRow('目标参考面', self.shift_plane)

        self.shift_direction = QComboBox()
        self.shift_direction.addItem('去掉网络（deembed）', 'deembed')
        self.shift_direction.addItem('加上网络（embed）', 'embed')
        form.addRow('方向', self.shift_direction)

        self.shift_fixture = QLineEdit('')
        shift_fixture_row = QHBoxLayout()
        shift_browse = QPushButton('浏览…')
        shift_browse.clicked.connect(self._browse_shift_fixture)
        shift_fixture_row.addWidget(self.shift_fixture, 1)
        shift_fixture_row.addWidget(shift_browse)
        form.addRow('夹具 S2P', shift_fixture_row)

        self.gmax = QDoubleSpinBox()
        self.gmax.setRange(0.0, 1.0)
        self.gmax.setDecimals(4)
        self.gmax.setSingleStep(0.05)
        self.gmax.setValue(0.9)
        form.addRow('|Γ| 阈值', self.gmax)

        self.x_max = QDoubleSpinBox()
        self.x_max.setRange(-1e6, 1e6)
        self.x_max.setDecimals(4)
        self.x_max.setValue(10.0)
        form.addRow('X 上限 (mm)', self.x_max)

        self.axis = QComboBox()
        for name in ('X', 'Z1', 'Z2', 'Z3'):
            self.axis.addItem(name, name)
        form.addRow('镜像轴', self.axis)

        self.freq_index = QSpinBox()
        self.freq_index.setRange(0, 1000)
        self.freq_index.setValue(0)
        form.addRow('频点索引', self.freq_index)

        self.fixture = QLineEdit('')
        fixture_row = QHBoxLayout()
        browse = QPushButton('浏览…')
        browse.clicked.connect(self._browse)
        fixture_row.addWidget(self.fixture, 1)
        fixture_row.addWidget(browse)
        form.addRow('夹具 S2P', fixture_row)

        self.direction = QComboBox()
        self.direction.addItem('embed（把夹具嵌入进去）', 'embed')
        self.direction.addItem('deembed（把夹具去掉）', 'deembed')
        form.addRow('方向', self.direction)

        self.path_edit = QLineEdit(str(Path(source).with_name(
            Path(source).stem + '_derived.h5')))
        out_row = QHBoxLayout()
        out_browse = QPushButton('浏览…')
        out_browse.clicked.connect(self._browse_out)
        out_row.addWidget(self.path_edit, 1)
        out_row.addWidget(out_browse)
        form.addRow('输出库', out_row)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        ok = QPushButton('派生')
        ok.setProperty('accent', True)
        ok.clicked.connect(self._accept)
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        buttons.addStretch()
        layout.addLayout(buttons)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, '选择夹具 S2P', str(paths.calibration_dir()),
            'Touchstone (*.s2p);;所有文件 (*)')
        if path:
            self.fixture.setText(path)

    def _browse_out(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, '选择输出库', self.path_edit.text(), 'HDF5 (*.h5)')
        if path:
            self.path_edit.setText(path)

    def _browse_shift_fixture(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, '选择夹具 S2P', str(paths.calibration_dir()),
            'Touchstone (*.s2p);;所有文件 (*)')
        if path:
            self.shift_fixture.setText(path)

    def body(self) -> dict:
        action = self.action.currentData()
        body = {'action': action, 'path': self.path_edit.text().strip()}
        if action == 'truncate_gamma':
            body['gmax'] = self.gmax.value()
        elif action == 'truncate_travel':
            body['x_max'] = self.x_max.value()
        elif action == 'reverse_probe':
            body['axis'] = self.axis.currentData()
        elif action == 'split_frequency':
            body['freq_index'] = self.freq_index.value()
        elif action == 'apply_fixture':
            body['fixture_s2p'] = self.fixture.text().strip()
            body['direction'] = self.direction.currentData()
        elif action == 'reference_shift':
            body['kind'] = self.shift_kind.currentData()
            body['length_mm'] = self.shift_length.value()
            body['velocity_factor'] = self.shift_velocity.value()
            body['alpha_np_per_m'] = self.shift_alpha.value()
            body['reference_plane'] = self.shift_plane.currentData()
            body['direction'] = self.shift_direction.currentData()
            body['fixture_s2p'] = self.shift_fixture.text().strip()
        return body

    def _accept(self) -> None:
        self.accept()

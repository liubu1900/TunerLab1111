"""The report audit page (P2).

An audit trail is only worth something if it can be read months later, so this
page shows the evidence rather than a summary of a summary:

* every calibration library in the folder, with its header, its point counts
  and the **SHA-256** of the file — so a library can be shown to be the same
  file that was used then;
* every validation record the libraries carry, with its verdict;
* the operation log's faults and what the software actually did.

The whole thing can be exported as one JSON file, which is the artefact an
audit trail is usually wanted for.
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
)

from tuner.ui_theme import numeric_font
from tuner.web import handlers

from . import paths
from .pages_base import _StudioPage, _page_title
from .ui import Card, hint_label, page_header

__all__ = ['StudioAuditPage']


class StudioAuditPage(_StudioPage):
    """Read-only view over the libraries, their validations and the log."""

    def __init__(self, app_state, parent=None):
        super().__init__(app_state, parent)
        self._report: dict = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 0, 22, 18)
        root.setSpacing(14)
        root.addWidget(page_header(
            '报告审计',
            '每个校准库的库头、点数与文件 SHA-256，库里带的验证记录，以及操作日志里的故障。'
            '全部只读，不会改动任何文件。'))

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.directory = QLabel(str(paths.calibration_dir()))
        self.directory.setObjectName('PageHint')
        self.directory.setWordWrap(True)
        self.directory.setTextInteractionFlags(
            self.directory.textInteractionFlags())
        browse = QPushButton('选择目录')
        browse.clicked.connect(self._browse)
        self.refresh_button = QPushButton('刷新审计')
        self.refresh_button.setProperty('accent', True)
        self.refresh_button.clicked.connect(self.on_refresh)
        self.export_button = QPushButton('导出审计 JSON…')
        self.export_button.clicked.connect(self.on_export)
        actions.addWidget(QLabel('库目录'))
        actions.addWidget(self.directory, 1)
        actions.addWidget(browse)
        actions.addWidget(self.refresh_button)
        actions.addWidget(self.export_button)
        root.addLayout(actions)

        summary = QLabel('尚未审计')
        summary.setWordWrap(True)
        summary.setFont(numeric_font(10, bold=True))
        self.summary = summary
        root.addWidget(summary)

        library_card = Card('校准库')
        self.library_table = QTableWidget(0, 7)
        self.library_table.setHorizontalHeaderLabels(
            ['库名', '校准 ID', '有效点/总点', '频率 (GHz)', '参考面', '模拟', 'SHA-256'])
        self.library_table.verticalHeader().setVisible(False)
        self.library_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.library_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.library_table.setMaximumHeight(210)
        library_card.body.addWidget(self.library_table)
        root.addWidget(library_card)

        validation_card = Card('验证记录')
        self.validation_table = QTableWidget(0, 6)
        self.validation_table.setHorizontalHeaderLabels(
            ['库名', '类型', '结论', '样本数', '最大 |ΔΓ|', '复位 dB'])
        self.validation_table.verticalHeader().setVisible(False)
        self.validation_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.validation_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.validation_table.setMaximumHeight(180)
        validation_card.body.addWidget(self.validation_table)
        root.addWidget(validation_card)

        log_card = Card('操作日志')
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(numeric_font(9))
        log_card.body.addWidget(self.log_view)
        root.addWidget(log_card, 1)

        self.on_refresh()

    # -- helpers ---------------------------------------------------------
    def _browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, '选择校准库目录', self.directory.text())
        if directory:
            self.directory.setText(directory)
            self.on_refresh()

    def on_library_changed(self, path: str) -> None:
        """Re-run the audit so its library/validation rows match the active file.

        The audit itself is read-only and directory-wide, but switching the
        active calibration can follow a derive / reference-shift operation that
        wrote a *new* library; refreshing here keeps the table, the SHA-256
        list and the validation records in step with what the other pages see.
        """
        self.summary.setText(
            f'当前校准库：{Path(path).name} · 正在刷新审计…' if path
            else '尚未审计')
        self.on_refresh()

    # -- actions ---------------------------------------------------------
    def on_refresh(self) -> None:
        body = {'directory': self.directory.text(),
                'operation_log': str(paths.log_dir() / 'operations.jsonl')}
        self.refresh_button.setEnabled(False)
        self.run(lambda: handlers.studio_audit(self.state, body),
                 self._audited, title='审计失败', description='audit')

    def _audited(self, report: dict) -> None:
        self._report = report
        self.refresh_button.setEnabled(True)

        libraries = report.get('libraries', [])
        self.library_table.setRowCount(len(libraries))
        for row, item in enumerate(libraries):
            freqs = item.get('frequency_ghz') or []
            values = (
                item.get('name', ''),
                item.get('cal_id', ''),
                f"{item.get('points_valid', 0)}/{item.get('points_total', 0)}",
                '–'.join(f'{value:.4f}' for value in freqs) if freqs else '—',
                item.get('reference_plane', '') or '—',
                '是' if item.get('simulated') else '否',
                (item.get('file_sha256', '') or '')[:16],
            )
            for column, text in enumerate(values):
                cell = QTableWidgetItem(str(text))
                cell.setFont(numeric_font(9))
                if column == 6:
                    cell.setToolTip(str(item.get('file_sha256', '')))
                self.library_table.setItem(row, column, cell)

        validations = report.get('validations', [])
        self.validation_table.setRowCount(len(validations))
        for row, item in enumerate(validations):
            passed = item.get('passed')
            verdict = '—' if passed is None else ('通过' if passed else '未通过')
            values = (
                item.get('library', ''),
                item.get('kind', '') or '—',
                verdict,
                '—' if item.get('n') is None else str(item['n']),
                '—' if item.get('max_delta_gamma') is None
                else f"{item['max_delta_gamma']:.6f}",
                '—' if item.get('reset_dB') is None
                else f"{item['reset_dB']:.1f}",
            )
            for column, text in enumerate(values):
                cell = QTableWidgetItem(str(text))
                cell.setFont(numeric_font(9))
                self.validation_table.setItem(row, column, cell)

        log = report.get('log', {})
        lines = [
            f"日志文件: {log.get('path', '')}",
            f"条目: {log.get('entries', 0)}"
            + (f"（{log.get('malformed', 0)} 行无法解析）"
               if log.get('malformed') else ''),
            f"时间范围: {log.get('first', '—')} → {log.get('last', '—')}",
        ]
        if log.get('by_category'):
            lines.append('按类别: ' + ' · '.join(
                f'{name} {count}'
                for name, count in sorted(log['by_category'].items())))
        if log.get('by_level'):
            lines.append('按级别: ' + ' · '.join(
                f'{name} {count}'
                for name, count in sorted(log['by_level'].items())))
        faults = log.get('faults') or []
        if faults:
            lines.append('')
            lines.append(f'故障与告警 ({len(faults)} 条):')
            for item in faults[-20:]:
                lines.append(f"  {item.get('time_text', '')}  "
                             f"{item.get('category', '')}/{item.get('action', '')}  "
                             f"{item.get('detail', '')[:80]}")
        self.log_view.setPlainText('\n'.join(lines))

        errors = report.get('library_errors') or []
        summary = (f"{report.get('library_count', 0)} 个校准库 · "
                   f"合计有效点 {report.get('total_points', 0)} · "
                   f"{report.get('validation_count', 0)} 条验证记录 · "
                   f"{report.get('fault_count', 0)} 条故障")
        if errors:
            summary += f" · {len(errors)} 个文件无法读取"
        self.summary.setText(summary)
        self.summary.setStyleSheet('color:#b03a2e;' if errors else '')
        self.report('审计完成')

    def on_export(self) -> None:
        if not self._report:
            QMessageBox.information(self, '导出审计', '还没有审计结果')
            return
        target, _ = QFileDialog.getSaveFileName(
            self, '导出审计报告', str(paths.default_report_path('audit')),
            'JSON (*.json)')
        if not target:
            return
        try:
            Path(target).write_text(
                json.dumps(self._report, indent=2, ensure_ascii=False, default=str),
                encoding='utf-8')
        except Exception as exc:  # noqa: BLE001
            self.warn('导出失败', str(exc))
            return
        self.report(f'审计报告已导出: {target}')

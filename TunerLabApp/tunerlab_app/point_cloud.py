"""Acquisition progress as a point cloud (P1-F11).

The widget draws the planned acquisition grid as a grid of cells: one column
per X layer, one row per Z1/Z2/Z3 combination.  A cell is filled when its
point has been written to the library, hollow when the point is still pending
and red when it failed.

The view is deliberately a *grid* rather than a scatter plot: the acquisition
order is layer by layer, so seeing which corner is still empty is the
operator's actual question while a run is in progress.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

__all__ = ['ProgressCloudWidget']


#: Cell colours, matching the shell's semantic palette.
_DONE = QColor('#42c5b7')
_PENDING = QColor('#2a3d52')
_FAILED = QColor('#b03a2e')
_EMPTY = QColor('#16222f')
_CAPTION = QColor('#9fb3c8')


class ProgressCloudWidget(QWidget):
    """Draws the acquisition grid, one column per X layer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layers: list[dict] = []
        self._failed: set[int] = set()
        self._caption = '尚未开始采集'
        self.setMinimumHeight(150)
        self.setMinimumWidth(320)

    # -- data ------------------------------------------------------------
    def set_cloud(self, cloud: dict | None) -> None:
        """Update from a :func:`tuner.progress_cloud.progress_cloud` result."""
        if not cloud:
            self._layers = []
            self._failed = set()
            self._caption = '尚未开始采集'
            self.update()
            return
        self._layers = list(cloud.get('layers') or [])
        self._failed = {int(item['index']) for item in cloud.get('failed') or []}
        done = int(cloud.get('done', 0))
        total = int(cloud.get('total', 0))
        self._caption = (f'{done} / {total} 点 · '
                         f"{cloud.get('complete_layers', 0)}/"
                         f"{cloud.get('layer_count', 0)} 层完成")
        self.update()

    # -- painting --------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor('#101b2b'))

        painter.setPen(QPen(_CAPTION, 1))
        painter.drawText(QRectF(8, 4, self.width() - 16, 18),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         self._caption)

        if not self._layers:
            painter.setPen(QPen(QColor('#5b6b7c'), 1))
            painter.drawText(self.rect(),
                             Qt.AlignmentFlag.AlignCenter,
                             '建库后这里显示采集点云')
            painter.end()
            return

        top = 28
        margin = 10
        width = max(1.0, self.width() - 2 * margin)
        height = max(1.0, self.height() - top - margin)
        columns = max(1, len(self._layers))
        rows = max((len(layer.get('rows') or []) for layer in self._layers),
                   default=1)
        rows = max(rows, 1)

        cell_width = width / columns
        cell_height = height / rows
        radius = max(2.0, min(cell_width, cell_height) * 0.28)

        for column, layer in enumerate(self._layers):
            indices = list(layer.get('indices') or [])
            done_rows = list(layer.get('done_rows') or [])
            for row in range(rows):
                index = indices[row] if row < len(indices) else None
                if index is None:
                    colour = _EMPTY
                elif index in self._failed:
                    colour = _FAILED
                elif row < len(done_rows) and done_rows[row]:
                    colour = _DONE
                else:
                    colour = _PENDING
                rect = QRectF(margin + column * cell_width + 1.5,
                              top + row * cell_height + 1.5,
                              max(1.0, cell_width - 3.0),
                              max(1.0, cell_height - 3.0))
                painter.setBrush(QBrush(colour))
                painter.setPen(QPen(QColor('#24374d'), 1))
                painter.drawRoundedRect(rect, radius, radius)

        painter.end()

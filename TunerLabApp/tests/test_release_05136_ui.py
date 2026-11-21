"""Desktop UI regressions for the 0.5.13.6 maintenance batch.

Pins down the operator-visible controls added for the six reported issues:
frequency pickers fed from the library, Smith zoom/pan/reset + click-to-select
wiring, per-axis 初始化 buttons and the one-click all-axis init button.
"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from tuner.web.state import AppState

from tunerlab_app.library_context import LibraryContext
from tunerlab_app.pages_pointops import GammaPlanDerivePage, VectorRepeatabilityPage


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def state():
    s = AppState()
    s.library = LibraryContext(s)
    return s


def _texts(widget):
    return [w.text() for w in widget.findChildren(QPushButton)]


def test_vector_page_has_frequency_picker_and_smith_controls(app, state):
    page = VectorRepeatabilityPage(state)
    assert page.freq_combo is not None
    assert page.freq_combo.count() == 0  # no library yet
    labels = _texts(page)
    for text in ('放大', '缩小', '复位', '缩放', '移动'):
        assert text in labels, f'missing Smith control: {text}'


def test_vector_page_frequency_is_required_before_run(app, state, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    page = VectorRepeatabilityPage(state)
    called = []
    monkeypatch.setattr(QMessageBox, 'information',
                        lambda *a, **k: called.append(a) or QMessageBox.Ok)
    page.on_run()
    # First guard is the frequency selection; the handler must not be reached.
    assert called and '频率' in called[-1][2]


def test_derive_page_has_frequency_picker(app, state):
    page = GammaPlanDerivePage(state)
    assert page.freq_combo is not None
    assert page.freq_combo.count() == 0


def test_smith_pick_closest_point_checks_the_row(app, state, monkeypatch):
    page = VectorRepeatabilityPage(state)
    page._points = [
        {'index': 3, 'position': [0, 0, 0], 'gamma_re': 0.2, 'gamma_im': 0.0,
         'gamma_mag': 0.2},
        {'index': 7, 'position': [1, 0, 0], 'gamma_re': -0.4, 'gamma_im': 0.1,
         'gamma_mag': 0.41},
    ]
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTableWidgetItem
    page.table.setRowCount(2)
    for row, p in enumerate(page._points):
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
        check.setCheckState(Qt.Unchecked)
        page.table.setItem(row, 0, check)
        page.table.setItem(row, 1, QTableWidgetItem(str(p['index'])))
    page._on_smith_picked(-0.39 + 0.11j)
    assert page._selected_indices() == [7]

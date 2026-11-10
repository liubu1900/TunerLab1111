"""Active-calibration linkage tests (standard calibration browser rule).

Choosing an old library must re-base the whole application on that file: the
Smith chart, the target-pattern editor, the tuner, the validation report and
the audit view all follow, and conclusions from the previous library are
discarded.  These tests run offscreen against two tiny synthetic libraries with
*different frequency bands and different point counts*, so a stale chart is
detectable rather than merely "not crashing".
"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from tuner.database import CalibrationDatabase
from tuner.models import Measurement, TunerConfig
from tuner.web import handlers
from tuner.web.state import AppState

from tunerlab_app.library_context import LibraryContext
from tunerlab_app.pages_audit import StudioAuditPage
from tunerlab_app.pages_base import _StudioPage
from tunerlab_app.pages_p1 import StudioLibraryBrowserPage
from tunerlab_app.pages_pattern import StudioPatternPage
from tunerlab_app.pages_studio import StudioDatabasePage
from tunerlab_app.pages_tune import (StudioSmithPage, StudioTuningPage,
                                     StudioValidationPage)


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def linked():
    state = AppState()
    state.library = LibraryContext(state)
    return state


def _make_library(tmp_path, freq, name, x=3, z=2, seed=0):
    """A single-frequency library of ``x * z**probes`` valid points."""
    rng = np.random.default_rng(seed)
    config = TunerConfig()
    xv = np.linspace(config.x_limits[0], config.x_limits[1], x)
    zv = np.linspace(config.z_limits[0], config.z_limits[1], z)
    positions = config.grid(xv, zv)
    freqs = np.array([freq], float)
    db = CalibrationDatabase(tmp_path / name)
    db.create(config, freqs, positions)
    for index, position in enumerate(positions):
        mag = 0.1 + 0.7 * (index / max(1, len(positions) - 1))
        angle = rng.uniform(-np.pi, np.pi)
        gamma = mag * np.exp(1j * angle)
        s = np.zeros((1, 2, 2), complex)
        s[:, 0, 0] = gamma
        s[:, 1, 0] = 0.05 * np.exp(1j * angle)
        s[:, 0, 1] = s[:, 1, 0]
        db.write_point(index, position.copy(), Measurement(freqs, s, True))
    return db


def _two_libraries(tmp_path):
    a = _make_library(tmp_path, 5e9, 'a.h5', x=3, seed=0)
    b = _make_library(tmp_path, 8e9, 'b.h5', x=4, seed=5)
    return a, b


# --------------------------------------------------------------------------
# The context itself
# --------------------------------------------------------------------------
def test_activate_broadcasts_and_invalidates(app, tmp_path, linked):
    a, b = _two_libraries(tmp_path)
    seen = []
    linked.library.changed.connect(seen.append)
    linked.studio['validation_report'] = {'kind': 'old', 'passed': True}
    linked.studio['interpolation_report'] = {'kind': 'old'}
    linked.studio['last_target'] = 0.3 + 0.2j

    assert linked.library.activate(a.path) is True
    assert linked.library.path == str(a.path)
    assert seen == [str(a.path)]
    assert linked.studio['validation_report'] is None
    assert linked.studio['interpolation_report'] is None
    assert linked.studio['last_target'] is None
    assert linked.studio['frequency_hz'] == 5e9

    assert linked.library.activate(b.path) is True
    assert seen[-1] == str(b.path)
    assert linked.studio['frequency_hz'] == 8e9


def test_failed_activation_keeps_previous_library(app, tmp_path, linked):
    a, _ = _two_libraries(tmp_path)
    assert linked.library.activate(a.path) is True
    errors = []
    linked.library.activation_failed.connect(errors.append)
    assert linked.library.activate(tmp_path / 'missing.h5') is False
    assert linked.library.path == str(a.path)
    assert errors and linked.library.last_error


def test_ensure_current_detects_backend_side_change(app, tmp_path, linked):
    """The web console / acquisition run can change the library behind Qt."""
    a, b = _two_libraries(tmp_path)
    linked.library.activate(a.path)
    seen = []
    linked.library.changed.connect(seen.append)
    assert linked.library.ensure_current() is False
    handlers.studio_open_db(linked, {'path': str(b.path)})
    assert linked.library.ensure_current() is True
    assert seen == [str(b.path)]
    assert linked.library.ensure_current() is False


def test_base_page_autosubscribes(app, linked):
    """A future page opts in just by implementing refresh()."""
    class Probe(_StudioPage):
        def __init__(self, state):
            super().__init__(state)
            self.calls = 0

        def refresh(self):
            self.calls += 1

    page = Probe(linked)
    linked.library.broadcast()
    assert page.calls == 1


# --------------------------------------------------------------------------
# Smith chart: the regression the operator reported
# --------------------------------------------------------------------------
def test_smith_page_rebases_frequency_and_points(app, tmp_path, linked):
    a, b = _two_libraries(tmp_path)
    linked.library.activate(a.path)
    page = StudioSmithPage(linked)
    assert abs(page.frequency.value() - 5.0) < 1e-6
    count_a = len(page.chart.points)
    assert count_a > 0
    page.verdict.setPlainText('旧库结论')  # Smith page uses a QTextEdit
    page.match_label.setText('旧库匹配')

    linked.library.activate(b.path)
    assert abs(page.frequency.value() - 8.0) < 1e-6
    assert len(page.chart.points) > count_a, 'chart still shows the old library'
    assert page.verdict.toPlainText() == ''
    assert page.match_label.text() == '—'
    assert page._picked is None


# --------------------------------------------------------------------------
# Tuning / validation: stale conclusions must not survive a switch
# --------------------------------------------------------------------------
def test_tuning_and_validation_pages_drop_stale_results(app, tmp_path, linked):
    a, b = _two_libraries(tmp_path)
    linked.library.activate(a.path)
    tuning = StudioTuningPage(linked)
    validation = StudioValidationPage(linked)

    tuning.table.setRowCount(2)
    tuning.result.setPlainText('旧库候选')
    tuning.deviation.verdict.setText('旧库对照')
    tuning.move_candidate_button.setEnabled(True)
    validation.verdict.setText('通过 · 旧库')
    validation.report_view.setPlainText('{"old": 1}')

    linked.library.activate(b.path)
    assert tuning.table.rowCount() == 0
    assert tuning.result.toPlainText() == ''
    assert tuning.deviation.verdict.text() == '—'
    assert not tuning.move_candidate_button.isEnabled()
    assert tuning.search_button.isEnabled()
    assert validation.verdict.text() == '—'
    assert validation.report_view.toPlainText() == ''
    assert abs(tuning.frequency.value() - 8.0) < 1e-6
    assert abs(validation.repeat_freq.value() - 8.0) < 1e-6


def test_pattern_page_reresolves_against_new_library(app, tmp_path, linked):
    a, b = _two_libraries(tmp_path)
    linked.library.activate(a.path)
    page = StudioPatternPage(linked)
    page._points = [0.2 + 0.1j, -0.3 + 0.2j]
    page.table.setRowCount(2)
    # The page normally resolves in a worker; make it synchronous so the
    # automatic re-resolution after a library switch is actually asserted.
    page.run = lambda function, on_done, **_kw: on_done(function())

    linked.library.activate(b.path)
    assert page.table.rowCount() == 2
    assert '可达' in page.resolve_summary.text()


def test_database_and_audit_pages_follow_broadcast(app, tmp_path, linked,
                                                   monkeypatch):
    a, b = _two_libraries(tmp_path)
    linked.library.activate(a.path)
    database_page = StudioDatabasePage(linked)
    audit_calls = []
    monkeypatch.setattr(StudioAuditPage, 'on_refresh',
                        lambda self: audit_calls.append(self.directory.text()))
    # Keep a Python reference: an unparented QWidget would be garbage
    # collected and its signal connection dropped before the broadcast.
    audit_page = StudioAuditPage(linked)
    before = len(audit_calls)
    assert audit_page is not None

    linked.library.activate(b.path)
    assert str(b.path) in database_page.summary.text()
    assert len(audit_calls) > before


# --------------------------------------------------------------------------
# Calibration Browser: selecting an old library actually activates it
# --------------------------------------------------------------------------
def test_browser_activation_marks_active_library(app, tmp_path, linked):
    a, b = _two_libraries(tmp_path)
    page = StudioLibraryBrowserPage(linked)
    page._scanned({'libraries': [
        {'name': a.path.name, 'cal_id': 'A', 'points_valid': 12,
         'points_total': 12, 'layers': 1, 'frequency_ghz': [5.0],
         'gamma_max': 0.8, 'gamma_max_vswr': 9.0, 'path': str(a.path)},
        {'name': b.path.name, 'cal_id': 'B', 'points_valid': 16,
         'points_total': 16, 'layers': 1, 'frequency_ghz': [8.0],
         'gamma_max': 0.8, 'gamma_max_vswr': 9.0, 'path': str(b.path)},
    ]})
    page.table.selectRow(0)
    page.on_activate()
    assert linked.library.path == str(a.path)
    assert page.table.item(0, 0).text().startswith('● ')

    page.table.selectRow(1)
    page.on_activate()
    assert linked.library.path == str(b.path)
    assert not page.table.item(0, 0).text().startswith('● ')
    assert page.table.item(1, 0).text().startswith('● ')

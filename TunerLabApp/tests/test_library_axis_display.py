"""Axis labels follow the loaded library; only the 3-axis model is offered.

A 3-axis library (X / Z1 / Z2) used to be labelled ``X / Z1 / Z2 / Z3`` by
the Smith page, which made a non-existent fourth probe look real.  The
communication page also offered a 4-axis model the machine on site does not
have.  Both are UI-level, reversible changes.
"""
from __future__ import annotations

import os
import pathlib
import tempfile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication

from tuner.settings import SettingsStore
from tuner.web import handlers
from tuner.web.state import AppState

from tunerlab_app.library_context import LibraryContext
from tunerlab_app.pages_comm import CommunicationPage
from tunerlab_app.pages_tune import StudioSmithPage, _axis_caption
from tunerlab_app.session import TunerSession


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(app):
    state = AppState()
    state.library = LibraryContext(state)
    return StudioSmithPage(state)


def _search_result(position):
    return {
        'candidates': [{
            'position': list(position),
            'gamma': {'re': 0.2, 'im': 0.6},
            'error': 0.001,
            'impedance': {'re': 12.0, 'im': 34.0},
        }],
        'tuned': None,
    }


def _move_only_report(position):
    return {
        'kind': 'smith_pick_move_only', 'measured': False,
        'validation_saved': False, 'frequency_hz': 29e9, 'tolerance': 0.08,
        'target_gamma_real': 0.2, 'target_gamma_imag': 0.6,
        'position': list(position), 'commanded_position': list(position),
        'position_error_max': 0.0, 'prediction_kind': 'calibrated point',
        'predicted_gamma_real': 0.2, 'predicted_gamma_imag': 0.6,
        'predicted_error': 0.001,
    }


def _verified_report(position):
    return {
        'frequency_hz': 29e9, 'tolerance': 0.08, 'vector_error': 0.01,
        'within_tolerance': True, 'target_gamma_real': 0.2,
        'target_gamma_imag': 0.6, 'measured_gamma_real': 0.21,
        'measured_gamma_imag': 0.61, 'position': list(position),
        'prediction_kind': 'calibrated point', 'position_error_max': 0.0,
        'impedance_delta_r': 0.1, 'impedance_delta_x': 0.1,
        'impedance_delta_r_percent': 1.0, 'impedance_delta_x_percent': 1.0,
        'vswr_delta': 0.1, 'simulated': False,
    }


# --------------------------------------------------------------- axis names
def test_axis_caption_is_derived_from_the_axis_count():
    assert _axis_caption(3) == 'X / Z1 / Z2'
    assert _axis_caption(4) == 'X / Z1 / Z2 / Z3'
    assert _axis_caption(3, '|') == 'X|Z1|Z2'
    assert 'Z3' not in _axis_caption(3)


def test_searched_position_of_a_three_axis_library_shows_three_axes(page):
    page._searched(_search_result([9.1084, 0.7462, -0.0066]))
    text = page.match_label.text()
    assert 'X / Z1 / Z2' in text
    assert 'Z3' not in text
    assert '9.1084 | 0.7462 | -0.0066' in text


def test_a_four_axis_library_still_shows_all_four_axes(page):
    page._searched(_search_result([9.1, 0.7, 1.2, 2.3]))
    assert 'X / Z1 / Z2 / Z3' in page.match_label.text()


def test_move_only_and_verified_cards_label_the_real_axes(page):
    point = [9.1084, 0.7462, -0.0066]
    page._moved_only(_move_only_report(point))
    move_text = page.verdict.toPlainText()
    assert 'X|Z1|Z2' in move_text
    assert 'Z3' not in move_text

    page._verified(_verified_report(point))
    verify_text = page.verdict.toPlainText()
    assert 'X|Z1|Z2' in verify_text
    assert 'Z3' not in verify_text


# --------------------------------------------------------------- tuner model
def test_communication_page_offers_only_the_three_axis_model(app):
    store = SettingsStore(pathlib.Path(tempfile.mkdtemp()) / 'settings.json')
    session = TunerSession(store)
    page = CommunicationPage(session)
    assert page.model_combo.count() == 1
    assert page.model_combo.findData(4) < 0
    assert page.model_combo.itemData(0) == 3
    assert page.model_combo.currentData() == 3


def test_a_legacy_four_axis_setting_is_normalised_to_three(app):
    store = SettingsStore(pathlib.Path(tempfile.mkdtemp()) / 'settings.json')
    session = TunerSession(store)
    assert session.settings.axis_count == 4
    CommunicationPage(session)
    assert session.settings.axis_count == 3
    assert store.load().axis_count == 3


def test_web_console_hides_the_four_axis_option():
    static = pathlib.Path(handlers.__file__).parent / 'static'
    html = (static / 'index.html').read_text(encoding='utf-8')
    js = (static / 'app.js').read_text(encoding='utf-8')
    assert '<option value="3"' in html
    assert '<option value="4"' not in html
    # A legacy settings file reporting 4 axes must not leave the select blank.
    assert 'model.options' in js

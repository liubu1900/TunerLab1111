"""Smith-chart「仅移动」button (0.5.13 + move-only change).

The move and the verified action share one search → move path.  These tests
pin down the two things an operator can see: the button sits immediately to
the left of 「移动并实测验证」, and a move-only result is never dressed up as
a measured, pass/fail verdict.
"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication

from tuner.web.state import AppState

from tunerlab_app import pages_tune
from tunerlab_app.library_context import LibraryContext
from tunerlab_app.pages_tune import StudioSmithPage


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(app):
    state = AppState()
    state.library = LibraryContext(state)
    return StudioSmithPage(state)


def _button_texts(page):
    """Labels of the widgets in the horizontal row holding the pick buttons."""
    def walk(layout):
        widgets = [layout.itemAt(i).widget() for i in range(layout.count())]
        widgets = [widget for widget in widgets if widget is not None]
        if page.move_only_button in widgets:
            return [widget.text() for widget in widgets]
        for i in range(layout.count()):
            child = layout.itemAt(i).layout()
            if child is not None:
                found = walk(child)
                if found:
                    return found
        return []
    return walk(page.layout())


def _move_only_report():
    return {
        'kind': 'smith_pick_move_only', 'measured': False,
        'validation_saved': False, 'frequency_hz': 5e9, 'tolerance': 0.05,
        'target_gamma_real': 0.3, 'target_gamma_imag': 0.1,
        'position': [3.75, 0.0, 1.0],
        'commanded_position': [3.75, 0.0, 1.0],
        'position_error_max': 0.0,
        'prediction_kind': 'calibrated point',
        'predicted_gamma_real': 0.31, 'predicted_gamma_imag': 0.11,
        'predicted_error': 0.014,
    }


def test_move_only_button_is_left_of_verify_and_follows_the_pick(page):
    labels = _button_texts(page)
    assert labels.index('仅移动') + 1 == labels.index('移动并实测验证')
    assert not page.move_only_button.isEnabled()
    assert not page.move_button.isEnabled()
    page.on_pick(0.3 + 0.1j)
    assert page.move_only_button.isEnabled()
    assert page.move_button.isEnabled()
    page.on_pick(None)
    assert not page.move_only_button.isEnabled()


def test_buttons_dispatch_to_their_own_backend(page):
    calls = []
    page._run_move = lambda handler, done, title, desc: calls.append(
        (handler.__name__, desc))
    page._picked = 0.2 + 0.1j
    page.on_move_only()
    page.on_move_verify()
    assert [name for name, _ in calls] == [
        'studio_move_to_gamma_only', 'studio_move_to_gamma']
    assert calls[0][1] == 'smith pick move only'
    assert calls[1][1] == 'smith pick move verify'


def test_move_disables_both_buttons_and_restores_them(monkeypatch, page):
    captured = {}

    def fake_background(function, on_done, on_error, description):
        captured.update(on_done=on_done, on_error=on_error,
                        description=description)

    monkeypatch.setattr(pages_tune, 'run_in_background', fake_background)
    errors = []
    page.fail = lambda exc, title='操作失败': errors.append((str(exc), title))
    page._picked = 0.2 + 0.1j

    page.on_move_only()
    assert captured['description'] == 'smith pick move only'
    assert not page.move_only_button.isEnabled()
    assert not page.move_button.isEnabled()
    assert not page.search_button.isEnabled()

    captured['on_error'](RuntimeError('boom'))
    assert errors == [('boom', '移动失败')]
    assert page.move_only_button.isEnabled()
    assert page.move_button.isEnabled()

    page.on_move_only()
    captured['on_done'](_move_only_report())
    assert page.move_only_button.isEnabled()
    text = page.verdict.toPlainText()
    assert text.startswith('仅移动（未实测')
    assert '未写验证报告' in text
    assert '3.7500 | 0.0000 | 1.0000' in text
    assert '通过' not in text
    assert page.verdict.styleSheet() == ''

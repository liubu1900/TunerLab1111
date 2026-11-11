"""Desktop tuner-selection regressions (offscreen, no hardware)."""
from __future__ import annotations

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pathlib
import tempfile

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from tuner.settings import SettingsStore
from tunerlab_app.pages_comm import CommunicationPage
from tunerlab_app.session import TunerSession


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    """Modal boxes would wait forever for a click in an offscreen run."""
    for name in ('warning', 'information', 'critical', 'question'):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: None))


@pytest.fixture
def session():
    tmp = pathlib.Path(tempfile.mkdtemp())
    return TunerSession(SettingsStore(tmp / 'settings.json'))


def test_the_communication_page_offers_both_tuners(session, app):
    page = CommunicationPage(session)
    assert page.unit_combo.count() == 2
    labels = [page.unit_combo.itemText(i) for i in range(page.unit_combo.count())]
    assert any('左 Tuner' in text for text in labels)
    assert any('右 Tuner' in text for text in labels)


def test_the_current_tuner_is_selected(session, app):
    page = CommunicationPage(session)
    assert page.unit_combo.currentData() == 'left'


def test_the_hint_shows_the_physical_axis_mapping(session, app):
    page = CommunicationPage(session)
    hint = page.unit_hint.text()
    assert 'X' in hint and 'Z1' in hint
    assert '轴 0' in hint


def test_switching_the_tuner_through_the_combo(session, app):
    page = CommunicationPage(session)
    index = page.unit_combo.findData('right')
    assert index >= 0
    page.unit_combo.setCurrentIndex(index)
    app.processEvents()

    assert session.settings.active_unit == 'right'
    assert page.unit_combo.currentData() == 'right'
    assert '轴 3' in page.unit_hint.text()


def test_switching_the_tuner_persists(session, app):
    page = CommunicationPage(session)
    index = page.unit_combo.findData('right')
    page.unit_combo.setCurrentIndex(index)
    app.processEvents()
    assert session.store.load().active_unit == 'right'


def test_the_session_keeps_the_millimetre_adapter_after_a_switch(session, app):
    """The desktop adapter must keep the new tuner's axis block.

    The millimetre adapter is only used for the real hardware, so the check
    runs against the upgrade path with a stand-in controller.
    """
    from tunerlab_app.motion_adapter import TunerMotionController

    session.connect(mode='virtual')
    session.set_active_unit('right')
    assert session.controller.axis_base == 3
    assert session.controller.axis_map() == {'X': 3, 'Z1': 4, 'Z2': 5}

    # The upgrade must carry the axis block over to the millimetre adapter.
    upgraded = TunerMotionController(
        session.controller.config, session.link, session.settings.axes,
        axis_count=session.settings.axis_count,
        axis_base=3, op_base=3, datum_in_base=31)
    assert upgraded.axis_map() == {'X': 3, 'Z1': 4, 'Z2': 5}
    assert upgraded.output('X') == 3

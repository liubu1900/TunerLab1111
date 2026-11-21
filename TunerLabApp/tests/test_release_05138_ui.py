"""Desktop UI regressions for the 0.5.13.8 maintenance batch.

Pins: one-click init confirmation fires once; the calibration wizard prefills
its VNA connection from 矢网控制 settings; "打开所在文件夹" reveals the active
library's own directory instead of the default data folder.
"""
import os
import tempfile
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication

from tuner.settings import SettingsStore
from tuner.web.state import AppState

from tunerlab_app.library_context import LibraryContext
from tunerlab_app.pages_motion import SafeAxisMotionPage
from tunerlab_app.pages_studio import StudioDatabasePage, StudioSetupPage
from tunerlab_app.session import TunerSession


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_one_click_init_confirmation_fires_once(app, monkeypatch):
    page = SafeAxisMotionPage(TunerSession(SettingsStore()))
    btn = page.init_all_button
    # Qt counts direct + receiver-listed connections; there must be exactly one
    # slot for the confirmation, otherwise one click shows two dialogs.
    assert btn.receivers('2clicked()') == 1, (
        '一键初始化 clicked must be connected once (base page only)')


def test_init_button_tooltips_have_no_process_explanation(app):
    page = SafeAxisMotionPage(TunerSession(SettingsStore()))
    text = (page.init_all_button.toolTip() or '')
    assert 'MPOS' not in text and '回零' not in text
    for entry in page.axis_panels.values():
        tip = entry.get('init_btn').toolTip() or ''
        assert 'MPOS' not in tip and '回零' not in tip


def test_studio_wizard_prefills_vna_from_live_config(app):
    state = AppState()
    state.library = LibraryContext(state)
    state.vna['config'].update(host='10.1.2.3', port=4321, vendor_key='ceyear',
                               interface='gpib', resource='GPIB0::8::INSTR',
                               timeout_ms=9999)
    page = StudioSetupPage(state)
    assert page.vna_ip.text() == '10.1.2.3'
    assert page.vna_resource.text() == 'GPIB0::8::INSTR'
    assert page.vna_vendor.currentData() == 'ceyear'
    assert page.vna_interface.currentData() == 'gpib'
    assert page.vna_port_value == 4321
    assert page.vna_timeout_value == 9999
    body = page._init_body()
    assert body['vna_ip'] == '10.1.2.3'
    assert body['vna_port'] == 4321
    assert body['vna_timeout_ms'] == 9999
    assert 'vna_dialect' in body  # carried through to the bridge


def test_reveal_button_points_at_active_library_folder(app, monkeypatch):
    state = AppState()
    state.library = LibraryContext(state)
    page = StudioDatabasePage(state)
    opened = []
    monkeypatch.setattr('os.startfile', lambda p: opened.append(p), raising=False)

    # Case 1: an active library outside the default data dir.
    lib = Path(tempfile.mkdtemp()) / 'my_cal.h5'
    lib.touch()
    page.state.studio['data'] = {'valid': [], 'frequencies': [5e9, 5e9], 'metadata': {}}
    page.state.studio['database'] = type('DB', (), {'path': lib})()
    page.on_reveal()
    assert opened and Path(opened[-1]) == lib.parent

    # Case 2: no active library -> falls back to the default calibration dir.
    page.state.studio['data'] = None
    opened.clear()
    page.on_reveal()
    assert opened and Path(opened[-1]).name == 'calibrations'

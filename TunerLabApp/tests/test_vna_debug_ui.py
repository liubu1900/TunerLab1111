"""Desktop SCPI debug toolbar regressions (offscreen, no instruments)."""
from __future__ import annotations

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import threading
from types import SimpleNamespace
from unittest import mock

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from tunerlab_app import shell, studio_bridge
from tunerlab_app.pages_vna_debug import VnaDebugPage


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def _state(connected=False):
    cfg = {
        'host': '192.168.2.111', 'port': 5025, 'timeout_ms': 10000,
        'vendor_key': 'auto', 'interface': 'tcpip',
        'resource': 'GPIB0::17::INSTR', 'dialect': {},
        'start_hz': 1e9, 'stop_hz': 20e9, 'points': 401,
        'power_dbm': -10.0, 'ifbw_hz': 1000.0,
    }
    state = SimpleNamespace(
        vna_lock=threading.RLock(),
        vna={'controller': None, 'connected': connected, 'idn': None,
             'vendor': None, 'last_error': None, 'config': cfg})
    state.vna_status = lambda: {
        'connected': state.vna['connected'], 'idn': state.vna['idn'],
        'vendor': state.vna['vendor'], 'last_error': state.vna['last_error'],
        'config': state.vna['config'], 'live_config': None}
    return state


def _probe_result():
    return {
        'target': '192.168.2.111:5025', 'interface': 'tcpip',
        'idn': 'Agilent Technologies,N4225A,SN1,A.01.00',
        'idn_fields': {'manufacturer': 'Agilent Technologies',
                       'model': 'N4225A', 'serial': 'SN1',
                       'firmware': 'A.01.00'},
        'capabilities': {'cls': True, 'error_queue': True,
                         'readback': True, 'notes': []},
        'define': [{'cmd': ":CALC1:PAR:DEF:EXT 'TL_probe_S11','S11'",
                    'note': 'PNA', 'error': '+0,"No error"',
                    'verdict': 'OK', 'value': ''},
                   {'cmd': ":CALC1:PAR:DEF 'TL_probe_S11','S11'",
                    'note': 'ENA', 'error': '-113,"Undefined header"',
                    'verdict': 'REJECT', 'value': ''}],
        'other': [{'cmd': ':CALC1:DATA? SDATA', 'note': '取数',
                   'error': '+0,"No error"', 'verdict': 'OK',
                   'value': '0.1,0.02'}],
        'accepted_define': [":CALC1:PAR:DEF:EXT 'TL_probe_S11','S11'"],
        'suggested_dialect': {'define_cmd': ':CALC1:PAR:DEF:EXT',
                              'fetch_mnemonic': 'SDATA'},
        'summary': ['仪器：Agilent Technologies N4225A，固件 A.01.00',
                    '可用的测量定义命令：DEF:EXT'],
        'report': ('1) 仪器身份\n*IDN? = Agilent Technologies,N4225A,SN1,A.01.00\n'
                   '2) 测量定义命令\n OK  :CALC1:PAR:DEF:EXT'),
        'aborted': False,
    }


def _sync_background(function, on_done=None, on_error=None, description=''):
    """Run the worker inline so the page's full click path can be asserted."""
    try:
        result = function()
    except BaseException as exc:  # noqa: BLE001
        if on_error:
            on_error(exc)
    else:
        if on_done:
            on_done(result)
    return SimpleNamespace()


# --------------------------------------------------------------------------- #
# Construction and navigation
# --------------------------------------------------------------------------- #
def test_debug_page_builds_all_toolbar_controls(app):
    page = VnaDebugPage(_state())
    try:
        for name in ('detect_button', 'config_button', 'probe_button',
                     'disconnect_probe_button', 'stop_button', 'apply_button',
                     'copy_button', 'export_button', 'clear_button',
                     'interface_combo', 'target_edit', 'port_spin',
                     'timeout_spin', 'table', 'report_view'):
            assert hasattr(page, name), name
        assert page.probe_button.isEnabled()
        assert not page.stop_button.isEnabled()
        assert not page.apply_button.isEnabled()
        assert page.tabs.count() == 2
    finally:
        page.close()


def test_debug_page_is_registered_in_navigation(app):
    keys = [key for _, entries in shell.NAVIGATION for key, _, _ in entries]
    assert 'vna-debug' in keys
    assert 'vna-debug' in shell._SCROLLED_PAGES


def test_debug_page_switches_tcpip_and_gpib_targets(app):
    page = VnaDebugPage(_state())
    try:
        assert page.interface_combo.currentData() == 'tcpip'
        assert page.target_edit.text() == '192.168.2.111'
        assert not page.port_spin.isHidden()

        page.interface_combo.setCurrentIndex(
            page.interface_combo.findData('gpib'))
        assert page.port_spin.isHidden()
        assert 'GPIB0' in page.target_edit.placeholderText()
        body = page._body()
        assert body['interface'] == 'gpib'
        assert body['resource'] == 'GPIB0::17::INSTR'
    finally:
        page.close()


def test_debug_page_loads_gpib_config_from_state(app):
    state = _state()
    state.vna['config']['interface'] = 'gpib'
    state.vna['config']['resource'] = 'GPIB0::16::INSTR'
    state.vna['config']['dialect'] = {'define_cmd': ':CALC1:PAR:DEF:EXT'}
    page = VnaDebugPage(state)
    try:
        assert page.interface_combo.currentData() == 'gpib'
        assert page.target_edit.text() == 'GPIB0::16::INSTR'
        assert page._body()['resource'] == 'GPIB0::16::INSTR'
        assert ':CALC1:PAR:DEF:EXT' in page.dialect_label.text()
    finally:
        page.close()


# --------------------------------------------------------------------------- #
# Rendering and actions
# --------------------------------------------------------------------------- #
def test_probe_result_is_rendered_into_table_and_report(app):
    page = VnaDebugPage(_state())
    try:
        page._finished(_probe_result())
        assert page.table.rowCount() == 3
        assert page.idn_label.text().startswith('Agilent Technologies')
        assert 'N4225A' in page.model_label.text()
        assert 'DEF:EXT' in page.report_view.toPlainText()
        assert page.apply_button.isEnabled()
        assert page.copy_button.isEnabled()
        assert '测量定义' in page.table.item(0, 0).text()
    finally:
        page.close()


def test_click_probe_runs_in_background_and_renders(app):
    page = VnaDebugPage(_state())
    try:
        result = _probe_result()
        with mock.patch('tunerlab_app.pages_vna_debug.run_in_background',
                        side_effect=_sync_background), \
             mock.patch.object(page, 'report'), \
             mock.patch('tunerlab_app.pages_vna_debug.handlers.vna_probe',
                        return_value=result) as probe_call:
            page.on_probe(False)
        assert probe_call.called
        kwargs = probe_call.call_args.kwargs
        assert callable(kwargs['should_stop']) and callable(kwargs['on_step'])
        assert page.table.rowCount() == 3
        assert page.result is result
        assert not page._running
    finally:
        page.close()


def test_disconnect_and_probe_closes_the_live_session(app):
    page = VnaDebugPage(_state(connected=True))
    try:
        with mock.patch('tunerlab_app.pages_vna_debug.run_in_background',
                        side_effect=_sync_background), \
             mock.patch.object(page, 'report'), \
             mock.patch('tunerlab_app.pages_vna_debug.handlers.vna_disconnect') as disconnect, \
             mock.patch('tunerlab_app.pages_vna_debug.handlers.vna_probe',
                        return_value=_probe_result()):
            page.on_probe(True)
        assert disconnect.called
        assert page.table.rowCount() == 3
    finally:
        page.close()


def test_apply_dialect_writes_to_state_config(app):
    state = _state()
    page = VnaDebugPage(state)
    try:
        page._finished(_probe_result())
        with mock.patch('tunerlab_app.pages_vna_debug.QMessageBox'), \
             mock.patch.object(page, 'report'):
            page.on_apply_dialect()
        assert state.vna['config']['dialect'] == {
            'define_cmd': ':CALC1:PAR:DEF:EXT', 'fetch_mnemonic': 'SDATA'}
        assert 'DEF:EXT' in page.dialect_label.text()
    finally:
        page.close()


def test_stop_sets_the_cancel_event(app):
    page = VnaDebugPage(_state())
    try:
        page._running = True
        page._set_buttons()
        page.on_stop()
        assert page._stop.is_set()
    finally:
        page.close()


def test_clear_resets_all_views(app):
    page = VnaDebugPage(_state())
    try:
        page._finished(_probe_result())
        page.on_clear()
        assert page.result is None
        assert page.table.rowCount() == 0
        assert page.report_view.toPlainText() == '尚未运行探测。'
        assert not page.apply_button.isEnabled()
    finally:
        page.close()


def test_copy_report_uses_clipboard(app):
    page = VnaDebugPage(_state())
    try:
        page._finished(_probe_result())
        with mock.patch.object(page, 'report'):
            page.on_copy_report()
        assert 'N4225A' in QApplication.clipboard().text()
    finally:
        page.close()


def test_studio_wizard_uses_pinned_dialect_from_state(app):
    state = SimpleNamespace(vna={'config': {
        'dialect': {'define_cmd': ':CALC1:PAR:DEF:EXT'}}})
    body = {'vna_source': 'real', 'vna_vendor': 'auto',
            'vna_ip': '192.168.2.111', 'vna_interface': 'gpib',
            'vna_resource': 'GPIB0::17::INSTR',
            'vna_start_hz': 1e9, 'vna_stop_hz': 2e9, 'vna_points': 11}
    with mock.patch.object(studio_bridge, 'create_vna',
                           return_value=mock.Mock()) as factory:
        studio_bridge._build_vna(state, body, None, None)
    assert factory.call_args.kwargs['dialect'] == {
        'define_cmd': ':CALC1:PAR:DEF:EXT'}

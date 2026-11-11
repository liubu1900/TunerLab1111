"""Desktop UI regressions for GPIB / VISA interface selection.

Runs offscreen.  The pages are constructed with a lightweight state stub so no
controller or VISA runtime is touched.
"""
from __future__ import annotations

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import threading
from types import SimpleNamespace
from unittest import mock

import pytest
from PySide6.QtWidgets import QApplication

from tunerlab_app import studio_bridge
from tunerlab_app.pages_studio import StudioSetupPage
from tunerlab_app.pages_vna import VnaPage


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def _vna_state():
    cfg = {
        'host': '192.168.2.111', 'port': 5025, 'timeout_ms': 10000,
        'vendor_key': 'auto', 'interface': 'tcpip',
        'resource': 'GPIB0::17::INSTR',
        'start_hz': 1e9, 'stop_hz': 20e9, 'points': 401,
        'power_dbm': -10.0, 'ifbw_hz': 1000.0,
    }
    state = SimpleNamespace(
        vna_lock=threading.RLock(),
        vna={'controller': None, 'connected': False, 'idn': None,
             'vendor': None, 'last_error': None, 'config': cfg},
    )
    state.vna_status = lambda: {
        'connected': state.vna['connected'], 'idn': state.vna['idn'],
        'vendor': state.vna['vendor'], 'last_error': state.vna['last_error'],
        'config': state.vna['config'], 'live_config': None,
    }
    return state


def _close_page(page):
    page.timer.stop()
    page.close()


def test_vna_page_switches_between_tcpip_and_gpib(app):
    page = VnaPage(_vna_state())
    try:
        assert page.interface_combo.currentData() == 'tcpip'
        assert not page.host_edit.isHidden()
        assert page.resource_field.isHidden()

        page.interface_combo.setCurrentIndex(
            page.interface_combo.findData('gpib'))
        assert page.host_edit.isHidden()
        assert page.port_spin.isHidden()
        assert not page.resource_field.isHidden()

        page.resource_edit.setText('17')
        body = page._body()
        assert body['interface'] == 'gpib'
        assert body['resource'] == '17'
        assert page.connection_info()[1] == '17'

        page.interface_combo.setCurrentIndex(
            page.interface_combo.findData('tcpip'))
        assert not page.host_edit.isHidden()
        assert page.resource_field.isHidden()
        assert page._body()['interface'] == 'tcpip'
    finally:
        _close_page(page)


def test_vna_page_restores_gpib_config_from_state(app):
    state = _vna_state()
    state.vna['config']['interface'] = 'gpib'
    state.vna['config']['resource'] = 'GPIB0::16::INSTR'
    page = VnaPage(state)
    try:
        assert page.interface_combo.currentData() == 'gpib'
        assert page.resource_edit.text() == 'GPIB0::16::INSTR'
        assert page.resource_field.isHidden() is False
        assert page._body()['resource'] == 'GPIB0::16::INSTR'
    finally:
        _close_page(page)


def _stub_setup_page():
    """Only the widgets _init_body() reads; no full wizard construction."""
    page = StudioSetupPage.__new__(StudioSetupPage)
    page.state = SimpleNamespace(hw=SimpleNamespace(probe_count=2))
    page.motion_combo = SimpleNamespace(currentData=lambda: 'mock')
    page.probes_combo = SimpleNamespace(currentData=lambda: 2)
    page.freq_mode = SimpleNamespace(currentData=lambda: 'fo_list')
    page.fo_ghz = SimpleNamespace(value=lambda: 10.0)
    for name in ('x_min', 'x_max', 'z_min', 'z_max', 'safe_z',
                 'vna_start', 'vna_stop', 'vna_points'):
        setattr(page, name, SimpleNamespace(value=lambda: 1.0))
    page.vna_combo = SimpleNamespace(currentData=lambda: 'real')
    page.vna_vendor = SimpleNamespace(currentData=lambda: 'rsp')
    page.vna_interface = SimpleNamespace(currentData=lambda: 'gpib')
    page.vna_resource = SimpleNamespace(text=lambda: 'GPIB0::16::INSTR')
    page.vna_ip = SimpleNamespace(text=lambda: '192.168.2.111')
    return page


def test_studio_init_body_carries_gpib_fields(app):
    body = _stub_setup_page()._init_body()
    assert body['vna_source'] == 'real'
    assert body['vna_interface'] == 'gpib'
    assert body['vna_resource'] == 'GPIB0::16::INSTR'
    assert body['vna_ip'] == '192.168.2.111'


class _FakeController:
    def connect(self):
        return 'Agilent Technologies,E5071C,SN123,A.01'

    def set_sweep(self, *args):
        pass


def test_studio_bridge_dispatches_gpib(app):
    body = {
        'vna_source': 'real', 'vna_vendor': 'rsp',
        'vna_interface': 'gpib', 'vna_resource': 'GPIB0::16::INSTR',
        'vna_ip': '192.168.2.111',
        'vna_start_hz': 1e9, 'vna_stop_hz': 2e9, 'vna_points': 11,
    }
    with mock.patch.object(studio_bridge, 'create_vna',
                           return_value=_FakeController()) as factory:
        vna, source = studio_bridge._build_vna(None, body, None, None)
    assert source == 'real'
    assert vna is not None
    assert factory.call_args.kwargs['interface'] == 'gpib'
    assert factory.call_args.kwargs['resource'] == 'GPIB0::16::INSTR'


def test_studio_bridge_defaults_to_tcpip(app):
    body = {
        'vna_source': 'real', 'vna_vendor': 'auto', 'vna_ip': '192.168.2.111',
        'vna_start_hz': 1e9, 'vna_stop_hz': 2e9, 'vna_points': 11,
    }
    with mock.patch.object(studio_bridge, 'create_vna',
                           return_value=_FakeController()) as factory:
        studio_bridge._build_vna(None, body, None, None)
    assert factory.call_args.kwargs['interface'] == 'tcpip'

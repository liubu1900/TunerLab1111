"""0.5.13.3: the VNA HTTP layer must never change instrument settings unless
the operator explicitly asks for it.

Connect = communication only.  单次扫描 = trigger + read.  下发扫描参数 is the
single writer.  从矢网读取当前参数 (``/api/vna/read_config``) is read-only.
"""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from tuner.models import Measurement
from tuner.web import handlers


CONFIG = {
    'host': '192.168.2.111', 'port': 5025, 'timeout_ms': 10000,
    'vendor_key': 'auto', 'interface': 'tcpip',
    'resource': 'GPIB0::17::INSTR',
    'start_hz': 1e9, 'stop_hz': 20e9, 'points': 401,
    'power_dbm': -10.0, 'ifbw_hz': 1000.0,
}


class FakeController:
    """Records every setter, answers read-back and returns a fake measurement."""

    vendor_key = 'keysight_ena'
    last_error = None
    connected = True

    def __init__(self, live=None, frequencies=None):
        self.calls: list[tuple] = []
        self.live = dict(live or {
            'start_hz': 3e9, 'stop_hz': 9e9, 'points': 101,
            'power_dbm': -7.0, 'ifbw_hz': 5000.0,
            'read': ['start_hz', 'stop_hz', 'points', 'power_dbm', 'ifbw_hz'],
            'fallback': [], 'source': 'instrument',
        })
        self.frequencies = (np.asarray(frequencies, float)
                            if frequencies is not None
                            else np.linspace(3e9, 9e9, 101))

    def connect(self):
        self.calls.append(('connect',))
        return 'Keysight Technologies,E5071C,SN123,A.01'

    def get_config(self):
        return dict(self.live)

    def read_config(self):
        self.calls.append(('read_config',))
        return dict(self.live)

    def set_sweep(self, *args):
        self.calls.append(('set_sweep',) + args)

    def set_power(self, *args):
        self.calls.append(('set_power',) + args)

    def set_ifbw(self, *args):
        self.calls.append(('set_ifbw',) + args)

    def trigger_single(self):
        self.calls.append(('trigger_single',))

    def measure(self):
        self.calls.append(('measure',))
        f = self.frequencies
        s = np.zeros((f.size, 2, 2), dtype=complex)
        return Measurement(f.copy(), s, False,
                           {'idn': 'FAKE', 'vendor': self.vendor_key})

    def close(self):
        self.calls.append(('close',))


def _state(controller=None):
    cfg = dict(CONFIG)
    state = SimpleNamespace(
        vna_lock=threading.RLock(),
        vna={'controller': controller, 'connected': controller is not None,
             'idn': None, 'vendor': None, 'last_error': None, 'config': cfg},
    )
    state.vna_status = lambda: {
        'connected': state.vna['connected'], 'idn': state.vna['idn'],
        'vendor': state.vna['vendor'], 'last_error': state.vna['last_error'],
        'config': state.vna['config'],
        'live_config': (controller.get_config() if controller else None),
    }
    return state


def _setters(controller):
    return [c for c in controller.calls
            if c[0] in ('set_sweep', 'set_power', 'set_ifbw')]


def test_read_config_route_is_registered():
    assert handlers.ROUTES['POST /api/vna/read_config'] is handlers.vna_read_config


def test_connect_is_communication_only():
    controller = FakeController()
    state = _state()
    with mock.patch.object(handlers, 'create_vna', return_value=controller):
        status = handlers.vna_connect(state, {
            'start_hz': 1e9, 'stop_hz': 20e9, 'points': 401,
            'power_dbm': -10.0, 'ifbw_hz': 1000.0})
    assert status['connected'] is True
    assert ('connect',) in controller.calls
    assert _setters(controller) == []
    # Session config mirrors the instrument, not the values typed in the form.
    assert state.vna['config']['start_hz'] == 3e9
    assert state.vna['config']['points'] == 101


def test_connect_apply_sweep_is_an_explicit_opt_in():
    """Legacy callers can still ask for the old behaviour, nothing does by default."""
    controller = FakeController()
    state = _state()
    with mock.patch.object(handlers, 'create_vna', return_value=controller):
        handlers.vna_connect(state, {'apply_sweep': True})
    assert [c[0] for c in _setters(controller)] == [
        'set_sweep', 'set_power', 'set_ifbw']


def test_read_config_is_read_only_and_syncs_state():
    controller = FakeController()
    state = _state(controller)
    result = handlers.vna_read_config(state, {})
    assert result['config']['start_hz'] == 3e9
    assert result['config']['source'] == 'instrument'
    assert state.vna['config']['stop_hz'] == 9e9
    assert _setters(controller) == []
    assert ('read_config',) in controller.calls


def test_read_config_survives_a_controller_without_read_back():
    class Legacy(FakeController):
        read_config = None  # getattr() falls back to the cached get_config()

    controller = Legacy()
    state = _state(controller)
    result = handlers.vna_read_config(state, {})
    assert result['config']['source'] == 'cached'
    assert result['config']['start_hz'] == 3e9
    assert state.vna['config']['points'] == 101
    assert _setters(controller) == []


def test_fetch_refreshes_grid_read_only_and_triggers_once():
    controller = FakeController()
    state = _state(controller)
    data = handlers.vna_fetch(state, {})
    assert len(data['frequencies_hz']) == 101
    assert data['instrument_config']['start_hz'] == 3e9
    assert ('read_config',) in controller.calls
    assert ('measure',) in controller.calls
    assert _setters(controller) == []
    # measure() triggers; the old desktop path also called vna_trigger and
    # therefore swept the instrument twice.
    assert controller.calls.count(('trigger_single',)) == 0


# --------------------------------------------------------------------------- #
# Browser console: same contract, separate UI
# --------------------------------------------------------------------------- #
STATIC = Path(handlers.__file__).resolve().parent / 'static'


def test_web_console_connect_payload_has_no_sweep_values():
    js = (STATIC / 'app.js').read_text(encoding='utf-8')
    start = js.index('function vnaConfigBody()')
    end = js.index("$('vna-connect').onclick", start)
    payload = js[start:end]
    for key in ('start_hz', 'stop_hz', 'points', 'power_dbm', 'ifbw_hz'):
        assert key not in payload
    assert "'/api/vna/read_config'" in js


def test_web_console_exposes_the_read_back_button():
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    assert 'vna-read-config' in html
    assert '连接（仅通信）' in html
    assert '不下发任何参数' in html

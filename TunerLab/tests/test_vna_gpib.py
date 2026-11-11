"""GPIB / VISA transport regression tests.

No VISA runtime and no instrument are needed: ``pyvisa.ResourceManager`` is
replaced with a scripted fake that answers SCPI the way a GPIB analyzer would.
The TCP/IP path is covered by ``test_vna_drivers.py``.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from tuner import vna_drivers as V
from tuner.web import handlers


# --------------------------------------------------------------------------- #
# Scripted VISA instrument
# --------------------------------------------------------------------------- #
class FakeVISAResource:
    """Minimal PyVISA resource: write / read / query / close + attributes."""

    def __init__(self, idn='Agilent Technologies,E5071C,SN123,A.01', n_points=4):
        self.idn = idn
        self.n_points = n_points
        self.timeout = None
        self.read_termination = None
        self.write_termination = None
        self.sent: list[str] = []
        self.last_cmd = ''
        self.closed = False

    def write(self, cmd: str) -> None:
        self.last_cmd = cmd
        self.sent.append(cmd)

    def query(self, cmd: str) -> str:
        self.write(cmd)
        return self._reply(cmd)

    def read(self) -> str:
        return self._reply(self.last_cmd)

    def _reply(self, cmd: str) -> str:
        if cmd.startswith(':CALC1:DATA?'):
            vals = []
            for i in range(self.n_points):
                vals.append(f'{0.1 + i * 0.01:.6f}')
                vals.append(f'{0.02 * i:.6f}')
            return ','.join(vals) + '\n'
        if cmd == '*IDN?':
            return self.idn + '\n'
        if cmd == ':SYST:ERR?':
            return '+0,"No error"\n'
        if cmd == '*OPC?' or cmd.endswith('*OPC?'):
            return '1\n'
        return ''

    def close(self) -> None:
        self.closed = True


class FakeManager:
    """ResourceManager stand-in; also exposes the diagnostic library path."""

    def __init__(self, instrument):
        self.instrument = instrument
        self.opened: list[str] = []
        self.closed = False
        self.visalib = SimpleNamespace(library_path=r'C:\Windows\System32\visa32.dll')

    def open_resource(self, name):
        self.opened.append(name)
        return self.instrument

    def list_resources(self):
        return ('GPIB0::17::INSTR', 'GPIB0::16::INSTR',
                'TCPIP0::192.168.2.111::INSTR')

    def close(self) -> None:
        self.closed = True


def make_gpib_vna(n_points=4, resource='GPIB0::17::INSTR'):
    instrument = FakeVISAResource(n_points=n_points)
    manager = FakeManager(instrument)
    vna = V.create_vna('auto', '', 5025, 5.0, interface='gpib',
                       resource=resource, resource_manager=manager)
    vna.connect()
    vna.set_sweep(1e9, 2e9, n_points)
    return vna, instrument, manager


# --------------------------------------------------------------------------- #
# Resource names
# --------------------------------------------------------------------------- #
def test_gpib_resource_builds_valid_visa_names():
    assert V.gpib_resource() == 'GPIB0::17::INSTR'
    assert V.gpib_resource(1, 16) == 'GPIB1::16::INSTR'
    assert V.gpib_resource(0, 7, 0) == 'GPIB0::7::0::INSTR'


@pytest.mark.parametrize('board,address', [(-1, 17), (16, 17), (0, -1), (0, 31)])
def test_gpib_resource_rejects_out_of_range_addresses(board, address):
    with pytest.raises(ValueError):
        V.gpib_resource(board, address)


@pytest.mark.parametrize('raw,expected', [
    ('17', 'GPIB0::17::INSTR'),
    ('GPIB0::16::INSTR', 'GPIB0::16::INSTR'),
    (' gpib1::7::0::instr ', 'GPIB1::7::0::INSTR'),
])
def test_normalise_gpib_resource(raw, expected):
    assert V.normalise_gpib_resource(raw) == expected


@pytest.mark.parametrize('bad', ['', '192.168.2.111', 'TCPIP0::1.2.3.4::INSTR',
                                 'GPIB0::31::INSTR'])
def test_normalise_gpib_resource_rejects_non_gpib(bad):
    with pytest.raises(ValueError, match='GPIB'):
        V.normalise_gpib_resource(bad)


def test_available_interfaces():
    assert set(V.available_interfaces()) == {'tcpip', 'gpib'}


def test_prepare_visa_dll_paths_is_safe():
    """Must be a no-op off Windows and never break when IO Libraries is absent."""
    V._prepare_visa_dll_paths()
    assert isinstance(V._VISA_DLL_HANDLES, list)


# --------------------------------------------------------------------------- #
# VISA transport contract
# --------------------------------------------------------------------------- #
def test_visascpi_matches_socket_transport_contract():
    instrument = FakeVISAResource()
    manager = FakeManager(instrument)
    transport = V.VISASCPI('17', 3.0, resource_manager=manager)
    assert transport.resource == 'GPIB0::17::INSTR'
    assert not transport.connected

    transport.open()
    assert manager.opened == ['GPIB0::17::INSTR']
    assert instrument.timeout == 3000
    # GPIB ends messages with EOI, not a newline.
    assert instrument.read_termination == ''
    assert instrument.write_termination == ''
    assert transport.connected
    assert 'E5071C' in transport.query('*IDN?')
    transport.write(':SENS1:FREQ:STAR 1e9')
    assert instrument.sent[-1] == ':SENS1:FREQ:STAR 1e9'
    transport.write(':SYST:ERR?')
    assert transport.read().startswith('+0')  # read() is part of the contract

    transport.close()
    assert not transport.connected
    assert instrument.closed
    assert not manager.closed  # an injected manager stays owned by its caller


def test_visascpi_reports_missing_visa_runtime():
    with mock.patch('pyvisa.ResourceManager',
                    side_effect=ValueError('Could not locate a VISA implementation')):
        with pytest.raises(RuntimeError, match='Keysight IO Libraries Suite'):
            V.VISASCPI().open()


def test_visascpi_reports_open_failure_with_resource_name():
    manager = FakeManager(FakeVISAResource())
    manager.open_resource = mock.Mock(side_effect=RuntimeError('VI_ERROR_RSRC_NFOUND'))
    with pytest.raises(RuntimeError, match='GPIB0::17::INSTR'):
        V.VISASCPI('GPIB0::17::INSTR', resource_manager=manager).open()


def test_visa_resources_diagnostic_lists_gpib():
    manager = FakeManager(FakeVISAResource())
    with mock.patch('pyvisa.ResourceManager', return_value=manager):
        info = V.visa_resources()
    assert 'GPIB0::17::INSTR' in info['resources']
    assert 'visa32' in info['backend']
    assert manager.closed


def test_visa_resources_diagnostic_explains_missing_runtime():
    with mock.patch('pyvisa.ResourceManager',
                    side_effect=ValueError('Could not locate a VISA implementation')):
        with pytest.raises(RuntimeError, match='82357B'):
            V.visa_resources()


# --------------------------------------------------------------------------- #
# Full GPIB acquisition through the shared driver
# --------------------------------------------------------------------------- #
def test_create_vna_gpib_uses_visa_transport_and_stamps_resource():
    vna, instrument, manager = make_gpib_vna(n_points=4)
    assert isinstance(vna.transport, V.VISASCPI)
    assert vna.interface == 'gpib'
    assert vna.resource == 'GPIB0::17::INSTR'
    assert manager.opened == ['GPIB0::17::INSTR']
    assert 'E5071C' in vna.idn
    assert vna.vendor_key == 'keysight_ena'

    from tuner.models import Measurement
    m = vna.measure()
    assert isinstance(m, Measurement)
    assert m.s.shape == (4, 2, 2)
    assert m.simulated is False
    assert m.metadata['resource'] == 'GPIB0::17::INSTR'
    assert m.metadata['interface'] == 'gpib'
    assert m.metadata['vendor'] == 'keysight_ena'
    assert any(c.startswith(':CALC1:DATA?') for c in instrument.sent)
    vna.close()
    assert instrument.closed


def test_create_vna_gpib_accepts_bare_address_and_secondary():
    vna = V.create_vna('rsp', '', 5025, 5.0, interface='gpib', resource='16')
    assert vna.resource == 'GPIB0::16::INSTR'
    vna2 = V.create_vna('rsp', '', 5025, 5.0, interface='gpib',
                        resource='GPIB1::7::0::INSTR')
    assert vna2.resource == 'GPIB1::7::0::INSTR'


def test_create_vna_tcpip_signature_unchanged():
    vna = V.create_vna('auto', '192.168.0.99', 5025, 5.0)
    assert isinstance(vna.transport, V.SocketSCPI)
    assert vna.interface == 'tcpip'
    assert vna.resource == '192.168.0.99:5025'


def test_create_vna_rejects_unknown_interface():
    with pytest.raises(ValueError, match='unknown VNA interface'):
        V.create_vna('auto', '1.2.3.4', interface='usb')


def test_read_errors_survives_instrument_without_error_queue():
    instrument = FakeVISAResource()
    instrument.query = mock.Mock(side_effect=RuntimeError('timeout'))
    transport = V.VISASCPI(resource_manager=FakeManager(instrument))
    transport.open()
    vna = V.TCPVNA('', transport=transport, resource='GPIB0::17::INSTR',
                   interface='gpib')
    assert vna.connected
    assert vna.read_errors() == []


# --------------------------------------------------------------------------- #
# Handler / studio dispatch
# --------------------------------------------------------------------------- #
def _state():
    cfg = {
        'host': '192.168.2.111', 'port': 5025, 'timeout_ms': 10000,
        'vendor_key': 'auto', 'interface': 'tcpip',
        'resource': 'GPIB0::17::INSTR',
        'start_hz': 1e9, 'stop_hz': 2e9, 'points': 11,
        'power_dbm': -10.0, 'ifbw_hz': 1000.0,
    }
    state = SimpleNamespace(
        vna_lock=threading.RLock(),
        vna={'controller': None, 'connected': False, 'idn': None,
             'vendor': None, 'last_error': None, 'config': cfg},
    )
    state.vna_status = lambda: dict(state.vna)
    return state


class _FakeController:
    vendor_key = 'keysight_ena'
    last_error = None

    def connect(self):
        return 'Agilent Technologies,E5071C,SN123,A.01'

    def set_sweep(self, *args):
        pass

    def set_power(self, *args):
        pass

    def set_ifbw(self, value):
        self.ifbw = value

    def get_config(self):
        return {'ifbw_hz': self.ifbw}

    def close(self):
        pass


def test_vna_connect_dispatches_gpib_interface():
    state = _state()
    with mock.patch.object(handlers, 'create_vna',
                           return_value=_FakeController()) as factory:
        status = handlers.vna_connect(
            state, {'interface': 'gpib', 'resource': 'GPIB0::16::INSTR'})
    assert status['connected'] is True
    assert state.vna['config']['interface'] == 'gpib'
    assert state.vna['config']['resource'] == 'GPIB0::16::INSTR'
    kwargs = factory.call_args.kwargs
    assert kwargs['interface'] == 'gpib'
    assert kwargs['resource'] == 'GPIB0::16::INSTR'


def test_vna_connect_defaults_to_tcpip():
    state = _state()
    with mock.patch.object(handlers, 'create_vna',
                           return_value=_FakeController()) as factory:
        handlers.vna_connect(state, {})
    assert factory.call_args.kwargs['interface'] == 'tcpip'


def test_vna_connect_failure_clears_stale_status():
    state = _state()
    state.vna['connected'] = True
    state.vna['controller'] = _FakeController()

    class Broken(_FakeController):
        def connect(self):
            raise RuntimeError('VISA open failed')

    with mock.patch.object(handlers, 'create_vna', return_value=Broken()):
        with pytest.raises(RuntimeError, match='VISA open failed'):
            handlers.vna_connect(state, {'interface': 'gpib', 'resource': '17'})
    assert state.vna['connected'] is False
    assert state.vna['controller'] is None
    assert 'VISA open failed' in state.vna['last_error']


def test_vna_visa_resources_handler():
    manager = FakeManager(FakeVISAResource())
    with mock.patch('pyvisa.ResourceManager', return_value=manager):
        info = handlers.vna_visa_resources(None, {})
    assert 'GPIB0::17::INSTR' in info['resources']


def test_vna_interfaces_handler():
    assert handlers.vna_interfaces(None, {})['interfaces']['gpib']


# --------------------------------------------------------------------------- #
# Web console keeps the same interface controls as the desktop app
# --------------------------------------------------------------------------- #
def test_web_console_exposes_gpib_controls():
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / 'tuner' / 'web' / 'static'
    html = (static / 'index.html').read_text(encoding='utf-8')
    js = (static / 'app.js').read_text(encoding='utf-8')
    for token in ('id="vna-interface"', 'id="vna-resource"',
                  'id="vna-visa-detect"', 'id="st-vna-interface"',
                  'id="st-vna-resource"'):
        assert token in html
    for token in ('syncVnaInterface', 'syncStVnaInterface',
                  "interface: $('vna-interface').value",
                  'body.vna_interface', '/api/vna/visa_resources'):
        assert token in js

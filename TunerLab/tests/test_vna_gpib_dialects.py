"""GPIB dialect safety nets for instruments without an IEEE-488.2 error queue.

A modern analyzer answers ``:SYST:ERR?`` with ``-113,"Undefined header"``
when a command is unknown; the driver uses that to negotiate the measurement
definition.  Older GPIB boxes (HP 87xx / early ENA) may have neither that
queue nor ``*CLS``.  Treating a VISA timeout as "no error" would make every
rejected command look accepted -- the exact failure mode these tests guard.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from tuner import vna_drivers as V
from tuner.web import handlers
from tunerlab_app import studio_bridge


class LegacyGPIBTransport:
    """Pre-SCPI analyzer: no *CLS, no :SYST:ERR?, optional catalogue readback."""

    IDN = 'HEWLETT-PACKARD,8753E,SN1,A.01.00'

    def __init__(self, accepted=':CALC1:PAR1:DEF', readback=False):
        self.accepted = accepted
        self.readback = readback
        self.sent: list[str] = []
        self.defined: list[str] = []
        self.opened = False

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False

    @property
    def connected(self):
        return self.opened

    def read(self):
        return ''

    def write(self, cmd: str) -> None:
        self.sent.append(cmd)
        if cmd == '*CLS':
            raise RuntimeError('VISA timeout: *CLS unsupported')
        if any(token in cmd for token in (':PAR:DEF', ':PAR:SDEF',
                                          ':PAR:EXT', ':MEAS:DEF', 'PAR1:DEF')):
            header = cmd.split(' ', 1)[0]
            if header == self.accepted:
                name = cmd.split("'")[1] if "'" in cmd else 'S11'
                self.defined.append(name)

    def query(self, cmd: str) -> str:
        self.sent.append(cmd)
        if cmd == '*IDN?':
            return self.IDN
        if cmd == ':SYST:ERR?':
            raise RuntimeError('VISA timeout: no error queue')
        if cmd == ':CALC1:PAR:CAT?':
            if not self.readback:
                raise RuntimeError('VISA timeout: no catalogue')
            return ','.join(f'"{n}"' for n in self.defined)
        if cmd == ':CALC1:PAR:SEL?':
            if not self.readback:
                raise RuntimeError('VISA timeout: no read-back')
            return ''
        if cmd.startswith(':CALC1:DATA?'):
            return '0.1,0.02,0.1,0.02'
        if cmd.endswith('*OPC?') or cmd == '*OPC?':
            return '1'
        return '1'


def _legacy_vna(**kw):
    tr = LegacyGPIBTransport(**kw)
    return V.TCPVNA('', transport=tr, resource='GPIB0::17::INSTR',
                    interface='gpib'), tr


# --------------------------------------------------------------------------- #
# No error queue: do not equate a timeout with success
# --------------------------------------------------------------------------- #
def test_connect_survives_instrument_without_error_queue_or_cls():
    vna, tr = _legacy_vna()
    assert vna.connect() == LegacyGPIBTransport.IDN
    assert vna._error_queue_state is False
    assert vna._cls_usable is False
    assert vna.dialect_warning and '无法自动判定' in vna.dialect_warning
    assert vna.last_error and 'SCPI 调试工具' in vna.last_error
    # *CLS is attempted once, then skipped -- no repeated VISA timeouts.
    assert sum(1 for c in tr.sent if c == '*CLS') <= 1


def test_no_queue_fallback_stops_probing_instead_of_spraying_candidates():
    vna, tr = _legacy_vna()
    vna.connect()
    defines = [c for c in tr.sent if any(t in c for t in (
        ':PAR:DEF', ':PAR:SDEF', ':PAR:EXT', ':MEAS:DEF', 'PAR1:DEF'))]
    # Four S-parameters, one command each: no blind 6-candidate sweep.
    assert len(defines) == 4
    assert all(c.startswith(':CALC1:PAR:DEF ') for c in defines)


def test_no_queue_with_catalogue_readback_picks_the_working_dialect():
    vna, tr = _legacy_vna(accepted=':CALC1:PAR:DEF:EXT', readback=True)
    vna.connect()
    assert vna._define_cmd == ':CALC1:PAR:DEF:EXT'
    assert vna.dialect_warning is None
    # All four S-parameters reuse the dialect that the catalogue confirmed.
    assert sum(1 for c in tr.sent
               if c.startswith(':CALC1:PAR:DEF:EXT ')) == 4


def test_status_exposes_queue_state_and_warning():
    vna, _ = _legacy_vna()
    vna.connect()
    cfg = vna.get_config()
    assert cfg['error_queue_usable'] is False
    assert cfg['dialect_warning']
    assert cfg['define_cmd'] == ':CALC1:PAR:DEF'


# --------------------------------------------------------------------------- #
# GPIB-only legacy candidates
# --------------------------------------------------------------------------- #
def test_gpib_adds_trace_numbered_dialects_tcpip_does_not():
    gpib, _ = _legacy_vna()
    tcp = V.TCPVNA('192.168.2.111')
    assert ':CALC1:PAR{tr}:DEF' in gpib._define_headers()
    assert ':CALC1:MEAS:DEF' in gpib._define_headers()
    assert ':CALC1:PAR{tr}:DEF' not in tcp._define_headers()
    assert ':CALC1:MEAS:DEF' not in tcp._define_headers()


@pytest.mark.parametrize('param,expected', [
    ('S11', ':CALC1:PAR1:DEF S11'),
    ('S21', ':CALC1:PAR2:DEF S21'),
    ('S12', ':CALC1:PAR3:DEF S12'),
    ('S22', ':CALC1:PAR4:DEF S22'),
])
def test_trace_numbered_define_uses_the_right_trace(param, expected):
    assert V.TCPVNA._format_define(':CALC1:PAR{tr}:DEF', param, param) == expected


def test_standard_define_keeps_named_two_argument_form():
    assert (V.TCPVNA._format_define(':CALC1:PAR:DEF:EXT', 'S11', 'S11')
            == ":CALC1:PAR:DEF:EXT 'S11','S11'")


# --------------------------------------------------------------------------- #
# Probe-pinned dialect
# --------------------------------------------------------------------------- #
def test_apply_dialect_only_accepts_known_fields():
    vna, _ = _legacy_vna()
    applied = vna.apply_dialect({'define_cmd': ':CALC1:PAR:DEF:EXT',
                                 'fetch_mnemonic': 'SDATA',
                                 'arbitrary': 'must be dropped'})
    assert applied == {'define_cmd': ':CALC1:PAR:DEF:EXT',
                       'fetch_mnemonic': 'SDATA'}
    assert vna._define_cmd == ':CALC1:PAR:DEF:EXT'
    assert vna._fetch_mnemonic == 'SDATA'


def test_dialect_survives_profile_application_on_connect():
    tr = LegacyGPIBTransport()
    vna = V.create_vna('auto', '', 5025, 5.0, interface='gpib',
                       resource='GPIB0::17::INSTR', dialect={
                           'define_cmd': ':CALC1:PAR:DEF:EXT',
                           'power_cmd': ':SOUR1:POW:LEV:IMM:AMPL'})
    # _FakeManager is not needed: replace the transport after construction.
    vna.transport = tr
    vna.connect()
    assert vna._define_cmd == ':CALC1:PAR:DEF:EXT'
    assert vna._power_cmd == ':SOUR1:POW:LEV:IMM:AMPL'
    assert vna.dialect['define_cmd'] == ':CALC1:PAR:DEF:EXT'


def test_handler_connect_passes_config_dialect_to_factory():
    cfg = {'host': '192.168.2.111', 'port': 5025, 'timeout_ms': 10000,
           'vendor_key': 'auto', 'interface': 'gpib',
           'resource': 'GPIB0::17::INSTR', 'dialect': {
               'define_cmd': ':CALC1:PAR:DEF:EXT'},
           'start_hz': 1e9, 'stop_hz': 20e9, 'points': 401,
           'power_dbm': -10.0, 'ifbw_hz': 1000.0}
    state = SimpleNamespace(
        vna_lock=threading.RLock(),
        vna={'controller': None, 'connected': False, 'idn': None,
             'vendor': None, 'last_error': None, 'config': cfg})
    state.vna_status = lambda: dict(state.vna)
    controller = mock.Mock()
    controller.connect.return_value = LegacyGPIBTransport.IDN
    with mock.patch.object(handlers, 'create_vna',
                           return_value=controller) as factory:
        handlers.vna_connect(state, {})
    assert factory.call_args.kwargs['dialect']['define_cmd'] == \
        ':CALC1:PAR:DEF:EXT'


def test_studio_wizard_uses_pinned_dialect_from_state():
    state = SimpleNamespace(vna={'config': {
        'dialect': {'define_cmd': ':CALC1:PAR:DEF:EXT'}}})
    body = {'vna_source': 'real', 'vna_vendor': 'auto',
            'vna_ip': '192.168.2.111', 'vna_interface': 'gpib',
            'vna_resource': 'GPIB0::17::INSTR',
            'vna_start_hz': 1e9, 'vna_stop_hz': 2e9, 'vna_points': 11}
    controller = mock.Mock()
    with mock.patch.object(studio_bridge, 'create_vna',
                           return_value=controller) as factory:
        studio_bridge._build_vna(state, body, None, None)
    assert factory.call_args.kwargs['dialect'] == {
        'define_cmd': ':CALC1:PAR:DEF:EXT'}


# --------------------------------------------------------------------------- #
# The workshop N5224A probe (2026-11-11, GPIB0::16, fw A.09.90.19)
# --------------------------------------------------------------------------- #
def test_n5224a_autodetects_the_pna_profile():
    idn = 'Agilent Technologies,N5224A,MY51441375,A.09.90.19'
    assert V.detect_vendor(idn) == 'keysight_pna'


def test_keysight_pna_profile_matches_the_verified_probe():
    """The shipped defaults are the command set the probe accepted live."""
    profile = V._VENDOR_PROFILES['keysight_pna']
    assert profile['define_cmd'] == ':CALC1:PAR:DEF:EXT'   # DEF:EXT got +0
    assert profile['fetch_mnemonic'] == 'SDATA'            # CALC1:DATA? SDATA
    assert profile['power_cmd'] == ':SOUR1:POW:LEV'        # LEV form got +0
    assert profile['ifbw_cmd'] == ':SENS1:BAND'
    assert profile['trigger_cmd'] == ':INIT1:IMM'          # INIT:IMM timed out

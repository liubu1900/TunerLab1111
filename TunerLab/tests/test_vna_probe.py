"""SCPI capability probe: shared module, CLI and Web API regressions.

A scripted instrument reproduces both a modern analyzer (IEEE-488.2 error
queue) and an older GPIB box that has neither ``*CLS`` nor ``:SYST:ERR?``.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from tuner import vna_drivers as V
from tuner import vna_probe as P
from tuner.web import handlers

IDN = 'Agilent Technologies,N4225A,SN123,A.01.00'


class ScriptedInstrument:
    """Fake SCPI transport with a configurable command set."""

    def __init__(self, accepted=':CALC1:PAR:DEF:EXT', *,
                 no_error_queue=False, no_cls=False, no_readback=False,
                 idn=IDN, data='0.1,0.02,0.2,0.03'):
        self.accepted = accepted
        self.no_error_queue = no_error_queue
        self.no_cls = no_cls
        self.no_readback = no_readback
        self.idn = idn
        self.data = data
        self.sent: list[str] = []
        self.queue: list[str] = []
        self.catalog: list[str] = []
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

    def _is_define(self, cmd):
        return any(token in cmd for token in (
            ':PAR:DEF', ':PAR:SDEF', ':PAR:EXT', ':MEAS:DEF', 'PAR1:DEF'))

    def write(self, cmd):
        self.sent.append(cmd)
        if cmd == '*CLS':
            if self.no_cls:
                raise RuntimeError('VISA timeout on *CLS')
            self.queue.clear()
        elif self._is_define(cmd):
            header = cmd.split(' ', 1)[0].replace('PAR1', 'PAR')
            if header == self.accepted or cmd.startswith(self.accepted + ' '):
                self.catalog.append('TL_probe_S11')
            else:
                self.queue.append('-113,"Undefined header"')

    def query(self, cmd):
        self.sent.append(cmd)
        if cmd == '*IDN?':
            return self.idn
        if cmd == '*OPT?':
            return '0'
        if cmd == ':SYST:ERR?':
            if self.no_error_queue:
                raise RuntimeError('VISA timeout on :SYST:ERR?')
            return self.queue.pop(0) if self.queue else '+0,"No error"'
        if cmd == ':CALC1:PAR:CAT?':
            if self.no_readback:
                raise RuntimeError('VISA timeout on :CALC1:PAR:CAT?')
            return ','.join(f'"{n}"' for n in self.catalog)
        if 'DATA' in cmd and '?' in cmd:
            return self.data
        if cmd.endswith('*OPC?') or cmd == '*OPC?':
            return '1'
        return '1'


# --------------------------------------------------------------------------- #
# Core probe
# --------------------------------------------------------------------------- #
def test_probe_identifies_instrument_and_accepted_dialect():
    inst = ScriptedInstrument()
    result = P.probe_transport(inst, target='192.168.2.111:5025')
    assert result['idn'] == IDN
    assert result['idn_fields']['model'] == 'N4225A'
    assert result['idn_fields']['firmware'] == 'A.01.00'
    assert result['capabilities']['error_queue'] is True
    assert result['accepted_define'] == [
        ":CALC1:PAR:DEF:EXT 'TL_probe_S11','S11'"]
    assert result['suggested_dialect']['define_cmd'] == ':CALC1:PAR:DEF:EXT'


def test_probe_report_contains_all_four_sections():
    result = P.probe_transport(ScriptedInstrument())
    text = P.format_report(result)
    for token in ('1) 仪器身份', '2) 测量定义命令', '3) 其余 SCPI 命令能力',
                  '4) 结论建议', IDN, '-113', ':CALC1:PAR:DEF:EXT'):
        assert token in text


def test_probe_reads_responses_for_queries_with_embedded_question_mark():
    """``:CALC1:DATA? SDATA`` is a query even though '?' is in the middle."""
    inst = ScriptedInstrument(data='0.1,0.02,0.2,0.03')
    result = P.probe_transport(inst)
    rows = {r['cmd']: r for r in result['other']}
    assert rows[':CALC1:DATA? SDATA']['value'] == '0.1,0.02,0.2,0.03'
    assert rows[':CALC1:DATA? SDATA']['verdict'] == 'OK'
    assert result['suggested_dialect']['fetch_mnemonic'] == 'SDATA'


def test_suggested_dialect_collects_power_ifbw_and_trigger():
    result = P.probe_transport(ScriptedInstrument())
    dialect = result['suggested_dialect']
    assert dialect['power_cmd'] == ':SOUR1:POW:LEV'
    assert dialect['ifbw_cmd'] == ':SENS1:BAND'
    assert dialect['trigger_cmd'] == ':INIT1:IMM'
    assert set(dialect) <= set(V.DIALECT_FIELDS)


def test_probe_without_error_queue_marks_unknown_not_reject():
    inst = ScriptedInstrument(no_error_queue=True, no_cls=True,
                              no_readback=True)
    result = P.probe_transport(inst)
    caps = result['capabilities']
    assert caps['error_queue'] is False
    assert caps['cls'] is False
    assert caps['readback'] is False
    assert any('没有可用的 :SYST:ERR?' in n for n in caps['notes'])
    assert result['define'][0]['verdict'] == 'UNKNOWN'
    assert not any(r['verdict'] == 'REJECT' for r in result['define'])
    assert 'UNKNOWN' in P.format_report(result)


def test_probe_without_error_queue_uses_catalogue_readback():
    inst = ScriptedInstrument(no_error_queue=True, no_cls=True)
    result = P.probe_transport(inst)
    accepted = [r['cmd'] for r in result['define'] if r['verdict'] == 'OK']
    assert accepted == [":CALC1:PAR:DEF:EXT 'TL_probe_S11','S11'"]
    assert result['suggested_dialect']['define_cmd'] == ':CALC1:PAR:DEF:EXT'


def test_probe_respects_stop_request():
    inst = ScriptedInstrument()
    result = P.probe_transport(inst, should_stop=lambda: True)
    assert result['aborted'] is True
    assert result['define'] == []
    assert any('不完整' in line for line in result['summary'])


def test_probe_reports_progress():
    steps: list[tuple] = []
    P.probe_transport(ScriptedInstrument(), on_step=lambda *a: steps.append(a))
    assert steps
    assert steps[0][0] == '身份'
    assert any(s[0] == '测量定义' for s in steps)
    assert any(s[0] == '命令能力' for s in steps)


def test_probe_opens_and_closes_its_own_transport():
    inst = ScriptedInstrument()
    with mock.patch.object(P, 'make_transport', return_value=inst):
        result = P.probe(interface='tcpip', host='10.0.0.1', port=5025)
    assert result['target'] == '10.0.0.1:5025'
    assert inst.opened is False   # closed again in the finally block


def test_probe_connection_failure_is_readable():
    class Broken(ScriptedInstrument):
        def open(self):
            raise OSError('connection refused')

    with mock.patch.object(P, 'make_transport', return_value=Broken()):
        with pytest.raises(RuntimeError, match='无法连接'):
            P.probe(interface='tcpip', host='10.0.0.1')


def test_probe_gpib_open_failure_mentions_io_libraries():
    class Broken(ScriptedInstrument):
        def open(self):
            raise OSError('VI_ERROR_RSRC_NFOUND')

    with mock.patch.object(P, 'make_transport', return_value=Broken()):
        with pytest.raises(RuntimeError, match='Keysight IO Libraries Suite'):
            P.probe(interface='gpib', resource='GPIB0::17::INSTR')


# --------------------------------------------------------------------------- #
# Web handlers
# --------------------------------------------------------------------------- #
def _state(connected=False):
    cfg = {
        'host': '192.168.2.111', 'port': 5025, 'timeout_ms': 10000,
        'vendor_key': 'auto', 'interface': 'tcpip',
        'resource': 'GPIB0::17::INSTR', 'dialect': {},
        'start_hz': 1e9, 'stop_hz': 20e9, 'points': 401,
        'power_dbm': -10.0, 'ifbw_hz': 1000.0,
    }
    state = SimpleNamespace(
        vna_lock=__import__('threading').RLock(),
        vna={'controller': None, 'connected': connected, 'idn': None,
             'vendor': None, 'last_error': None, 'config': cfg})
    state.vna_status = lambda: dict(state.vna)
    return state


def test_handler_probe_returns_report_and_structured_result():
    state = _state()
    inst = ScriptedInstrument()
    with mock.patch.object(P, 'make_transport', return_value=inst):
        result = handlers.vna_probe(state, {'timeout_ms': 2500})
    assert result['idn'] == IDN
    assert ':CALC1:PAR:DEF:EXT' in result['report']
    assert result['suggested_dialect']['define_cmd'] == ':CALC1:PAR:DEF:EXT'
    assert inst.opened is False


def test_handler_probe_refuses_to_share_a_live_gpib_session():
    state = _state(connected=True)
    state.vna['controller'] = SimpleNamespace(connected=True)
    with pytest.raises(RuntimeError, match='断开'):
        handlers.vna_probe(state, {})


def test_handler_apply_dialect_filters_unknown_keys():
    state = _state()
    result = handlers.vna_apply_dialect(state, {
        'dialect': {'define_cmd': ':CALC1:PAR:DEF:EXT',
                    'fetch_mnemonic': 'SDATA', 'evil_key': 'drop me'}})
    assert result['dialect'] == {'define_cmd': ':CALC1:PAR:DEF:EXT',
                                 'fetch_mnemonic': 'SDATA'}
    assert state.vna['config']['dialect'] == result['dialect']


def test_handler_apply_dialect_accepts_a_full_probe_result():
    state = _state()
    inst = ScriptedInstrument()
    with mock.patch.object(P, 'make_transport', return_value=inst):
        probe_result = handlers.vna_probe(state, {})
    applied = handlers.vna_apply_dialect(state, {'result': probe_result})
    assert applied['dialect']['define_cmd'] == ':CALC1:PAR:DEF:EXT'
    assert state.vna['config']['dialect'] == applied['dialect']


def test_handler_connect_passes_pinned_dialect():
    state = _state()
    state.vna['config']['dialect'] = {'define_cmd': ':CALC1:PAR:DEF:EXT'}
    controller = mock.Mock()
    controller.connect.return_value = IDN
    with mock.patch.object(handlers, 'create_vna', return_value=controller) as fac:
        handlers.vna_connect(state, {})
    assert fac.call_args.kwargs['dialect'] == {
        'define_cmd': ':CALC1:PAR:DEF:EXT'}


# --------------------------------------------------------------------------- #
# Dialect persistence (schema 5 vna_dialect)
# --------------------------------------------------------------------------- #
def test_handler_apply_dialect_persists_to_settings(tmp_path):
    """An apply writes the pinned dialect into the settings file."""
    from tuner.settings import SettingsStore

    store = SettingsStore(tmp_path / 'settings.json')
    session = SimpleNamespace(store=store, settings=store.load())
    state = _state()
    state.hw = session

    result = handlers.vna_apply_dialect(state, {
        'dialect': {'define_cmd': ':CALC1:PAR:DEF:EXT'}})
    assert result['persisted'] is True
    assert result['persist_error'] is None

    reloaded = store.load()
    assert reloaded.vna_dialect == {'define_cmd': ':CALC1:PAR:DEF:EXT'}


def test_handler_apply_dialect_without_a_store_stays_in_memory():
    """The bare web console has no settings store; apply must still work."""
    state = _state()
    result = handlers.vna_apply_dialect(state, {
        'dialect': {'define_cmd': ':CALC1:PAR:DEF:EXT'}})
    assert result['dialect'] == {'define_cmd': ':CALC1:PAR:DEF:EXT'}
    assert result['persisted'] is False
    assert result['persist_error']


# --------------------------------------------------------------------------- #
# CLI + Web console wiring
# --------------------------------------------------------------------------- #
def test_cli_script_is_a_thin_wrapper():
    source = (Path(__file__).resolve().parents[2] / 'verification' /
              'probe_vna_scpi.py').read_text(encoding='utf-8')
    assert 'from tuner.vna_probe import format_report, probe' in source
    assert '--interface' in source and '--resource' in source and '--json' in source


def test_web_console_exposes_probe_controls():
    static = Path(__file__).resolve().parents[1] / 'tuner' / 'web' / 'static'
    html = (static / 'index.html').read_text(encoding='utf-8')
    js = (static / 'app.js').read_text(encoding='utf-8')
    for token in ('id="vna-probe-run"', 'id="vna-probe-disconnect"',
                  'id="vna-probe-apply"', 'id="vna-probe-copy"',
                  'id="vna-probe-out"'):
        assert token in html
    for token in ('/api/vna/probe', '/api/vna/apply_dialect',
                  'runVnaProbe', 'suggested_dialect'):
        assert token in js

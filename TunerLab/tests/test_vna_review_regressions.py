"""Regressions for the Windows VNA connection and measurement review."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from tuner.web import handlers
from tuner.vna_drivers import TCPVNA, is_duplicate_measurement
from tunerlab_app import studio_bridge


def state():
    s = SimpleNamespace(vna_lock=threading.RLock(), vna={
        'controller': None, 'config': dict(
            vendor_key='auto', host='example.invalid', port=5025,
            timeout_ms=1000, interface='gpib', resource='GPIB0::16::INSTR',
            start_hz=1e9, stop_hz=2e9, points=11, power_dbm=-10,
            ifbw_hz=1000)})
    s.vna_status = lambda: s.vna
    return s


def test_connect_preserves_zero_dbm():
    s = state()
    c = Mock(vendor_key='keysight_pna', last_error=None)
    with patch.object(handlers, 'create_vna', return_value=c):
        handlers.vna_connect(s, {'power_dbm': 0})
    c.set_power.assert_called_once_with(0.0)
    assert s.vna['connected']


@pytest.mark.parametrize('step', ['connect', 'set_sweep', 'set_power', 'set_ifbw'])
def test_connect_failure_always_closes_unpublished_session(step):
    s = state()
    c = Mock()
    getattr(c, step).side_effect = ValueError('failed ' + step)
    with patch.object(handlers, 'create_vna', return_value=c):
        with pytest.raises(ValueError, match='failed'):
            handlers.vna_connect(s, {})
    c.close.assert_called_once()
    assert s.vna['controller'] is None
    assert not s.vna['connected']
    assert s.vna['last_error'] == 'failed ' + step


@pytest.mark.parametrize('step', ['connect', 'set_sweep'])
def test_real_studio_build_closes_failed_vna(step):
    c = Mock()
    getattr(c, step).side_effect = ValueError('failed ' + step)
    with patch.object(studio_bridge, 'create_vna', return_value=c):
        with pytest.raises(ValueError):
            studio_bridge._build_vna(None, {'vna_source': 'real'}, None, None)
    c.close.assert_called_once()


@pytest.mark.parametrize('response', [
    '-104,"Data type error in measurement name"',
    '+1104,"Duplicate measurement name"',
    '1040,"Duplicate measurement name"',
    '+104,"Invalid measurement name"',
])
def test_duplicate_requires_exact_code_and_collision_semantics(response):
    assert not is_duplicate_measurement(response)


class ExistingMeasurement:
    def __init__(self, catalog):
        self.catalog = catalog
        self.commands = []
        self.error = '+0,"No error"'

    def write(self, cmd):
        self.commands.append(cmd)
        if cmd.startswith(':CALC1:PAR:DEF'):
            self.error = '+104,"Duplicate measurement name"'

    def query(self, cmd):
        self.commands.append(cmd)
        if cmd == ':CALC1:PAR:CAT?':
            if isinstance(self.catalog, Exception):
                raise self.catalog
            return self.catalog
        result, self.error = self.error, '+0,"No error"'
        return result


@pytest.mark.parametrize('catalog', ['"S11,S11,S21,S21"', '"S11","S11","S21","S21"'])
def test_matching_duplicate_is_reused_after_catalogue_verification(catalog):
    tr = ExistingMeasurement(catalog)
    v = TCPVNA('unused', transport=tr)
    v._define_measurement('S11', 'S11')
    assert ':CALC1:PAR:CAT?' in tr.commands
    assert not any(':DEL' in cmd for cmd in tr.commands)


@pytest.mark.parametrize('catalog', ['"S11,S22"', '"S110,S11"', '', '"S11"', TimeoutError()])
def test_conflicting_or_unverifiable_duplicate_cannot_be_used(catalog):
    tr = ExistingMeasurement(catalog)
    v = TCPVNA('unused', transport=tr)
    with pytest.raises(RuntimeError, match='measurement S11'):
        v._define_measurement('S11', 'S11')
    assert not any(':DEL' in cmd for cmd in tr.commands)


def test_dialect_replacement_and_clear_restore_vendor_defaults():
    v = TCPVNA('unused', vendor_key='keysight_pna')
    v._apply_profile()
    v.apply_dialect({'fetch_mnemonic': 'SDAT', 'power_cmd': ':CUSTOM'})
    v.apply_dialect({'ifbw_cmd': ':CUSTOM_BAND'})
    assert v._fetch_mnemonic == 'SDATA'
    assert v._power_cmd == ':SOUR1:POW:LEV'
    assert v._ifbw_cmd == ':CUSTOM_BAND'
    assert v.apply_dialect({}) == {}
    assert v._trigger_cmd == ':SENS1:SWE:MODE SING'


@pytest.mark.parametrize('step', ['connect', 'set_sweep'])
def test_shared_studio_build_closes_failed_real_vna(step):
    from tuner.web.state import AppState
    s = AppState()
    c = Mock()
    getattr(c, step).side_effect = ValueError('failed ' + step)
    with patch.object(handlers, 'create_vna', return_value=c):
        with pytest.raises(ValueError, match='failed'):
            handlers.studio_init_hardware(s, {
                'motion_source': 'mock', 'vna_source': 'real'})
    c.close.assert_called_once()

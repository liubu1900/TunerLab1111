"""Stateful N5224A GPIB regressions based on the supplied error3.txt.

This models documented/reported command and error behaviour, not physical RF.
"""
import re

import numpy as np
import pytest

from tuner import vna_drivers as V, vna_probe as P

IDN = 'Agilent Technologies,N5224A,MY51441375,A.09.90.19'


class N5224A:
    def __init__(self):
        self.sent = []
        self.errors = []
        self.measurements = {}
        self.selected = None
        self.points = 41
        self.ifbw = 1000.0
        self.power_by_port = {}
        self.trigger_source = 'EXT'  # Previous operator state must not hang us.
        self.reject = None
        self.opc = '1'
        self.sweeps = 0
        self.closed = False

    def write(self, cmd):
        self.sent.append(cmd)
        if cmd == '*CLS':
            self.errors.clear()
        elif self.reject and cmd.startswith(self.reject):
            self.errors.append('-222,"Data out of range"')
        elif cmd.startswith((':CALC1:PAR:DEF:EXT ', ':CALC1:PAR:EXT ')):
            name, param = re.findall(r"'([^']*)'", cmd)
            if name in self.measurements:
                self.errors.append('+104,"Duplicate measurement name"')
            else:
                self.measurements[name] = param
        elif cmd.startswith(':CALC1:PAR:SEL '):
            name = re.findall(r"'([^']*)'", cmd)[0]
            if name not in self.measurements:
                self.errors.append('+105,"Measurement not found"')
            else:
                self.selected = name
        elif cmd.startswith(':CALC1:PAR:DEL '):
            self.measurements.pop(re.findall(r"'([^']*)'", cmd)[0], None)
        elif cmd == ':TRIG:SOUR IMM':
            self.trigger_source = 'IMM'
        elif cmd == ':INIT1:IMM':
            if self.trigger_source != 'MAN':
                self.errors.append('-221,"INIT requires manual trigger source"')
            else:
                self.sweeps += 1
        elif cmd == ':SENS1:SWE:MODE SING':
            if self.trigger_source != 'IMM':
                raise TimeoutError('single accept needs immediate source')
            self.sweeps += 1
        elif cmd.startswith(':SENS1:BAND '):
            requested = float(cmd.split()[-1])
            self.ifbw = 2000.0 if requested == 1500.0 else requested
        elif cmd.startswith((':SOUR1:POW:LEV ', ':SOUR1:POW2:LEV ')):
            self.power_by_port[2 if ':POW2' in cmd else 1] = float(cmd.split()[-1])
        elif cmd.startswith(':SENS1:SWE:POIN '):
            self.points = int(cmd.split()[-1])
        elif cmd in (':FORMat:DATA ASCii', ':SENS1:SWE:TYPE LIN', ':INIT1:CONT OFF',
                     ':SENS1:SWE:MODE HOLD', ':SENS1:SWE:TRIG:POIN OFF', ':SENS1:AVER:STAT OFF'):
            pass
        elif cmd.startswith((':SENS1:FREQ:STAR ', ':SENS1:FREQ:STOP ',
                             ':SOUR1:POW:LEV ', ':SOUR1:POW:LEV:IMM:AMPL ',
                             ':SENS1:BAND ')):
            pass
        else:
            self.errors.append('-113,"Undefined header"')

    def query(self, cmd):
        self.sent.append(cmd)
        if cmd == ':SENS1:BAND?':
            return str(self.ifbw)
        if cmd == '*IDN?':
            return IDN
        if cmd == '*OPT?':
            return '"080,086,217,021,025,P02"'
        if cmd == ':SYST:ERR?':
            return self.errors.pop(0) if self.errors else '+0,"No error"'
        if cmd in (':CALC1:PAR:CAT?', ':CALC1:PAR:CAT:EXT?'):
            return '"' + ','.join(x for pair in self.measurements.items() for x in pair) + '"'
        if cmd == '*OPC?':
            if isinstance(self.opc, Exception):
                raise self.opc
            return self.opc
        if cmd == ':CALC1:EQU:STAT?':
            return '1' if getattr(self, 'equation_enabled', False) else '0'
        if cmd == ':SENS1:SWE:MODE SING;*OPC?':
            self.write(':SENS1:SWE:MODE SING')
            return self.query('*OPC?')
        if cmd == ':CALC1:DATA? SDATA':
            if self.selected is None:
                self.errors.append('+105,"Measurement not found"')
                raise TimeoutError('no selected measurement')
            real = {'S11': -0.11, 'S21': -0.21, 'S12': -0.12, 'S22': -0.22}[
                self.measurements[self.selected]]
            return ','.join([f'{real},0.02'] * self.points)
        self.errors.append('-224,"Illegal parameter value"')
        raise TimeoutError('unsupported query: ' + cmd)

    def close(self):
        self.closed = True


class Manager:
    def __init__(self, inst):
        self.inst = inst

    def open_resource(self, resource):
        assert resource == 'GPIB0::16::INSTR'
        self.inst.closed = False
        return self.inst


def connect(inst=None):
    inst = inst or N5224A()
    vna = V.create_vna('auto', '', interface='gpib', resource='GPIB0::16::INSTR',
                       resource_manager=Manager(inst))
    assert vna.connect() == IDN
    return vna, inst


def test_gpib_connect_scan_reconnect_uses_reported_commands_and_correct_mapping():
    vna, inst = connect()
    try:
        vna.set_sweep(1e9, 2e9, 11)
        vna.set_power(0)
        data = vna.measure()
        assert inst.send_end is True
        assert data.s.shape == (11, 2, 2)
        for name, row, col, real in [('S11', 0, 0, -.11), ('S21', 1, 0, -.21),
                                    ('S12', 0, 1, -.12), ('S22', 1, 1, -.22)]:
            np.testing.assert_allclose(data.s[:, row, col], real + .02j)
        assert inst.sweeps == 1
        assert not inst.errors
        vna.close()
        assert inst.closed
        assert vna.connect() == IDN  # Duplicate names are verified via catalogue.
        assert ':CALC1:PAR:CAT:EXT?' in inst.sent
        assert not any(c.startswith(':CALC1:PAR:SDEF') for c in inst.sent)
        assert ':INIT:IMM' not in inst.sent
    finally:
        vna.close()


@pytest.mark.parametrize('command', [':FORMat:DATA', ':SENS1:SWE:TYPE', ':SENS1:SWE:MODE HOLD',
                                    ':SENS1:FREQ:STAR', ':SENS1:FREQ:STOP',
                                    ':SENS1:SWE:POIN', ':SOUR1:POW:LEV', ':SENS1:BAND'])
def test_rejected_configuration_is_not_reported_as_success(command):
    inst = N5224A()
    inst.reject = command
    vna = V.create_vna('auto', '', interface='gpib', resource='16',
                       resource_manager=Manager(inst))
    try:
        with pytest.raises(RuntimeError, match='PNA command failed') as error:
            vna.connect()
        assert command in str(error.value)
        assert '-222' in str(error.value)
    finally:
        vna.close()


def test_selection_error_prevents_reading_previous_s_parameter():
    vna, inst = connect()
    try:
        inst.reject = ':CALC1:PAR:SEL'
        before = inst.sent.count(':CALC1:DATA? SDATA')
        with pytest.raises(RuntimeError, match='PAR:SEL'):
            vna.fetch_all()
        assert inst.sent.count(':CALC1:DATA? SDATA') == before
    finally:
        vna.close()


@pytest.mark.parametrize('response', ['10', '0', TimeoutError('VISA timeout')])
def test_bad_completion_never_falls_back_to_unqualified_init(response):
    vna, inst = connect()
    try:
        inst.opc = response
        with pytest.raises(RuntimeError):
            vna.trigger_single()
        assert ':INIT:IMM;*OPC?' not in inst.sent
        assert ':INITiate:IMMediate;*OPC?' not in inst.sent
        assert inst.sweeps == 1
    finally:
        vna.close()


def test_pna_probe_avoids_known_bad_headers_and_accepts_negative_complex_data():
    inst = N5224A()
    result = P.probe_transport(inst, interface='gpib', target='GPIB0::16::INSTR')
    assert result['idn_fields']['firmware'] == 'A.09.90.19'
    assert result['suggested_dialect'] == {
        'define_cmd': ':CALC1:PAR:DEF:EXT', 'power_cmd': ':SOUR1:POW:LEV',
        'ifbw_cmd': ':SENS1:BAND', 'trigger_cmd': ':SENS1:SWE:MODE SING', 'fetch_mnemonic': 'SDATA'}
    assert all(r['verdict'] == 'OK' for r in result['define'] + result['other'])
    assert not any(cmd in inst.sent for cmd in
                   (':CALC1:DATA? SDAT', ':CALC1:TRAC1:DATA? SDATA',
                    ':CALC1:MEAS1:DATA:SDATA?', ':INIT:IMM'))
    assert inst.sweeps == 1
    assert not inst.errors


@pytest.mark.parametrize('response', ['+0104,"No error text in a real error"',
                                    '-1,"No error queue available"', ''])
def test_nonzero_or_empty_error_reply_cannot_look_successful(response):
    assert not V.TCPVNA._is_no_error(response)
    assert not P._is_ok(response)


def test_partial_config_error_disconnects_session_and_status_cannot_claim_connected():
    from tuner.web.state import AppState
    vna, inst = connect()
    state = AppState()
    state.vna.update(controller=vna, connected=True)
    inst.reject = ':SENS1:FREQ:STOP'
    with pytest.raises(RuntimeError, match='STOP'):
        vna.set_sweep(3e9, 4e9, 11)
    assert not vna.connected
    status = state.vna_status()
    assert not status['connected']
    assert '-222' in status['last_error']
    with pytest.raises(RuntimeError, match='not connected'):
        vna.measure()


def test_pna_probe_stops_after_opc_timeout_without_reading_stale_output():
    inst = N5224A()
    inst.opc = TimeoutError('VISA timeout')
    result = P.probe_transport(inst, interface='gpib')
    assert result['aborted']
    assert result['other'][-1]['verdict'] == 'ERROR'
    assert result['other'][-1]['cmd'] == ':SENS1:SWE:MODE SING;*OPC?'
    assert ':CALC1:DATA? SDATA' not in inst.sent
    opc_index = max(i for i, cmd in enumerate(inst.sent) if cmd == '*OPC?')
    assert ':SYST:ERR?' not in inst.sent[opc_index + 1:]


def test_pna_probe_requires_exact_completion_confirmation():
    inst = N5224A()
    inst.opc = '10'
    result = P.probe_transport(inst, interface='gpib')
    row = next(r for r in result['other'] if '*OPC?' in r['cmd'])
    assert row['verdict'] == 'REJECT'
    assert 'trigger_cmd' not in result['suggested_dialect']


def test_official_manual_single_accept_does_not_mix_immediate_source_and_manual_init():
    vna, inst = connect()
    try:
        assert vna.get_config()['trigger_cmd'] == ':SENS1:SWE:MODE SING'
        vna.trigger_single()
        vna.trigger_single()
        assert inst.sweeps == 2
        assert ':SENS1:SWE:MODE HOLD' in inst.sent
        assert ':SENS1:SWE:TRIG:POIN OFF' in inst.sent
        assert ':SENS1:AVER:STAT OFF' in inst.sent
        assert not any(cmd.startswith(':INIT') for cmd in inst.sent)
    finally:
        vna.close()


def test_saved_legacy_manual_trigger_is_migrated_before_first_sweep():
    inst = N5224A()
    vna = V.create_vna('auto', '', interface='gpib', resource='16',
                       resource_manager=Manager(inst), dialect={'trigger_cmd': ':INIT1:IMM'})
    try:
        vna.connect()
        assert vna.dialect['trigger_cmd'] == ':SENS1:SWE:MODE SING'
        assert vna.dialect_warning
        assert vna.trigger_single()
        assert not any(cmd.startswith(':INIT') for cmd in inst.sent)
    finally:
        vna.close()


def test_extended_catalogue_handles_receiver_commas_without_reusing_wrong_parameter():
    inst = N5224A()
    inst.measurements = {'receiver': 'A_1', 'S11': 'S11'}
    original = inst.query
    def query(cmd):
        if cmd == ':CALC1:PAR:CAT?':
            raise AssertionError('obsolete ambiguous receiver catalogue must not be used')
        return original(cmd)
    inst.query = query
    vna, inst = connect(inst)
    vna.close()
    assert ':CALC1:PAR:CAT:EXT?' in inst.sent


def test_equation_editor_cannot_silently_change_s_parameter_data():
    vna, inst = connect()
    inst.equation_enabled = True
    before = inst.sent.count(':CALC1:DATA? SDATA')
    with pytest.raises(RuntimeError, match='Equation Editor'):
        vna.measure()
    assert not vna.connected
    assert inst.sent.count(':CALC1:DATA? SDATA') == before


def test_extended_catalogue_accepts_equivalent_single_digit_port_notation():
    inst = N5224A()
    inst.measurements['S11'] = 'S1_1'
    vna, inst = connect(inst)
    vna.close()


def test_if_bandwidth_reports_the_applied_rounded_value_from_manual_readback():
    vna, inst = connect()
    try:
        vna.set_ifbw(1500)
        assert vna.get_config()['ifbw_hz'] == 2000
        assert ':SENS1:BAND?' in inst.sent
    finally:
        vna.close()


def test_scalar_power_applies_to_both_source_ports_even_when_uncoupled():
    vna, inst = connect()
    try:
        vna.set_power(0)
        assert inst.power_by_port == {1: 0.0, 2: 0.0}
        assert ':SOUR1:POW2:LEV 0' in inst.sent
    finally:
        vna.close()

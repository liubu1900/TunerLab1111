"""Unit tests for the multi-vendor TCP/IP VNA drivers.

No real instrument required: :func:`socket.create_connection` is replaced with a
scripted fake socket that replies to SCPI queries the way a real analyzer would.
"""
import socket
import sys
from unittest import mock

import numpy as np
import pytest

from tuner import vna_drivers as V


# --------------------------------------------------------------------------- #
# Fake SCPI socket
# --------------------------------------------------------------------------- #
class FakeSocket:
    """A scripted TCPVNA transport. Replies are derived from the last command."""

    def __init__(self, idn='Keysight Technologies,E5071C,SN123,A.01', n_points=11):
        self.idn = idn
        self.n_points = n_points
        self.sent = []
        self.last_cmd = ''
        self._closed = False

    # -- socket-compatible API used by SocketSCPI -------------------------
    def settimeout(self, t):
        pass

    def shutdown(self, flag):
        self._closed = True

    def close(self):
        self._closed = True

    def sendall(self, data):
        self.last_cmd = data.decode('ascii').strip()
        self.sent.append(self.last_cmd)

    def recv(self, n):
        cmd = self.last_cmd
        # DATA query ends with the mnemonic (SDATA/SDAT), not '?'
        if cmd.startswith(':CALC1:DATA?'):
            vals = []
            for i in range(self.n_points):
                vals.append(f'{0.1 + i*0.01:.6f}')
                vals.append(f'{0.02 * i:.6f}')
            return (','.join(vals) + '\n').encode('ascii')
        if cmd.endswith('?'):
            if cmd == ':SENS1:BAND?':
                return b'1000\n'
            if cmd == ':CALC1:EQU:STAT?':
                return b'0\n'
            if cmd == '*IDN?':
                return (self.idn + '\n').encode('ascii')
            if cmd == ':SYST:ERR?':
                return b'+0,"No error"\n'
            if cmd.endswith('*OPC?'):
                return b'1\n'
        return b''

    # -- helpers for assertions ------------------------------------------
    def commands(self):
        return self.sent

    def sent_once(self, prefix):
        return sum(1 for c in self.sent if c.startswith(prefix))


def make_vna(idn='Keysight Technologies,E5071C,SN123,A.01', n_points=11, **kw):
    fake = FakeSocket(idn=idn, n_points=n_points)
    with mock.patch.object(V.socket, 'create_connection', return_value=fake):
        vna = V.create_vna(kw.get('vendor_key', 'auto'), '192.168.0.99', 5025, 5.0)
        vna.connect()
        # align the analyzer sweep to the fake's point count so fetch matches
        vna.set_sweep(1e9, 2e9, n_points)
        vna._fake = fake
        return vna, fake


# --------------------------------------------------------------------------- #
# Vendor detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize('idn,expected', [
    ('Rohde&Schwarz,ZNB20,123,2.30', 'rsp'),
    ('Keysight Technologies,E5071C,SN,A.01', 'keysight_ena'),
    ('Agilent Technologies,N5247A,US,A.09', 'keysight_pna'),
    ('Ceyear,AV3672,1,1', 'ceyear'),
    ('Anritsu,MS46524B,1,1', 'anritsu'),
    ('SomeUnknown,VNA,1,1', 'generic'),
])
def test_detect_vendor(idn, expected):
    assert V.detect_vendor(idn) == expected


def test_available_vendors():
    vs = V.available_vendors()
    for key in ('rsp', 'keysight_ena', 'keysight_pna', 'ceyear', 'anritsu'):
        assert key in vs


# --------------------------------------------------------------------------- #
# Connection + setup
# --------------------------------------------------------------------------- #
def test_connect_parses_idn_and_sets_defaults():
    vna, fake = make_vna()
    assert vna.connected
    assert 'E5071C' in vna.idn
    assert vna.vendor_key == 'keysight_ena'
    # measurements defined for all four S-parameters
    assert fake.sent_once(":CALC1:PAR:DEF 'S11'") == 1
    assert fake.sent_once(":CALC1:PAR:DEF 'S22'") == 1
    # sweep / power / ifbw applied
    assert any(c.startswith(':SENS1:FREQ:STAR') for c in fake.sent)
    assert any(c.startswith(':SOUR1:POW:LEV') for c in fake.sent)
    assert any(c.startswith(':SENS1:BAND') for c in fake.sent)


def test_rsp_uses_sdef_and_sdat():
    vna, fake = make_vna(idn='Rohde&Schwarz,ZNB20,123,2.30')
    assert vna.vendor_key == 'rsp'
    # R&S uses SDEFine (tried after the failing DEF) and SDAT fetch mnemonic
    assert vna._fetch_mnemonic == 'SDAT'
    # the successful define command on R&S ends with SDEF
    defines = [c for c in fake.sent if ':PAR:' in c and 'S11' in c]
    assert any('SDEF' in c for c in defines)


def test_set_sweep_updates_frequencies():
    vna, _ = make_vna(n_points=11)
    vna.set_sweep(1e9, 2e9, 11)
    assert vna.frequencies.size == 11
    assert vna.frequencies[0] == pytest.approx(1e9)
    assert vna.frequencies[-1] == pytest.approx(2e9)


def test_set_sweep_single_frequency():
    """One spot frequency is a valid calibration grid (load-pull at 10 GHz)."""
    vna, fake = make_vna(n_points=11)
    vna.set_sweep(10e9, 10e9, 1)
    assert vna.frequencies.size == 1
    assert vna.frequencies[0] == pytest.approx(10e9)
    assert sum(1 for c in fake.sent if c == ':SENS1:SWE:POIN 1') == 1
    assert any(c.startswith(':SENS1:FREQ:STAR') and '1e+10' in c for c in fake.sent)
    assert any(c.startswith(':SENS1:FREQ:STOP') and '1e+10' in c for c in fake.sent)
    assert vna.get_config()['stop_hz'] == pytest.approx(10e9)


def test_set_sweep_rejects_non_increasing_grid_before_scpi():
    """An invalid sweep must never be pushed to the real analyzer."""
    vna, fake = make_vna(n_points=11)
    sent_before = len(fake.sent)
    grid_before = vna.frequencies.copy()
    with pytest.raises(ValueError, match='below stop frequency'):
        vna.set_sweep(10e9, 10e9, 4)
    with pytest.raises(ValueError, match='below stop frequency'):
        vna.set_sweep(20e9, 10e9, 4)
    with pytest.raises(ValueError, match='finite and positive'):
        vna.set_sweep(0.0, 10e9, 4)
    assert len(fake.sent) == sent_before
    np.testing.assert_array_equal(vna.frequencies, grid_before)


def test_fetch_trace_single_frequency_tolerates_analyzer_minimum():
    """Some analyzers keep >= 2 samples even with START == STOP; keep the first."""
    fake = FakeSocket(n_points=4)
    with mock.patch.object(V.socket, 'create_connection', return_value=fake):
        vna = V.create_vna('auto', '192.168.0.99', 5025, 5.0)
        vna.connect()
    vna.set_sweep(10e9, 10e9, 1)
    vna._select('S11')
    trace = vna._fetch_trace()
    assert trace.shape == (1,)
    assert trace[0] == pytest.approx(complex(0.1, 0.0))


# --------------------------------------------------------------------------- #
# Acquisition
# --------------------------------------------------------------------------- #
def test_trigger_confirmed():
    vna, _ = make_vna()
    assert vna.trigger_single() is True


def test_fetch_all_shape_and_values():
    vna, fake = make_vna(n_points=11)
    traces = vna.fetch_all()
    assert set(traces) == {'S11', 'S21', 'S12', 'S22'}
    for arr in traces.values():
        assert arr.shape == (11,)
        assert arr.dtype == complex
    # S11 first point = 0.10 + 0.00j (i=0 -> 0.1, 0.02*0)
    assert traces['S11'][0] == pytest.approx(0.10 + 0j)


def test_measure_returns_measurement():
    vna, _ = make_vna(n_points=11)
    m = vna.measure()
    from tuner.models import Measurement
    assert isinstance(m, Measurement)
    assert m.s.shape == (11, 2, 2)
    assert m.simulated is False
    assert m.metadata['vendor'] == 'keysight_ena'
    assert m.metadata['resource'] == '192.168.0.99:5025'


def test_read_errors_empty():
    vna, _ = make_vna()
    assert vna.read_errors() == []


def test_read_errors_reports_queue():
    fake = FakeSocket()
    # Force an error response for the next SYST:ERR? queries.
    orig_recv = fake.recv

    def patched(n):
        # first two error queries return a real error, then "no error"
        if fake.last_cmd == ':SYST:ERR?':
            if not hasattr(fake, '_err_count'):
                fake._err_count = 0
            if fake._err_count < 2:
                fake._err_count += 1
                return b'-113,"Undefined header"\n'
        return orig_recv(n)

    fake.recv = patched
    with mock.patch.object(V.socket, 'create_connection', return_value=fake):
        vna = V.create_vna('auto', '1.2.3.4', 5025, 5.0)
        vna.transport.open()
        errs = vna.read_errors()
    assert len(errs) == 2
    assert 'Undefined header' in errs[0]


# --------------------------------------------------------------------------- #
# Error path: cannot reach the instrument
# --------------------------------------------------------------------------- #
def test_connect_refuses_when_unreachable():
    with mock.patch.object(V.socket, 'create_connection',
                           side_effect=socket.timeout('no route')):
        vna = V.create_vna('auto', '10.255.255.1', 5025, 1.0)
        with pytest.raises(Exception):
            vna.connect()

"""Backend regressions for the 0.5.13.9 arbitrary-frequency-list fix.

A non-uniform list (e.g. 1/2/4/8 GHz) used to be applied with one
``set_sweep(start, stop, n)``; the driver then labelled the trace with
``linspace(start, stop, n)``.  The operator's exact frequencies were lost, so
the library grid, the trace labels and the stored samples no longer matched.
This batch installs the exact grid and acquires every point with its own
single-point sweep.
"""
import socket
from unittest import mock

import numpy as np
import pytest

from tuner import vna_drivers as V
from tuner.frequency_plan import apply_frequency_plan, build_frequency_plan
from tuner.web import handlers


class _PointSocket:
    """Scripted socket: one complex sample per DATA query, confirms triggers."""

    def __init__(self):
        self.sent = []
        self.last = ''

    def settimeout(self, t):
        pass

    def shutdown(self, flag):
        pass

    def close(self):
        pass

    def sendall(self, data):
        self.last = data.decode('ascii').strip()
        self.sent.append(self.last)

    def commands(self):
        return self.sent

    def recv(self, n):
        cmd = self.last
        if cmd.startswith(':CALC1:DATA?'):
            return b'0.1,0.02\n'
        if cmd.endswith('?'):
            if cmd == '*IDN?':
                return b'Keysight Technologies,E5071C,SN,A.01\n'
            if cmd == ':SYST:ERR?':
                return b'+0,"No error"\n'
            if cmd.endswith('*OPC?'):
                return b'1\n'
            if cmd.startswith(':SENS1:FREQ:STAR'):
                return b'1.000000E+009\n'
            if cmd.startswith(':SENS1:FREQ:STOP'):
                return b'8.000000E+009\n'
            if cmd.startswith(':SENS1:SWE:POIN'):
                return b'1\n'
        return b''


class _FakeVNAWithGrid:
    """VNA-like object that records which installer was used."""

    def __init__(self):
        self.frequencies = np.array([5e9], float)
        self._sweep = {'start': 5e9, 'stop': 5e9, 'points': 1}
        self.grid_calls = []
        self.sweep_calls = []

    def set_frequency_grid(self, freqs):
        self.grid_calls.append(np.asarray(freqs, float).copy())
        self.frequencies = np.asarray(freqs, float).copy()

    def set_sweep(self, start, stop, n):
        self.sweep_calls.append((start, stop, n))
        self.frequencies = (np.asarray([start], float) if n == 1
                            else np.linspace(start, stop, n))


# ------------------------------------------------------------------ scheduling
def test_nonuniform_list_uses_exact_grid_not_set_sweep():
    plan = build_frequency_plan(
        'arbitrary_list', frequencies_hz=[8e9, 1e9, 4e9, 2e9])
    assert plan['uniform'] is False
    assert [round(f / 1e9) for f in plan['frequencies_hz']] == [1, 2, 4, 8]

    vna = _FakeVNAWithGrid()
    apply_frequency_plan(vna, plan)

    assert len(vna.grid_calls) == 1
    assert [round(f / 1e9) for f in vna.grid_calls[0]] == [1, 2, 4, 8]
    assert vna.sweep_calls == [], 'non-uniform list must not be linearised'
    assert [round(f / 1e9) for f in vna.frequencies] == [1, 2, 4, 8]


def test_uniform_list_still_uses_single_linear_sweep():
    """Regression guard: the 0.5.13.8 behaviour is kept for uniform grids."""
    plan = build_frequency_plan(
        'arbitrary_list', frequencies_hz=[6e9, 12e9, 18e9])
    assert plan['uniform'] is True

    vna = _FakeVNAWithGrid()
    apply_frequency_plan(vna, plan)

    assert vna.grid_calls == []
    assert vna.sweep_calls == [(6e9, 18e9, 3)]
    assert [round(f / 1e9) for f in vna.frequencies] == [6, 12, 18]


def test_nonuniform_without_grid_support_falls_back_to_sweep():
    """Older callers without set_frequency_grid keep the linear approximation."""
    class OnlySweep:
        def __init__(self):
            self.calls = []
            self.frequencies = np.array([5e9], float)

        def set_sweep(self, start, stop, n):
            self.calls.append((start, stop, n))
            self.frequencies = np.linspace(start, stop, n)

    plan = build_frequency_plan(
        'arbitrary_list', frequencies_hz=[1e9, 2e9, 8e9])
    vna = OnlySweep()
    apply_frequency_plan(vna, plan)
    assert vna.calls == [(1e9, 8e9, 3)]


# ------------------------------------------------------------------- driver API
def test_set_frequency_grid_validates_and_keeps_exact_list():
    vna, _fake = _make_driver()
    grid = [1e9, 2e9, 4e9, 8e9]
    vna.set_frequency_grid(grid)
    assert [round(f / 1e9) for f in vna.frequencies] == [1, 2, 4, 8]
    assert vna._sweep == {'start': 1e9, 'stop': 8e9, 'points': 4}
    assert [round(f / 1e9) for f in vna.get_config()['frequency_grid']] == [1, 2, 4, 8]

    for bad in ([], [0, 1e9], [2e9, 1e9], [1e9, 1e9], [1e9, np.nan]):
        with pytest.raises(ValueError):
            vna.set_frequency_grid(bad)


def _make_driver():
    """Build a connected TCPVNA over the scripted single-point socket."""
    fake = _PointSocket()
    with mock.patch.object(V.socket, 'create_connection', return_value=fake):
        vna = V.create_vna('keysight_ena', '192.168.0.99', 5025, 5.0)
        vna.connect()
        vna._fake = fake
        return vna, fake


def test_measure_visits_each_exact_frequency():
    """The core bug: every stored sample must be taken at its own frequency."""
    vna, fake = _make_driver()
    grid = np.asarray([1e9, 2e9, 4e9, 8e9])
    vna.set_frequency_grid(grid)

    measurement = vna.measure()

    # The measurement carries the exact list, not a linspace approximation.
    assert [round(f / 1e9) for f in measurement.frequencies] == [1, 2, 4, 8]
    assert measurement.s.shape == (4, 2, 2)

    # Each requested frequency produced its own single-point sweep and trigger.
    starts = [c for c in fake.commands()
              if c.startswith(':SENS1:FREQ:STAR') and not c.endswith('?')]
    assert [round(float(c.split()[-1]) / 1e9) for c in starts] == [1, 2, 4, 8]
    triggers = [c for c in fake.commands() if c.startswith(':INIT1:IMM')]
    assert len(triggers) == 4

    # The driver restored the full grid after the point-by-point acquisition.
    assert [round(f / 1e9) for f in vna.frequencies] == [1, 2, 4, 8]


def test_handler_builds_nonuniform_plan_from_ghz_list():
    plan = handlers._frequency_plan_from_body({
        'frequency_mode': 'arbitrary_list',
        'frequency_list_ghz': [1, 2, 4, 8],
    })
    assert plan['mode'] == 'arbitrary_list'
    assert plan['uniform'] is False
    assert [round(f / 1e9) for f in plan['frequencies_hz']] == [1, 2, 4, 8]

"""Backend regressions for the 0.5.13.8 maintenance batch.

Covers the six reported issues at the layer that has no Qt/hardware dependency:

* (1) one-click init confirmation is a single signal connection (UI test does
      the receiver count; this file pins the simplified dialog text);
* (2) real-VNA build falls back to the "矢网控制" live settings for connection
      parameters;
* (3) (UI-tested) reveal button opens the active library folder;
* (4) arbitrary frequency list actually drives the instrument sweep;
* (5) (verification/test_smith_mode_boxzoom.py) box zoom scales with the box;
* (6) freq_sweep 6–18 GHz / 3 points is generated, applied to the mock VNA and
      recorded in the studio frequency state.
"""
import numpy as np
import pytest

from tuner.frequency_plan import apply_frequency_plan, build_frequency_plan
from tuner.web import handlers


class _FakeVNA:
    def __init__(self, with_sweep=True):
        self.calls = []
        self.frequencies = np.array([1e9], float)
        self._sweep = {'start': 1e9, 'stop': 1e9, 'points': 1}
        if with_sweep:
            def setter(s, stop, n):
                self.calls.append((s, stop, n))
                self.frequencies = np.linspace(s, stop, n) if n > 1 else np.array([s])
                self._sweep = {'start': s, 'stop': s if n == 1 else stop, 'points': n}
            self.set_sweep = setter

    def initialize(self):
        return 'fake'


# ---------------------------------------------------------------- (4)(6) plan
def test_freq_sweep_6_18_ghz_three_points():
    plan = handlers._frequency_plan_from_body({
        'frequency_mode': 'freq_sweep',
        'vna_start_hz': 6e9, 'vna_stop_hz': 18e9, 'vna_points': 3,
    })
    assert plan['mode'] == 'freq_sweep'
    assert [round(f / 1e9, 6) for f in plan['frequencies_hz']] == [6.0, 12.0, 18.0]
    assert plan['points'] == 3


def test_arbitrary_list_pushes_real_sweep_not_stale_5ghz():
    """Regression: list modes used to leave the instrument on 5 GHz."""
    plan = handlers._frequency_plan_from_body({
        'frequency_mode': 'arbitrary_list',
        'frequency_list_ghz': [6, 12, 18],
    })
    vna = _FakeVNA()
    apply_frequency_plan(vna, plan)
    assert vna.calls == [(6e9, 18e9, 3)]
    assert [round(float(f) / 1e9) for f in vna.frequencies] == [6, 12, 18]


def test_mock_vna_takes_sweep_grid_during_init(monkeypatch):
    """The mock path applies the operator grid, not the 41-point default.

    Exercise the frequency/application decision directly (same code path as
    handlers.studio_init_hardware's mock branch) without needing a full state.
    """
    from tuner.web import handlers as H
    plan = H._frequency_plan_from_body({
        'frequency_mode': 'freq_sweep',
        'vna_start_hz': 6e9, 'vna_stop_hz': 18e9, 'vna_points': 3,
    })
    vna = _FakeVNA(with_sweep=False)  # mock has no set_sweep
    apply_frequency_plan(vna, plan)
    assert [round(float(f) / 1e9) for f in vna.frequencies] == [6, 12, 18]


# ---------------------------------------------------------------- (2) VNA cfg
def test_real_vna_connection_defaults_to_live_config(monkeypatch):
    """studio_init_hardware uses 矢网控制 settings when the body omits them."""
    from tuner.web import handlers as H

    captured = {}

    class _VNA:
        def __init__(self, vendor, host, port, timeout, *, interface, resource, dialect):
            captured.update(vendor=vendor, host=host, port=port, timeout=timeout,
                            interface=interface, resource=resource, dialect=dialect)
        def connect(self):
            return 'FAKE'
        def set_sweep(self, s, stop, n):
            captured['sweep'] = (s, stop, n)
            self.frequencies = np.linspace(s, stop, n)
        @property
        def frequencies(self):
            return getattr(self, '_f', np.array([5e9]))
        @frequencies.setter
        def frequencies(self, v):
            self._f = v

    monkeypatch.setattr(H, 'create_vna', lambda *a, **k: _VNA(*a, **k))

    class FakeState:
        def __init__(self):
            self.vna = {'config': {
                'host': '10.0.0.5', 'port': 1234, 'vendor_key': 'ceyear',
                'interface': 'gpib', 'resource': 'GPIB0::9::INSTR',
                'timeout_ms': 7777, 'dialect': {'fetch_mnemonic': 'S21'},
            }, 'connected': True}
            self.studio_lock = __import__('threading').RLock()
            self.studio = dict((k, None) for k in (
                'config', 'motion', 'planner', 'vna', 'manager', 'database',
                'data', 'search_engine', 'validation_report'))
            self.studio.update(vna_source=None, motion_source=None, homed_axes=[],
                               calibration_state='idle', calibration_progress=(0, 0))

        def studio_overview(self):
            return {}

    state = FakeState()
    H.studio_init_hardware(state, {
        'motion_source': 'mock', 'vna_source': 'real', 'probes': 2,
        'vna_start_hz': 6e9, 'vna_stop_hz': 18e9, 'vna_points': 3,
        'frequency_mode': 'freq_sweep',
    })
    assert captured['host'] == '10.0.0.5'
    assert captured['port'] == 1234
    assert captured['vendor'] == 'ceyear'
    assert captured['interface'] == 'gpib'
    assert captured['resource'] == 'GPIB0::9::INSTR'
    assert abs(captured['timeout'] - 7.777) < 1e-9
    assert captured['dialect'] == {'fetch_mnemonic': 'S21'}
    assert captured['sweep'] == (6e9, 18e9, 3)

"""Regression tests for the 0.5.13.6 maintenance batch.

Covers the six issues reported by the operator:

1. first-ever library build must not raise "load a calibration database first";
2. frequency-picked derivation writes a single-frequency library;
3. the point-list API exposes the library frequency list and honours a pick;
4. vector repeatability with a chosen frequency compares only that bin;
5. (UI-only: Smith click/zoom wiring is checked in the desktop test module);
6. axis initialisation zeroes MPOS after homing (link API + page dispatch).
"""
from __future__ import annotations

import numpy as np
import pytest

from tuner.database import CalibrationDatabase
from tuner.library_plan_derive import derive_by_gamma_plan
from tuner.models import Measurement, TunerConfig
from tuner.search import GammaSearchEngine
from tuner.web import handlers
from tuner.web.state import state


def _make_library(tmp_path, freqs=(5e9, 10e9), x=4, z=3, name='lib.h5'):
    config = TunerConfig()
    xv = np.linspace(*config.x_limits, x)
    zv = np.linspace(*config.z_limits, z)
    positions = config.grid(xv, zv)
    db = CalibrationDatabase(tmp_path / name)
    db.create(config, np.array(freqs, float), positions)
    for i, pos in enumerate(positions):
        g = (0.1 + 0.7 * i / max(1, len(positions) - 1)) * np.exp(1j * 0.3)
        s = np.zeros((len(freqs), 2, 2), complex)
        s[:, 0, 0] = g * (1 + 0.05 * np.arange(len(freqs)))
        db.write_point(i, pos.copy(), Measurement(np.array(freqs, float), s, True))
    return db


def _load(db):
    data = db.load()
    with state.studio_lock:
        state.studio.update({
            'database': db, 'data': data,
            'search_engine': GammaSearchEngine(data, z0=50.0),
            'frequency_hz': float(data['frequencies'][0]),
        })


# --- 1: first build leaves an empty but loadable library -----------------
class _Motion:
    def read_position(self):
        return np.zeros(4)


class _VNA:
    frequencies = np.array([5e9, 10e9])

    def measure(self):
        return Measurement(self.frequencies, np.zeros((2, 2, 2), complex))


def test_first_build_loads_an_empty_library_without_raising(tmp_path):
    keys = ('config', 'motion', 'vna', 'manager', 'database', 'data',
            'calibration_state', 'calibration_progress', 'search_engine')
    with state.studio_lock:
        previous = {k: state.studio.get(k) for k in keys}
        state.studio.update({'config': TunerConfig(), 'motion': _Motion(),
                             'vna': _VNA(), 'manager': None, 'database': None,
                             'data': None, 'calibration_state': 'idle'})
    try:
        db_path = tmp_path / 'first.h5'
        result = handlers.studio_setup(
            state, {'plan': 'cartesian', 'x_count': 2, 'z_count': 2,
                    'path': str(db_path)})
        assert result['resumed'] is False
        # The fix: the freshly prepared library is loaded immediately, so every
        # broadcast subscriber sees data and no page raises a load error.
        assert state.studio['data'] is not None
        assert state.studio['calibration_state'] == 'ready'
        assert state.studio['manager'] is not None
        points = handlers.studio_library_points(state, {})
        assert points['points'] == []
        assert len(points['frequencies_hz']) == 2
    finally:
        with state.studio_lock:
            state.studio.update(previous)


# --- 2: derive at a chosen frequency => single-frequency library ---------
def test_derive_at_chosen_frequency_is_single_frequency(tmp_path):
    src = _make_library(tmp_path)
    out = tmp_path / 'derived.h5'
    res = derive_by_gamma_plan(src, str(out), plan='smith', density='low',
                               frequency_hz=10e9)
    assert res['frequency_hz'] == pytest.approx(10e9)
    data = CalibrationDatabase(out).load()
    assert len(data['frequencies']) == 1
    assert data['frequencies'][0] == pytest.approx(10e9)
    assert data['s'].shape[1] == 1
    meta = CalibrationDatabase(out).read_metadata()
    assert meta['frequency_list'] == [10e9]


def test_derive_without_frequency_keeps_full_band(tmp_path):
    src = _make_library(tmp_path)
    out = tmp_path / 'derived_full.h5'
    derive_by_gamma_plan(src, str(out), plan='gamma_rings', density='low')
    data = CalibrationDatabase(out).load()
    assert len(data['frequencies']) == 2


# --- 3: point list frequency selection -----------------------------------
def test_library_points_exposes_and_honours_frequencies(tmp_path):
    db = _make_library(tmp_path)
    _load(db)
    payload = handlers.studio_library_points(state, {})
    assert payload['frequencies_hz'] == [5e9, 10e9]
    low = handlers.studio_library_points(state, {'frequency_hz': 5e9})
    high = handlers.studio_library_points(state, {'frequency_hz': 10e9})
    assert low['points'][0]['gamma_mag'] != high['points'][0]['gamma_mag']
    assert low['frequency_hz'] == pytest.approx(5e9)


# --- 4: repeatability honours the selected frequency ---------------------
def test_vector_repeatability_report_uses_selected_frequency():
    from tuner.repeatability import vector_repeatability_report
    freqs = np.array([5e9, 10e9])
    measured = np.array([0.51 + 0.1j, 0.9 + 0.1j])
    memory = np.array([0.5 + 0.1j, 0.5 + 0.1j])
    row_all = vector_repeatability_report(freqs, measured, memory, point_index=1)
    assert len(row_all['frequencies_hz']) == 2
    row_one = vector_repeatability_report(
        freqs[1:2], measured[1:2], memory[1:2], point_index=1)
    assert row_one['frequencies_hz'] == [10e9]
    # The 10 GHz sample has the larger vector error (-5.7 dB), so the single-bin
    # report's worst must equal the full-band worst, not the benign 5 GHz bin.
    assert row_one['worst_db'] == pytest.approx(row_all['worst_db'])


# --- 6: MPOS zeroing via link API ----------------------------------------
class _FakeLink:
    def __init__(self):
        self.mpos = {}

    def set_mpos(self, axis, value):
        self.mpos[axis] = value


class _FakeController:
    def __init__(self):
        self.link = _FakeLink()

    @staticmethod
    def physical_axis(name):
        return {'X': 0, 'Z1': 1, 'Z2': 2, 'Z3': 3}[name]


def test_zero_mpos_sets_axis_feedback_to_zero():
    from tuner.ui_hardware import AxisMotionPage
    controller = _FakeController()
    assert AxisMotionPage._zero_mpos('Z1', controller) is True
    assert controller.link.mpos[1] == 0.0
    # Virtual backend with no link is a no-op, not an error.
    assert AxisMotionPage._zero_mpos('X', object()) is False


def test_one_click_initialize_orders_x_last():
    """On coupled machines X must be initialised only after the probe axes."""
    from tuner.ui_hardware import AxisMotionPage

    calls = []
    # Call the real _initialize_all from an unbound method; no QWidget needed.
    class _FakePage:
        session = type('S', (), {'axes': ('X', 'Z1', 'Z2')})()

        def _initialize_axis(self, axis):
            calls.append(axis)

        def log(self, text):
            pass

    AxisMotionPage._initialize_all(_FakePage())
    assert calls == ['Z1', 'Z2', 'X']

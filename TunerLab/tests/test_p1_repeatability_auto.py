"""F-20 automatic A→B→A acquisition: backend tests.

The automatic repeatability run *moves the machine*, so the test drives it
through the mock controller and the mock analyser.  What is checked here is
the cycle itself — the legs, the labels, the hysteresis number and the
refusal to measure outside the instrument grid.
"""
import numpy as np
import pytest

from tuner.models import TunerConfig
from tuner.motion import MockMotionController, MotionPlanner
from tuner.repeatability import (
    analyze_repeatability, auto_repeatability_measurements,
    build_repeatability_report,
)
from tuner.vna import MockVNAController


class _StubVNA:
    """A stand-in analyser that returns a fixed Γ at a known frequency."""

    def __init__(self, frequency=5e9, gamma=0.2 + 0.1j):
        self.frequency = frequency
        self.gamma = gamma
        self.simulated = True
        self.measurements = 0

    def measure(self):
        from tuner.models import Measurement
        self.measurements += 1
        s = np.zeros((1, 2, 2), complex)
        s[0, 0, 0] = self.gamma
        s[0, 1, 0] = 0.05
        s[0, 0, 1] = 0.05
        return Measurement(np.array([self.frequency], float), s, True)


def _planner():
    config = TunerConfig()
    motion = MockMotionController(config)
    motion.initialize()
    motion.home()
    return MotionPlanner(config, motion)


def test_cycle_visits_three_legs_per_cycle():
    planner = _planner()
    vna = _StubVNA()
    result = auto_repeatability_measurements(
        planner, vna, [0.0, 0.0, 0.0], [1.0, 0.1, 0.0],
        repeats=2, frequency_hz=5e9)
    assert vna.measurements == 6              # 2 cycles x 3 legs
    labels = [item['label'] for item in result['measurements']]
    assert labels == ['A#1', 'B#1', 'A#1-返回',
                      'A#2', 'B#2', 'A#2-返回']
    legs = [item['leg'] for item in result['measurements']]
    assert legs == ['A', 'B', 'A-return'] * 2


def test_hysteresis_is_measured_against_the_first_a():
    planner = _planner()
    vna = _StubVNA()
    result = auto_repeatability_measurements(
        planner, vna, [0.0, 0.0, 0.0], [1.0, 0.1, 0.0],
        repeats=2, frequency_hz=5e9)
    # A constant analyser gives a zero hysteresis.
    assert result['reference_gamma'] == pytest.approx(0.2 + 0.1j)
    assert len(result['hysteresis']) == 2
    for item in result['hysteresis']:
        assert item['delta_gamma'] == pytest.approx(0.0, abs=1e-9)


def test_measurements_feed_the_standard_analysis():
    planner = _planner()
    vna = _StubVNA()
    result = auto_repeatability_measurements(
        planner, vna, [0.0, 0.0, 0.0], [1.0, 0.1, 0.0],
        repeats=2, frequency_hz=5e9)
    records = [{'gamma_re': item['gamma'].real,
                'gamma_im': item['gamma'].imag,
                'label': item['label']} for item in result['measurements']]
    report = build_repeatability_report(records, z0=50.0, tolerance=0.05,
                                        frequency_hz=5e9)
    assert report['n'] == 6
    assert report['mean_delta_gamma'] == pytest.approx(0.0, abs=1e-9)
    assert report['all_within_tolerance'] is True


def test_a_frequency_outside_the_instrument_grid_is_refused():
    planner = _planner()
    vna = _StubVNA(frequency=5e9)
    with pytest.raises(ValueError):
        auto_repeatability_measurements(
            planner, vna, [0.0, 0.0, 0.0], [1.0, 0.1, 0.0],
            repeats=1, frequency_hz=10e9)


def test_mismatched_positions_are_refused():
    planner = _planner()
    vna = _StubVNA()
    with pytest.raises(ValueError):
        auto_repeatability_measurements(
            planner, vna, [0.0, 0.0, 0.0], [1.0, 0.1],
            repeats=1, frequency_hz=5e9)


def test_zero_repeats_are_refused():
    planner = _planner()
    vna = _StubVNA()
    with pytest.raises(ValueError):
        auto_repeatability_measurements(
            planner, vna, [0.0, 0.0, 0.0], [1.0, 0.1, 0.0],
            repeats=0, frequency_hz=5e9)

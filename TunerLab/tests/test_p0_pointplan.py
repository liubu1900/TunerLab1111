"""P0: frequency four modes, equal-|Gamma| ring plan, cold start, interp mode."""
import itertools

import numpy as np
import pytest

from tuner.frequency_plan import (
    FREQUENCY_MODES, build_frequency_plan, apply_frequency_plan,
)
from tuner.interp import (
    interpolation_error_report, select_interpolation_mode,
)
from tuner.models import TunerConfig
from tuner.pointplan import (
    cold_start_positions, gamma_rings_table, gamma_rings_targets,
)


# --------------------------------------------------------------------------- #
# F-05 frequency four modes
# --------------------------------------------------------------------------- #
def test_frequency_modes_constant():
    assert FREQUENCY_MODES == ('fo_list', 'freq_sweep', 'harmonic_list',
                               'arbitrary_list')


def test_fo_list_default_is_single_5ghz():
    plan = build_frequency_plan()
    assert plan['mode'] == 'fo_list'
    assert plan['frequencies_hz'] == [5e9]
    assert plan['points'] == 1
    assert plan['start_hz'] == plan['stop_hz'] == 5e9
    assert plan['uniform'] is True
    assert plan['harmonic_numbers'] == []
    assert isinstance(plan['warnings'], list)


def test_freq_sweep_is_linspace_and_uniform():
    plan = build_frequency_plan('freq_sweep', start_hz=1e9, stop_hz=2e9, points=5)
    assert plan['frequencies_hz'] == pytest.approx(list(np.linspace(1e9, 2e9, 5)))
    assert plan['points'] == 5
    assert plan['start_hz'] == 1e9
    assert plan['stop_hz'] == 2e9
    assert plan['uniform'] is True


def test_freq_sweep_single_point_degenerates():
    plan = build_frequency_plan('freq_sweep', start_hz=5e9, stop_hz=5e9, points=1)
    assert plan['frequencies_hz'] == [5e9]
    assert plan['points'] == 1


def test_harmonic_list_default_is_5_10_15ghz():
    plan = build_frequency_plan('harmonic_list')
    assert plan['frequencies_hz'] == [5e9, 10e9, 15e9]
    assert plan['harmonic_numbers'] == [1, 2, 3]
    assert plan['uniform'] is True


def test_harmonic_list_custom():
    plan = build_frequency_plan('harmonic_list', harmonics=[1, 2, 4])
    assert plan['frequencies_hz'] == [5e9, 10e9, 20e9]
    assert plan['uniform'] is False
    assert plan['warnings']


def test_arbitrary_list_sorted_and_nonuniform():
    plan = build_frequency_plan('arbitrary_list', frequencies_hz=[10e9, 5e9, 7e9])
    assert plan['frequencies_hz'] == [5e9, 7e9, 10e9]
    assert plan['points'] == 3
    assert plan['uniform'] is False  # steps 2 and 3 GHz


def test_arbitrary_list_uniform():
    plan = build_frequency_plan('arbitrary_list', frequencies_hz=[3e9, 1e9, 2e9])
    assert plan['frequencies_hz'] == [1e9, 2e9, 3e9]
    assert plan['uniform'] is True


def test_nonuniform_grid_rejected_for_freq_sweep():
    with pytest.raises(ValueError):
        build_frequency_plan('freq_sweep', frequencies_hz=[1e9, 2e9, 4e9])


def test_duplicate_frequencies_rejected():
    with pytest.raises(ValueError):
        build_frequency_plan('arbitrary_list', frequencies_hz=[5e9, 5e9, 10e9])
    with pytest.raises(ValueError):
        build_frequency_plan('harmonic_list', harmonics=[1, 1, 2])


@pytest.mark.parametrize('kwargs', [
    {'mode': 'freq_sweep', 'start_hz': -1e9, 'stop_hz': 2e9, 'points': 3},
    {'mode': 'freq_sweep', 'start_hz': 2e9, 'stop_hz': 1e9, 'points': 3},
    {'mode': 'freq_sweep', 'start_hz': 1e9, 'stop_hz': 2e9, 'points': 0},
    {'mode': 'arbitrary_list', 'frequencies_hz': [5e9, float('nan')]},
    {'mode': 'arbitrary_list', 'frequencies_hz': [5e9, -1e9]},
    {'mode': 'unknown_mode'},
])
def test_invalid_frequency_inputs_raise_value_error(kwargs):
    with pytest.raises(ValueError):
        build_frequency_plan(**kwargs)


def test_all_modes_return_valid_increasing_sequences():
    cases = [
        build_frequency_plan(),
        build_frequency_plan('freq_sweep', start_hz=1e9, stop_hz=9e9, points=9),
        build_frequency_plan('harmonic_list'),
        build_frequency_plan('arbitrary_list', frequencies_hz=[9e9, 1e9, 5e9]),
    ]
    for plan in cases:
        f = np.asarray(plan['frequencies_hz'], float)
        assert f.ndim == 1 and f.size >= 1
        assert np.isfinite(f).all()
        assert np.all(f > 0)
        if f.size > 1:
            assert np.all(np.diff(f) > 0)
        assert isinstance(plan['uniform'], bool)
        assert isinstance(plan['warnings'], list)


class _FakeVNA:
    def __init__(self, with_sweep=True):
        self.frequencies = np.array([1e9])
        if with_sweep:
            self._sweep = {'start': 1e9, 'stop': 1e9, 'points': 1}
        self.calls = []

    def set_sweep(self, start, stop, points):
        self.calls.append((start, stop, points))
        self.frequencies = (np.asarray([start], float) if points == 1
                            else np.linspace(start, stop, points))
        if hasattr(self, '_sweep'):
            self._sweep = {'start': start, 'stop': stop, 'points': points}


def test_apply_frequency_plan_sweep_uses_set_sweep():
    vna = _FakeVNA(with_sweep=True)
    plan = build_frequency_plan('freq_sweep', start_hz=1e9, stop_hz=2e9, points=5)
    assert apply_frequency_plan(vna, plan) is None
    assert vna.calls == [(1e9, 2e9, 5)]
    assert len(vna.frequencies) == 5


def test_apply_frequency_plan_without_set_sweep_is_safe():
    class Plain:
        frequencies = np.array([1e9])

    vna = Plain()
    plan = build_frequency_plan()  # fo_list single 5 GHz
    apply_frequency_plan(vna, plan)
    assert list(vna.frequencies) == [5e9]


def test_apply_frequency_plan_list_mode_syncs_sweep_without_scpi():
    vna = _FakeVNA(with_sweep=True)
    plan = build_frequency_plan('arbitrary_list',
                                frequencies_hz=[5e9, 7e9, 11e9])
    apply_frequency_plan(vna, plan)
    assert vna.calls == []  # no non-uniform SCPI sweep issued
    assert list(vna.frequencies) == [5e9, 7e9, 11e9]
    assert vna._sweep['start'] == 5e9
    assert vna._sweep['stop'] == 11e9
    assert vna._sweep['points'] == 3


# --------------------------------------------------------------------------- #
# F-06 equal-|Gamma| ring plan
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize('count', [1, 2, 7, 95, 181, 361])
def test_gamma_rings_targets_count_radius_determinism(count):
    a = gamma_rings_targets(count)
    b = gamma_rings_targets(count)
    assert a.size == count
    assert np.array_equal(a, b)
    assert np.isfinite(a.real).all() and np.isfinite(a.imag).all()
    mag = np.abs(a)
    assert np.all(mag <= 0.95 + 1e-12)
    assert np.all(mag >= 0.0 - 1e-12)


def test_gamma_rings_targets_custom_radius_bounds():
    a = gamma_rings_targets(120, r_min=0.2, r_max=0.8)
    mag = np.abs(a)
    assert a.size == 120
    assert np.all(mag >= 0.2 - 1e-12)
    assert np.all(mag <= 0.8 + 1e-12)


def test_gamma_rings_targets_enhanced_and_offset():
    base = gamma_rings_targets(181)
    ec = gamma_rings_targets(181, enhanced_center=True)
    e50 = gamma_rings_targets(181, enhanced_50ohm=True)
    off = gamma_rings_targets(181, phase_offset_deg=17.0)
    for arr in (ec, e50, off):
        assert arr.size == 181
        mag = np.abs(arr)
        assert np.all(mag <= 0.95 + 1e-12) and np.all(mag >= -1e-12)
    assert np.array_equal(ec, gamma_rings_targets(181, enhanced_center=True))
    assert not np.array_equal(base, off)


def test_gamma_rings_table_sum_and_layers():
    count = 181
    table = gamma_rings_table(count)
    assert sum(item['points'] for item in table) == count
    assert len(table) == int(np.ceil(np.sqrt(count)))
    for item in table:
        assert set(item) == {'layer', 'gamma_min', 'gamma_max',
                             'radius', 'points', 'x_step'}
        assert 0.0 <= item['x_step'] <= 1.0
        assert item['gamma_min'] <= item['radius'] <= item['gamma_max']
    t5 = gamma_rings_table(181, rings=5)
    assert len(t5) == 5
    assert sum(item['points'] for item in t5) == 181


def test_table_matches_targets_length():
    kwargs = dict(count=137, r_min=0.05, r_max=0.9, rings=9,
                  enhanced_center=True, enhanced_50ohm=True)
    table = gamma_rings_table(**kwargs)
    targets = gamma_rings_targets(**kwargs)
    assert sum(item['points'] for item in table) == targets.size == 137


# --------------------------------------------------------------------------- #
# F-07 cold start
# --------------------------------------------------------------------------- #
def test_cold_start_shape_and_bounds():
    cfg = TunerConfig(probes=3, x_limits=(2.0, 8.0), z_limits=(0.1, 0.9),
                      safe_z=0.1, fixed_offsets=(0.0, 2.0, 4.0))
    targets = gamma_rings_targets(50)
    pos = cold_start_positions(targets, cfg)
    assert pos.shape == (50, 4)
    assert np.isfinite(pos).all()
    assert np.all(pos[:, 0] >= 2.0 - 1e-12) and np.all(pos[:, 0] <= 8.0 + 1e-12)
    assert np.all(pos[:, 1:] >= 0.1 - 1e-12) and np.all(pos[:, 1:] <= 0.9 + 1e-12)
    assert np.array_equal(pos, cold_start_positions(targets, cfg))


def test_cold_start_two_probes_has_three_columns():
    cfg = TunerConfig(probes=2)
    pos = cold_start_positions(gamma_rings_targets(20), cfg)
    assert pos.shape == (20, 3)


def test_cold_start_missing_config_defaults_to_four_columns():
    targets = gamma_rings_targets(10)
    for cfg in (None, object(), 5, 'nonsense'):
        pos = cold_start_positions(targets, cfg)
        assert pos.shape == (10, 4)
        assert np.isfinite(pos).all()


def test_cold_start_empty_targets():
    pos = cold_start_positions(np.array([], dtype=complex), TunerConfig())
    assert pos.shape == (0, 3)


# --------------------------------------------------------------------------- #
# F-08 interpolation mode explicit + error report
# --------------------------------------------------------------------------- #
def _full_grid():
    return np.asarray(list(itertools.product([0., 1., 2.], repeat=3)), float)


def test_select_mode_complete_grid_is_tensor():
    result = select_interpolation_mode('cartesian', _full_grid())
    assert result['mode'] == 'tensor'
    assert result['degraded'] is False
    assert result['grid_complete'] is True
    assert result['requested'] == 'tensor'
    assert result['explicit'] is False
    assert isinstance(result['reason'], str) and result['reason']


def test_select_mode_incomplete_grid_is_explicitly_degraded():
    grid = _full_grid()[:-1]  # one point removed -> product 27 != 26
    result = select_interpolation_mode('cartesian', grid)
    assert result['mode'] == 'scattered'
    assert result['degraded'] is True
    assert result['grid_complete'] is False
    assert '不完整' in result['reason'] or '降级' in result['reason']


@pytest.mark.parametrize('plan', ['smith', 'gamma_rings'])
def test_select_mode_scattered_plans(plan):
    result = select_interpolation_mode(plan, _full_grid())
    assert result['mode'] == 'scattered'
    assert result['degraded'] is False


def test_select_mode_explicit():
    ok = select_interpolation_mode('cartesian', _full_grid(), explicit='scattered')
    assert ok['mode'] == 'scattered'
    assert ok['explicit'] is True
    assert ok['requested'] == 'scattered'
    with pytest.raises(ValueError):
        select_interpolation_mode('cartesian', _full_grid(), explicit='bogus')


def test_interpolation_error_report_does_not_raise():
    grid = _full_grid()
    gamma = 0.1 + 0.5j * np.linspace(0, 1, len(grid))
    s = np.zeros((len(grid), 1, 2, 2), dtype=complex)
    s[:, 0, 0, 0] = gamma
    report = interpolation_error_report(grid, s, 'tensor')
    assert report['mode'] == 'tensor'
    assert report['samples'] > 0
    assert np.isfinite(report['mean_error']) and report['mean_error'] >= 0
    assert np.isfinite(report['max_error']) and report['max_error'] >= 0
    assert len(report['per_layer']) == 3
    for layer in report['per_layer']:
        assert np.isfinite(layer['mean_error'])
        assert np.isfinite(layer['max_error'])


def test_interpolation_error_report_degenerate_inputs_are_safe():
    safe_cases = [
        (np.zeros((1, 3)), np.array([[1.0]]), 'tensor'),
        (np.zeros((0, 3)), np.zeros((0, 1, 2, 2)), 'scattered'),
        (np.array([1.0, 2.0]), np.array([0.1, 0.2]), 'tensor'),
        (np.zeros((3, 2)), np.array([1.0, 2.0, 3.0]), 'scattered'),
    ]
    for positions, s, mode in safe_cases:
        report = interpolation_error_report(positions, s, mode)
        assert report['mode'] == mode
        assert np.isfinite(report['mean_error'])
        assert np.isfinite(report['max_error'])
        assert isinstance(report['per_layer'], list)

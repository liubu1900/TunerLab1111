"""The interpolated-tuning validation loop.

The tests use a synthetic tuner with a known analytic response, so the model's
prediction, the "measured" value and the resulting verdict can all be checked
against ground truth.
"""
import numpy as np
import pytest

from tuner.interp import TunerResponseModel
from tuner.tuner_validation import (
    DEFAULT_TOLERANCE, STRATEGIES, evaluate, plan_points, summarise,
)


def synthetic_tuner(nx=9, nz=5, x_range=(0.0, 10.0), z_range=(0.0, 1.0)):
    """Gamma = r(Z) * exp(j * phase(X)); S11 = Gamma, S21 = 0.9."""
    x_levels = np.linspace(*x_range, nx)
    z_levels = np.linspace(*z_range, nz)
    positions = np.array([[x, z] for x in x_levels for z in z_levels], dtype=float)

    def gamma_of(x, z, magnitude_gain=1.0, phase_shift=0.0):
        radius = (0.05 + 0.9 * z) * magnitude_gain
        phase = 2.0 * np.pi * x / (x_range[1] - x_range[0]) + phase_shift
        return radius * np.exp(1j * phase)

    s = np.zeros((len(positions), 1, 2, 2), dtype=complex)
    for index, (x, z) in enumerate(positions):
        s[index, 0, 0, 0] = gamma_of(x, z)
        s[index, 0, 1, 0] = 0.9
    model = TunerResponseModel(positions, s, [1e9])
    return model, gamma_of


def make_measure(gamma_of, magnitude_gain=1.0, phase_shift=0.0):
    """A measurement function that returns the true S of the synthetic tuner."""
    def measure(position):
        x, z = float(position[0]), float(position[1])
        s = np.zeros((2, 2), dtype=complex)
        s[0, 0] = gamma_of(x, z, magnitude_gain, phase_shift)
        s[1, 0] = 0.9
        return s
    return measure


# --------------------------------------------------------------- planning
def test_planned_points_lie_between_the_calibrated_levels():
    model, _gamma = synthetic_tuner(nx=7, nz=4)
    points = plan_points(model, 5, strategy='coverage', frequency=1e9)
    levels_x = model.interpolator.layout.levels[0]
    levels_z = model.interpolator.layout.levels[1]
    assert points.shape == (5, 2)
    for point in points:
        # A cell centre is strictly between two levels on every axis.
        assert not np.any(np.isclose(point[0], levels_x))
        assert not np.any(np.isclose(point[1], levels_z))
        index = int(np.searchsorted(levels_x, point[0]))
        assert 0 < index < len(levels_x)
        assert levels_x[index - 1] < point[0] < levels_x[index]


def test_coverage_strategy_picks_the_hardest_places():
    model, _gamma = synthetic_tuner(nx=9, nz=5)
    points = plan_points(model, 3, strategy='coverage', frequency=1e9)
    measured = model.measured_gammas(1e9)
    predicted = np.asarray([model.gamma_at(p, 1e9) for p in points])
    gaps = np.abs(predicted[:, None] - measured[None, :]).min(axis=1)
    # Every chosen point is far away from the calibrated set.
    assert gaps.min() > 0.05


def test_random_strategy_spreads_the_points():
    model, _gamma = synthetic_tuner(nx=9, nz=5)
    points = plan_points(model, 4, strategy='random', frequency=1e9)
    assert points.shape == (4, 2)
    # The points must be distinct.
    for index in range(len(points)):
        for other in range(index + 1, len(points)):
            assert not np.allclose(points[index], points[other])


def test_planning_rejects_bad_arguments():
    model, _gamma = synthetic_tuner()
    with pytest.raises(ValueError):
        plan_points(model, 0)
    with pytest.raises(ValueError):
        plan_points(model, 3, strategy='magic')
    assert 'coverage' in STRATEGIES and 'random' in STRATEGIES


# -------------------------------------------------------------- evaluation
def test_a_faithful_model_passes_the_validation():
    """The measured value must agree with the model within the tolerance.

    The two do not agree *exactly*: the cell centre lies between the calibrated
    levels, so the difference is exactly the interpolation error that this loop
    exists to measure.  With nine levels the reflection phase turns 45 degrees
    per step, and the error is small but clearly not zero.
    """
    model, gamma_of = synthetic_tuner(nx=9, nz=5)
    points = plan_points(model, 4, strategy='coverage', frequency=1e9)
    report = evaluate(model, points, make_measure(gamma_of), frequency=1e9)
    assert report['passed'] is True
    assert report['verdict'] == 'pass'
    assert 0.0 < report['max_error'] < DEFAULT_TOLERANCE
    assert report['count'] == 4
    assert '可信' in report['message']


def test_the_measured_error_shrinks_with_a_denser_grid():
    """The interpolation error is what the loop reports, so it must converge."""
    errors = []
    for nx in (9, 17, 33):
        model, gamma_of = synthetic_tuner(nx=nx, nz=5)
        points = plan_points(model, 4, strategy='coverage', frequency=1e9)
        report = evaluate(model, points, make_measure(gamma_of), frequency=1e9)
        errors.append(report['max_error'])
    assert errors[0] > errors[1] > errors[2]


def test_a_biased_model_is_caught_and_recalibration_is_recommended():
    model, gamma_of = synthetic_tuner(nx=9, nz=5)
    points = plan_points(model, 4, strategy='coverage', frequency=1e9)
    # The "real" tuner reflects 20 % more than the model believes.
    report = evaluate(model, points, make_measure(gamma_of, magnitude_gain=1.2),
                      frequency=1e9)
    assert report['passed'] is False
    assert report['verdict'] == 'recalibration_recommended'
    assert '建议重新标定' in report['message']
    assert report['failed_points']
    assert report['max_error'] > 0.05


def test_the_error_vector_separates_magnitude_and_phase():
    """A pure phase shift and a pure magnitude change must land in their own column.

    The comparison is made against the baseline (the interpolation error itself),
    because the loop measures the *difference* between the model and the machine.
    """
    model, gamma_of = synthetic_tuner(nx=9, nz=5)
    points = plan_points(model, 3, strategy='coverage', frequency=1e9)
    baseline = evaluate(model, points, make_measure(gamma_of), frequency=1e9)

    # A pure phase shift of 5 degrees must show up in the phase column only.
    shifted = evaluate(model, points, make_measure(gamma_of, phase_shift=np.deg2rad(5.0)),
                       frequency=1e9)
    for before, after in zip(baseline['points'], shifted['points']):
        assert (after['phase_error_deg'] - before['phase_error_deg']) == pytest.approx(
            5.0, abs=0.1)
        assert abs(after['magnitude_error'] - before['magnitude_error']) < 1e-3

    # A pure magnitude change must show up in the magnitude column only.
    scaled = evaluate(model, points, make_measure(gamma_of, magnitude_gain=1.1),
                      frequency=1e9)
    for before, after in zip(baseline['points'], scaled['points']):
        assert (after['magnitude_error'] - before['magnitude_error']) > 0.005
        assert abs(after['phase_error_deg'] - before['phase_error_deg']) < 0.5


def test_the_error_is_reported_in_decibels():
    model, gamma_of = synthetic_tuner(nx=9, nz=5)
    points = plan_points(model, 3, strategy='coverage', frequency=1e9)
    report = evaluate(model, points, make_measure(gamma_of, magnitude_gain=1.5),
                      frequency=1e9)
    for point in report['points']:
        assert point['error_db'] == pytest.approx(
            20.0 * np.log10(point['error']), rel=1e-9)


def test_progress_is_reported():
    model, gamma_of = synthetic_tuner(nx=9, nz=5)
    points = plan_points(model, 3, strategy='coverage', frequency=1e9)
    seen = []
    evaluate(model, points, make_measure(gamma_of), frequency=1e9,
             progress=lambda done, total: seen.append((done, total)))
    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_the_tolerance_decides_the_verdict():
    model, gamma_of = synthetic_tuner(nx=9, nz=5)
    points = plan_points(model, 3, strategy='coverage', frequency=1e9)
    measure = make_measure(gamma_of, magnitude_gain=1.2)
    strict = evaluate(model, points, measure, tolerance=0.01, frequency=1e9)
    lenient = evaluate(model, points, measure, tolerance=0.5, frequency=1e9)
    assert strict['passed'] is False
    assert lenient['passed'] is True


def test_evaluation_rejects_bad_input():
    model, gamma_of = synthetic_tuner()
    points = plan_points(model, 2, frequency=1e9)
    with pytest.raises(ValueError):
        evaluate(model, points, make_measure(gamma_of), tolerance=-1.0)
    with pytest.raises(ValueError):
        evaluate(model, [], make_measure(gamma_of))
    with pytest.raises(ValueError):
        evaluate(model, points, lambda position: np.zeros(3), frequency=1e9)


def test_summarise_handles_an_empty_result():
    report = summarise([], tolerance=DEFAULT_TOLERANCE)
    assert report['count'] == 0
    assert report['passed'] is False

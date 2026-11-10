"""Smith-chart uniform calibration point planning."""
import numpy as np
import pytest

from tuner.pointplan import (
    DENSITY_PRESETS, MeasuredModel, coverage_report, density_count,
    plan_positions_from_targets, recommended_phase_levels, refine_positions,
    smith_targets, smith_xy,
)


def nearest_neighbour_stats(values):
    distance = np.abs(values[:, None] - values[None, :])
    np.fill_diagonal(distance, np.inf)
    nearest = distance.min(axis=1)
    return nearest.mean(), nearest.std(), nearest.min(), nearest.max()


# --------------------------------------------------------------- targets
def test_density_presets_match_the_documented_counts():
    # Industry documentation gives 95 / 181 / 361 calibration points.
    assert DENSITY_PRESETS == {'low': 95, 'medium': 181, 'high': 361}
    assert density_count('medium') == 181
    assert density_count('LOW') == 95
    assert density_count('high', 500) == 500
    with pytest.raises(ValueError):
        density_count('ultra')
    with pytest.raises(ValueError):
        density_count('low', 0)


@pytest.mark.parametrize('count', [1, 2, 3, 7, 95, 181, 361, 500])
def test_target_count_is_exact(count):
    for method in ('sunflower', 'rings'):
        targets = smith_targets(count, method=method)
        assert targets.size == count
        assert np.all(np.isfinite(targets.real))
        assert np.all(np.isfinite(targets.imag))


def test_targets_stay_inside_the_requested_radii():
    targets = smith_targets(181, r_min=0.1, r_max=0.9)
    radii = np.abs(targets)
    assert radii.min() >= 0.1 - 1e-9
    assert radii.max() <= 0.9 + 1e-9


def test_sunflower_is_more_uniform_than_random():
    targets = smith_targets(181, method='sunflower')
    mean, std, low, high = nearest_neighbour_stats(targets)
    rng = np.random.default_rng(7)
    random = np.sqrt(rng.random(181)) * np.exp(1j * rng.random(181) * 2 * np.pi)
    r_mean, r_std, _, _ = nearest_neighbour_stats(random)
    # The sunflower distribution has an almost constant spacing; a random draw
    # has the same average but clumps badly.
    assert mean > r_mean * 0.9
    assert std < r_std / 5
    assert high / low < 1.6


def test_rings_method_is_also_usable():
    targets = smith_targets(95, method='rings')
    assert targets.size == 95
    radii = np.abs(targets)
    assert radii.max() <= 0.95 + 1e-9


def test_invalid_arguments_are_rejected():
    with pytest.raises(ValueError):
        smith_targets(0)
    with pytest.raises(ValueError):
        smith_targets(10, r_min=0.9, r_max=0.1)
    with pytest.raises(ValueError):
        smith_targets(10, method='spiral-web')


def test_smith_xy_is_the_cartesian_projection():
    assert smith_xy(complex(0.25, -0.5)) == (0.25, -0.5)


# ---------------------------------------------------------------- model
def build_linear_model(seed=3, count=14):
    """A synthetic tuner whose Gamma is linear in the (x, z) coordinates."""
    rng = np.random.default_rng(seed)
    positions = rng.random((count, 2))
    coefficients = (complex(0.6, -0.2), complex(0.35, 0.45), complex(0.1, 0.05))
    gammas = np.array([
        coefficients[0] + coefficients[1] * p[0] + coefficients[2] * p[1]
        for p in positions])
    return MeasuredModel(positions, gammas), positions, gammas, coefficients


def test_model_recovers_a_linear_response():
    model, positions, gammas, _ = build_linear_model()
    for position, gamma in zip(positions, gammas):
        predicted = model.predict_gamma(position)
        assert abs(predicted - gamma) < 1e-6


def test_model_inverts_a_gamma_back_to_a_position():
    model, positions, gammas, coefficients = build_linear_model()
    # A target inside the measured domain: the response of the box centre.
    centre = positions.mean(axis=0)
    target = (coefficients[0] + coefficients[1] * centre[0]
              + coefficients[2] * centre[1])
    point = model.invert_gamma(target)
    assert abs(model.predict_gamma(point) - target) < 1e-6
    # Every measured Gamma must invert back onto the measured set.
    for position, gamma in zip(positions, gammas):
        recovered = model.invert_gamma(gamma)
        assert abs(model.predict_gamma(recovered) - gamma) < 1e-6


def test_inverse_respects_the_axis_limits():
    model, _positions, _gammas, coefficients = build_linear_model()
    centre = model.positions.mean(axis=0)
    target = (coefficients[0] + coefficients[1] * centre[0]
              + coefficients[2] * centre[1])
    point = model.invert_gamma(target, limits=[(0.0, 0.5), (0.0, 0.5)])
    assert np.all(point >= 0.0 - 1e-12)
    assert np.all(point <= 0.5 + 1e-12)


def test_model_needs_enough_points():
    with pytest.raises(ValueError):
        MeasuredModel(np.zeros((1, 2)), np.zeros(1))
    with pytest.raises(ValueError):
        MeasuredModel(np.zeros((3, 2)), np.zeros(2))


def test_plan_positions_from_targets_round_trips():
    model, _positions, gammas, _c = build_linear_model()
    planned = plan_positions_from_targets(model, gammas)
    assert planned.shape == (14, 2)
    for point, target in zip(planned, gammas):
        assert abs(model.predict_gamma(point) - target) < 1e-6


# ------------------------------------------------------------ refinement
def test_refinement_fills_the_gaps_in_the_chart():
    model, _positions, _gammas, _c = build_linear_model()
    # Everything measured so far sits in one corner of the Gamma plane.
    measured = [complex(0.05 + 0.01 * k, 0.02) for k in range(4)]
    planned, targets = refine_positions(measured, model, 20)
    assert planned.shape == (20, 2)
    assert targets.size == 20
    distance = np.abs(np.asarray(measured)[:, None] - targets[None, :]).min(axis=0)
    # Every proposal is farther away than the measured points are from each other.
    assert distance.min() > 0.1


def test_refinement_can_be_repeated_to_fill_in_the_chart():
    model, _positions, _gammas, _c = build_linear_model()
    measured = [complex(0.05 + 0.01 * k, 0.02) for k in range(4)]
    first, first_targets = refine_positions(measured, model, 15)
    combined = list(measured) + list(first_targets)
    before = coverage_report(combined)
    second, second_targets = refine_positions(combined, model, 15)
    after = coverage_report(list(combined) + list(second_targets))
    # Adding points always lowers the *mean* nearest-neighbour distance; what
    # the refinement promises is that the largest remaining gap shrinks.
    assert after['count'] == before['count'] + 15
    assert after['max_spacing'] <= before['max_spacing'] + 1e-9
    assert second.shape == (15, 2)


def test_coverage_report_measures_the_spacing():
    values = [complex(0, 0), complex(1, 0), complex(0, 1)]
    report = coverage_report(values)
    assert report['count'] == 3
    # Every point has a neighbour exactly one unit away.
    assert report['mean_spacing'] == pytest.approx(1.0, rel=1e-6)
    assert report['max_spacing'] == pytest.approx(1.0, rel=1e-6)
    assert report['min_spacing'] == pytest.approx(1.0, rel=1e-6)
    empty = coverage_report([])
    assert empty['count'] == 0


def test_recommended_phase_levels_keeps_the_phase_step_small():
    # A full 360 degree wrap with a 20 degree limit needs 19 levels.
    assert recommended_phase_levels(360.0, 20.0) == 19
    assert recommended_phase_levels(360.0, 45.0) == 9
    assert recommended_phase_levels(180.0, 20.0) == 10
    # Never fewer than three levels, which the second-order fit needs.
    assert recommended_phase_levels(10.0, 20.0) == 3
    with pytest.raises(ValueError):
        recommended_phase_levels(360.0, 0.0)

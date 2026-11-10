"""Second-order interpolation and two-stage tuning.

The tests use a synthetic tuner whose reflection coefficient is known analytically, so
the interpolated response and the tuning error can be checked against ground truth
instead of against themselves.
"""
import numpy as np
import pytest

from tuner.interp import (
    GridLayout, ScatteredInterpolator, TensorInterpolator, TunerResponseModel,
    build_interpolator, lagrange_weights,
)


# --------------------------------------------------------------------------- #
# Synthetic tuner
# --------------------------------------------------------------------------- #
def synthetic_tuner(nx=9, nz=5, x_range=(0.0, 10.0), z_range=(0.0, 1.0)):
    """A tuner whose Gamma is r(Z) * exp(j * phase(X)), with S11 = Gamma.

    With ``termination = 0`` and no fixture the RF engine reduces Gamma to S11, so
    the measured data can be built directly from the analytic expression.
    """
    x_levels = np.linspace(*x_range, nx)
    z_levels = np.linspace(*z_range, nz)
    positions = np.array([[x, z] for x in x_levels for z in z_levels], dtype=float)

    def gamma_of(x, z):
        radius = 0.05 + 0.9 * z
        phase = 2.0 * np.pi * x / (x_range[1] - x_range[0])
        return radius * np.exp(1j * phase)

    s = np.zeros((len(positions), 1, 2, 2), dtype=complex)
    for index, (x, z) in enumerate(positions):
        s[index, 0, 0, 0] = gamma_of(x, z)
        s[index, 0, 1, 0] = 0.9
    return positions, s, gamma_of, (x_levels, z_levels)


# --------------------------------------------------------------------------- #
# Lagrange basis
# --------------------------------------------------------------------------- #
def test_lagrange_weights_reproduce_the_nodes():
    for node, expected in ((1.0, (1.0, 0.0, 0.0)), (2.0, (0.0, 1.0, 0.0)),
                           (3.0, (0.0, 0.0, 1.0))):
        assert lagrange_weights(node, 1.0, 2.0, 3.0) == pytest.approx(expected)
    # The weights always sum to one, so a constant is reproduced exactly.
    assert sum(lagrange_weights(2.4, 1.0, 2.0, 3.0)) == pytest.approx(1.0)


def test_lagrange_weights_are_exact_for_a_quadratic():
    y1, y2, y3 = 0.0, 1.0, 2.5
    for x in (-0.5, 0.3, 1.0, 2.0, 3.0):
        weights = lagrange_weights(x, y1, y2, y3)
        values = [v * v - 3 * v + 1 for v in (y1, y2, y3)]
        assert sum(w * v for w, v in zip(weights, values)) == pytest.approx(x * x - 3 * x + 1)


def test_lagrange_weights_reject_repeated_nodes():
    with pytest.raises(ValueError):
        lagrange_weights(1.0, 1.0, 1.0, 2.0)


# --------------------------------------------------------------------------- #
# Grid detection
# --------------------------------------------------------------------------- #
def test_grid_is_detected_for_a_complete_tensor():
    positions, _s, _gamma, _levels = synthetic_tuner()
    layout = GridLayout.detect(positions)
    assert layout is not None
    assert layout.ndim == 2
    assert layout.shape == (9, 5)
    assert layout.size == len(positions)


def test_grid_is_rejected_when_a_point_is_missing():
    positions, _s, _gamma, _levels = synthetic_tuner()
    assert GridLayout.detect(positions[:-1]) is None


def test_grid_is_rejected_for_scattered_points():
    rng = np.random.default_rng(2)
    points = rng.random((40, 2))
    assert GridLayout.detect(points) is None


# --------------------------------------------------------------------------- #
# Tensor interpolation
# --------------------------------------------------------------------------- #
def test_tensor_interpolator_reproduces_the_measured_points():
    positions, s, gamma_of, _levels = synthetic_tuner()
    model = TunerResponseModel(positions, s, [1e9])
    for index, (x, z) in enumerate(positions):
        predicted = model.gamma_at([x, z], 1e9)
        assert predicted == pytest.approx(gamma_of(x, z), abs=1e-9)


def test_tensor_interpolation_is_exact_for_an_additively_separable_response():
    """A response that is a *sum* of per-axis functions is interpolated exactly."""
    positions, _s, _gamma, _levels = synthetic_tuner()
    values = np.zeros((len(positions), 1, 2, 2), dtype=complex)
    for index, (x, z) in enumerate(positions):
        # f(x, z) = a(x) + b(z): the tensor interpolation reproduces this exactly.
        values[index, 0, 0, 0] = (0.3 + 0.02 * x) + 1j * (0.1 + 0.4 * z)
    model = TunerResponseModel(positions, values, [1e9])
    for x, z in ((1.234, 0.456), (7.89, 0.21), (3.3, 0.77)):
        expected = (0.3 + 0.02 * x) + 1j * (0.1 + 0.4 * z)
        assert model.gamma_at([x, z], 1e9) == pytest.approx(expected, abs=1e-12)


def test_second_order_interpolation_beats_a_first_order_one():
    """The second-order scheme must be clearly more accurate than a linear one.

    The margin depends on the grid: with nine X levels the reflection phase turns
    45 degrees per step, which is coarse for any interpolant.  The second-order
    scheme still wins by a wide margin, and the margin grows with a denser grid
    (see the convergence test below).
    """
    positions, s, gamma_of, _levels = synthetic_tuner(nx=9, nz=5)
    model = TunerResponseModel(positions, s, [1e9])
    linear = first_order_tensor_gamma(positions, s, (9, 5))
    queries = [(1.234, 0.456), (7.89, 0.21), (3.3, 0.77), (8.4, 0.66)]
    second_order_error = 0.0
    first_order_error = 0.0
    for x, z in queries:
        truth = gamma_of(x, z)
        second_order_error = max(second_order_error,
                                 abs(model.gamma_at([x, z], 1e9) - truth))
        first_order_error = max(first_order_error, abs(linear(x, z) - truth))
    assert second_order_error < first_order_error * 0.6


def test_interpolation_error_falls_as_the_grid_gets_denser():
    """Refining the grid must reduce the interpolation error."""
    errors = []
    for nx in (9, 17, 33):
        positions, s, gamma_of, _levels = synthetic_tuner(nx=nx, nz=5)
        model = TunerResponseModel(positions, s, [1e9])
        worst = max(abs(model.gamma_at([x, z], 1e9) - gamma_of(x, z))
                    for x, z in ((1.234, 0.456), (7.89, 0.21), (3.3, 0.77)))
        errors.append(worst)
    assert errors[0] > errors[1] > errors[2]
    # A second-order scheme converges faster than the linear one: doubling the
    # level count divides the error by more than four.
    assert errors[0] / errors[1] > 4.0


def first_order_tensor_gamma(positions, s, shape):
    """A deliberately simple bilinear reference, used to quantify the benefit."""
    levels = [np.unique(positions[:, column]) for column in range(positions.shape[1])]
    lookup = {tuple(int(np.searchsorted(levels[c], p[c])) for c in range(len(levels))): i
              for i, p in enumerate(positions)}
    tensor = np.empty(tuple(shape) + s.shape[1:], dtype=complex)
    for key, flat in lookup.items():
        tensor[key] = s[flat]

    def evaluate(x, z):
        values = tensor
        # Reverse order, so that removing an axis does not shift the index of
        # the axis that is still to be processed.
        for axis in reversed(range(len(levels))):
            query = (x, z)[axis]
            level = levels[axis]
            upper = int(np.searchsorted(level, query))
            if upper <= 0:
                low = high = 0
            elif upper >= len(level):
                low = high = len(level) - 1
            else:
                low, high = upper - 1, upper
            weight = 0.0 if level[high] == level[low] else (
                (query - level[low]) / (level[high] - level[low]))
            weight = min(max(weight, 0.0), 1.0)
            values = values.take(low, axis=axis) * (1 - weight) + values.take(
                high, axis=axis) * weight
        return values[0, 0, 0]

    return evaluate


def test_tensor_interpolation_uses_only_three_levels_per_axis():
    """The classic nine-point neighbourhood: the result must not depend on far points."""
    positions, s, gamma_of, _levels = synthetic_tuner()
    layout = GridLayout.detect(positions)
    interpolator = TensorInterpolator(positions, s, layout)
    query = [4.4, 0.55]
    baseline = interpolator.evaluate(query, 0)

    # Corrupt a point that is outside the three closest levels of each axis.
    neighbours = interpolator._levels_and_weights
    used_x = set(neighbours(0, query[0])[0])
    used_z = set(neighbours(1, query[1])[0])
    far = None
    for index, (x, z) in enumerate(positions):
        xi = int(np.searchsorted(layout.levels[0], x))
        zi = int(np.searchsorted(layout.levels[1], z))
        if xi not in used_x and zi not in used_z:
            far = index
            break
    assert far is not None
    corrupted = s.copy()
    corrupted[far, 0, 0, 0] = 1234.0
    changed = TensorInterpolator(positions, corrupted, layout).evaluate(query, 0)
    assert changed == pytest.approx(baseline, abs=1e-12)


def test_tensor_interpolator_rejects_a_wrong_position_shape():
    positions, s, _gamma, _levels = synthetic_tuner()
    layout = GridLayout.detect(positions)
    interpolator = TensorInterpolator(positions, s, layout)
    with pytest.raises(ValueError):
        interpolator.evaluate([1.0, 2.0, 3.0], 0)


# --------------------------------------------------------------------------- #
# Scattered interpolation
# --------------------------------------------------------------------------- #
def test_scattered_interpolator_recovers_a_smooth_response():
    rng = np.random.default_rng(5)
    positions = rng.random((120, 2)) * np.array([10.0, 1.0])
    s = np.zeros((120, 1, 2, 2), dtype=complex)
    for index, (x, z) in enumerate(positions):
        s[index, 0, 0, 0] = 0.2 + 0.6 * z * np.exp(1j * 0.7 * x)
    model = TunerResponseModel(positions, s, [1e9])
    assert model.interpolator.kind == 'scattered'
    value = model.gamma_at([5.0, 0.5], 1e9)
    assert value == pytest.approx(0.2 + 0.6 * 0.5 * np.exp(1j * 0.7 * 5.0), abs=2e-2)


def test_scattered_interpolator_is_built_when_no_grid_exists():
    rng = np.random.default_rng(6)
    positions = rng.random((40, 2))
    s = np.zeros((40, 1, 2, 2), dtype=complex)
    interpolator = build_interpolator(positions, s)
    assert isinstance(interpolator, ScatteredInterpolator)


# --------------------------------------------------------------------------- #
# Two-stage tuning
# --------------------------------------------------------------------------- #
def test_tuning_reaches_the_target_and_reports_every_stage():
    positions, s, gamma_of, _levels = synthetic_tuner(nx=17, nz=7)
    model = TunerResponseModel(positions, s, [1e9])
    target = 0.6 * np.exp(1j * 1.1)
    result = model.tune(target, 1e9, limits=[(0.0, 10.0), (0.0, 1.0)])

    assert result['predicted'] is True
    assert result['interpolator'] == 'tensor'
    assert [stage['stage'] for stage in result['stages']] == [
        'general_search', 'gradient_search', 'fine_grid']
    # The achieved error must be far below the general-search error.
    assert result['error'] < result['stages'][0]['error']
    assert result['error'] < 0.02
    # The reported position must reproduce the reported Gamma.
    assert model.gamma_at(result['position'], 1e9) == pytest.approx(result['gamma'],
                                                                       abs=1e-9)


def test_general_search_alone_returns_a_measured_point():
    positions, s, _gamma, _levels = synthetic_tuner(nx=17, nz=7)
    model = TunerResponseModel(positions, s, [1e9])
    result = model.tune(0.6 * np.exp(1j * 1.1), 1e9, method='general')
    assert len(result['stages']) == 1
    assert result['stages'][0]['stage'] == 'general_search'
    # The general search returns one of the measured points.
    assert any(np.allclose(result['position'], point) for point in positions)


def test_tuning_respects_the_limits():
    positions, s, _gamma, _levels = synthetic_tuner(nx=17, nz=7)
    model = TunerResponseModel(positions, s, [1e9])
    result = model.tune(0.6 * np.exp(1j * 1.1), 1e9, limits=[(2.0, 8.0), (0.2, 0.8)])
    assert 2.0 - 1e-9 <= result['position'][0] <= 8.0 + 1e-9
    assert 0.2 - 1e-9 <= result['position'][1] <= 0.8 + 1e-9


def test_tuning_on_a_scattered_set_still_converges():
    rng = np.random.default_rng(11)
    positions = rng.random((200, 2)) * np.array([10.0, 1.0])
    s = np.zeros((200, 1, 2, 2), dtype=complex)
    for index, (x, z) in enumerate(positions):
        s[index, 0, 0, 0] = (0.05 + 0.9 * z) * np.exp(1j * 0.628 * x)
    model = TunerResponseModel(positions, s, [1e9])
    result = model.tune(0.5 * np.exp(1j * 2.0), 1e9,
                         limits=[(0.0, 10.0), (0.0, 1.0)])
    assert result['error'] < 0.05


def test_frequency_interpolation_between_calibration_frequencies():
    positions, s, _gamma, _levels = synthetic_tuner()
    frequencies = [1e9, 2e9]
    s2 = np.concatenate([s, s * 0.5], axis=1)
    model = TunerResponseModel(positions, s2, frequencies)
    low = model.gamma_at([4.4, 0.55], 1e9)
    middle = model.gamma_at([4.4, 0.55], 1.5e9)
    high = model.gamma_at([4.4, 0.55], 2e9)
    assert middle == pytest.approx((low + high) / 2, rel=1e-9)


def test_frequency_outside_the_band_is_rejected():
    positions, s, _gamma, _levels = synthetic_tuner()
    model = TunerResponseModel(positions, s, [1e9])
    with pytest.raises(ValueError):
        model.gamma_at([1.0, 0.5], 2e9)

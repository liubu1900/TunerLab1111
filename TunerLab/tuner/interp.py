"""Second-order interpolation of the tuner's response, and two-stage tuning.

Why this module exists
----------------------
The calibration database holds measured S-parameters at a set of machine positions.
Asking "which position presents this impedance?" therefore needs an *interpolant*
between those measured points, and the quality of that interpolant is what limits the
tuning accuracy.

The standard second-order interpolation scheme works as follows:

* the tuner's response is described in a coordinate system that fits its nature —
  **the horizontal (carriage) movement controls the phase, the vertical (probe depth)
  movement controls the magnitude** of the reflection coefficient;
* the interpolation is **second order Lagrange** and uses a **nine point**
  neighbourhood: the three closest horizontal levels crossed with three vertical
  levels;
* the vertical direction is interpolated first, producing virtual calibration points
  at the requested vertical level, and the horizontal direction is interpolated
  afterwards;
* the tuning itself is two-staged: a **general search** that identifies the closest
  calibrated point, followed by a **gradient search over a fine interpolation grid**
  to reach the target.  The patent quotes −60 dB tuning accuracy with the fine grid
  against roughly −20 dB without it.

This module implements that scheme, generalised to a machine with one shared carriage
axis and *n* independent probe axes:

* :class:`TensorInterpolator` — the tensor-grid algorithm proper.  It needs the measured
  points to form a complete tensor grid, which is what the cartesian plan produces.
  For two axes it uses the classic nine points; for *n* axes it uses 3ⁿ.
* :class:`ScatteredInterpolator` — the equivalent for scattered points (the
  Smith-chart plan): a local second-order polynomial fit with a first-order and a
  nearest-neighbour fallback.
* :meth:`TunerResponseModel.tune` — the two-stage tuning with a fine-grid
  refinement, reporting the error after every stage.

Nothing here is measured: every result is a model prediction and is reported as such.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from .rf import RFNetworkEngine as RF

__all__ = [
    'lagrange_weights',
    'GridLayout',
    'TensorInterpolator',
    'ScatteredInterpolator',
    'build_interpolator',
    'TunerResponseModel',
    'LEVEL_POLICIES',
]

#: How the three interpolation levels around a query are chosen.
#:
#: ``'closest'``  the three levels whose centre is nearest to the query.  The query
#:                 is bracketed whenever the calibration covers it, so the fit
#:                 interpolates instead of extrapolating.
#: ``'classic'``  classic rule: one level below the query and two above.
LEVEL_POLICIES = ('closest', 'classic')

#: Old key kept readable for libraries and settings written before the rename.
_LEGACY_LEVEL_POLICY = {'focus': 'classic'}


# --------------------------------------------------------------------------- #
# Lagrange basis
# --------------------------------------------------------------------------- #
def lagrange_weights(x: float, y1: float, y2: float, y3: float
                     ) -> tuple[float, float, float]:
    """Second-order Lagrange basis evaluated at ``x``.

    The three nodes ``y1 < y2 < y3`` and the evaluation point ``x`` give the weights
    that reproduce a quadratic through the three nodes::

        L0 = (x-y2)(x-y3) / ((y1-y2)(y1-y3))
        L1 = (x-y1)(x-y3) / ((y2-y1)(y2-y3))
        L2 = (x-y1)(x-y2) / ((y3-y1)(y3-y2))

    This is the standard second-order Lagrange formula.
    """
    value = float(x)
    a, b, c = float(y1), float(y2), float(y3)
    denominator0 = (a - b) * (a - c)
    denominator1 = (b - a) * (b - c)
    denominator2 = (c - a) * (c - b)
    if denominator0 == 0 or denominator1 == 0 or denominator2 == 0:
        raise ValueError('the three interpolation levels must be distinct')
    w0 = (value - b) * (value - c) / denominator0
    w1 = (value - a) * (value - c) / denominator1
    w2 = (value - a) * (value - b) / denominator2
    return float(w0), float(w1), float(w2)


# --------------------------------------------------------------------------- #
# Grid layout
# --------------------------------------------------------------------------- #
@dataclass
class GridLayout:
    """A complete tensor grid of machine positions."""

    levels: list[np.ndarray]
    lookup: dict[tuple[int, ...], int]

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(len(level) for level in self.levels)

    @property
    def ndim(self) -> int:
        return len(self.levels)

    @property
    def size(self) -> int:
        total = 1
        for length in self.shape:
            total *= length
        return total

    @classmethod
    def detect(cls, positions: np.ndarray, minimum_levels: int = 3
               ) -> Optional['GridLayout']:
        """Detect a complete tensor grid, or return ``None``.

        A grid is only accepted when *every* combination of the per-axis levels is
        present: a hole would break the tensor interpolation along that axis.
        """
        points = np.atleast_2d(np.asarray(positions, dtype=float))
        if points.ndim != 2 or not len(points):
            return None
        axes = []
        for column in range(points.shape[1]):
            unique = np.unique(points[:, column])
            if len(unique) < minimum_levels:
                return None
            axes.append(unique)
        total = 1
        for level in axes:
            total *= len(level)
        if total != len(points):
            return None
        lookup: dict[tuple[int, ...], int] = {}
        for index, point in enumerate(points):
            key = tuple(int(np.searchsorted(axes[column], point[column]))
                        for column in range(points.shape[1]))
            if key in lookup:
                return None
            lookup[key] = index
        if len(lookup) != total:
            return None
        return cls(levels=axes, lookup=lookup)


# --------------------------------------------------------------------------- #
# Interpolators
# --------------------------------------------------------------------------- #
class TensorInterpolator:
    """standard second-order Lagrange interpolation on a tensor grid.

    The interpolation proceeds one axis at a time.  The magnitude axes (the probe
    depths) are interpolated first and the phase axis (the carriage) last, which is
    the usual order: the vertical pass produces virtual calibration points
    at the requested depth, and the horizontal pass then moves them to the requested
    carriage position.
    """

    kind = 'tensor'

    def __init__(self, positions: np.ndarray, s: np.ndarray,
                 layout: GridLayout, phase_axis: int = 0,
                 level_policy: str = 'closest'):
        level_policy = _LEGACY_LEVEL_POLICY.get(str(level_policy).strip().lower(),
                                                level_policy)
        if level_policy not in LEVEL_POLICIES:
            raise ValueError(f'unknown level policy: {level_policy!r}')
        self.layout = layout
        self.phase_axis = int(phase_axis)
        self.level_policy = level_policy
        points = np.atleast_2d(np.asarray(positions, dtype=float))
        values = np.asarray(s, complex)
        tensor = np.empty(layout.shape + values.shape[1:], dtype=complex)
        for key, flat in layout.lookup.items():
            tensor[key] = values[flat]
        self.tensor = tensor
        # Magnitude axes first, phase axis last (the usual order).
        others = [axis for axis in range(layout.ndim) if axis != self.phase_axis]
        self.axis_order = others + [self.phase_axis]

    # -- level selection -------------------------------------------------
    def _levels_and_weights(self, axis: int, value: float):
        levels = self.layout.levels[axis]
        count = len(levels)
        if self.level_policy == 'classic':
            below = int(np.searchsorted(levels, value)) - 1
            below = max(0, min(count - 1, below))
            indices = [below, min(count - 1, below + 1), min(count - 1, below + 2)]
            indices = sorted(set(indices))
            while len(indices) < 3:
                candidate = indices[0] - 1 if indices[0] > 0 else indices[-1] + 1
                indices = sorted(set(indices + [candidate]))
        else:
            # The window of three whose centre is closest to the query; this
            # brackets the query whenever the calibration covers it.
            best = 0
            best_distance = np.inf
            for start in range(count - 2):
                centre = float(levels[start:start + 3].mean())
                distance = abs(centre - value)
                if distance < best_distance:
                    best_distance = distance
                    best = start
            indices = [best, best + 1, best + 2]
        nodes = [float(levels[index]) for index in indices]
        weights = lagrange_weights(value, *nodes)
        return indices, weights

    # -- evaluation ------------------------------------------------------
    def evaluate(self, position: Sequence[float], frequency_index: int = 0
                 ) -> np.ndarray:
        """Interpolated S-parameter matrix at ``position``."""
        point = np.asarray(position, dtype=float).ravel()
        if point.size != self.layout.ndim:
            raise ValueError('position has the wrong number of axes')
        values = self.tensor
        # ``take`` with an integer index removes the axis, so the later axes
        # shift down.  ``remaining`` keeps the mapping from the original axis
        # index to the axis it currently occupies.
        remaining = list(range(self.layout.ndim))
        for axis in self.axis_order:
            current = remaining.index(axis)
            indices, weights = self._levels_and_weights(axis, point[axis])
            values = (values.take(indices[0], axis=current) * weights[0]
                      + values.take(indices[1], axis=current) * weights[1]
                      + values.take(indices[2], axis=current) * weights[2])
            remaining.pop(current)
        # Every position axis has been collapsed; what is left is
        # (frequency, 2, 2).
        return values[int(frequency_index)]


class ScatteredInterpolator:
    """Local second-order polynomial interpolation for scattered points.

    For every query the *k* nearest measured points are selected in the normalised
    coordinate box and a weighted polynomial least-squares fit is evaluated at the
    query.  The degree drops to one (and finally to the nearest point) whenever the
    neighbourhood cannot support a quadratic, so the interpolant always returns a
    value.
    """

    kind = 'scattered'

    def __init__(self, positions: np.ndarray, s: np.ndarray, degree: int = 2,
                 minimum_neighbours: int = 12):
        points = np.atleast_2d(np.asarray(positions, dtype=float))
        values = np.asarray(s, complex)
        if points.shape[0] != values.shape[0]:
            raise ValueError('positions and s must describe the same points')
        self.positions = points
        self.s = values
        self.degree = int(degree)
        self.minimum_neighbours = int(minimum_neighbours)
        self.low = points.min(axis=0)
        scale = points.max(axis=0) - self.low
        scale[scale <= 1e-12] = 1.0
        self.scale = scale
        self.normalised = (points - self.low) / scale
        self.dimension = points.shape[1]
        self._cache: dict[int, np.ndarray] = {}

    # -- helpers ---------------------------------------------------------
    def _design(self, points: np.ndarray, degree: int) -> np.ndarray:
        """Polynomial design matrix (monomials up to the given total degree)."""
        columns = [np.ones(len(points))]
        for index in range(self.dimension):
            columns.append(points[:, index])
        if degree >= 2:
            for i in range(self.dimension):
                for j in range(i, self.dimension):
                    columns.append(points[:, i] * points[:, j])
        return np.column_stack(columns)

    def _terms(self, degree: int) -> int:
        """Number of coefficients of a polynomial of the given degree."""
        if degree >= 2:
            return 1 + self.dimension + self.dimension * (self.dimension + 1) // 2
        if degree == 1:
            return 1 + self.dimension
        return 1

    def _fit(self, query: np.ndarray, frequency_index: int):
        """Weighted polynomial fit of the four S components near the query."""
        distance = np.linalg.norm(self.normalised - query, axis=1)
        count = int(min(len(distance), max(self.minimum_neighbours,
                                           self._terms(self.degree))))
        index = np.argsort(distance)[:count]
        scale = max(float(distance[index].max()), 1e-9)
        weights = (1.0 - (distance[index] / scale) ** 3) ** 3
        weighted = np.sqrt(weights)[:, None]
        # The four S components are fitted together, each with its own complex
        # coefficients, exactly as the tensor interpolator treats them.
        targets = self.s[index, int(frequency_index)].reshape(len(index), 4)
        for degree in (self.degree, 1, 0):
            design = self._design(self.normalised[index], degree)
            if len(index) < design.shape[1]:
                continue
            try:
                solution, *_ = np.linalg.lstsq(design * weighted, targets * weighted,
                                               rcond=None)
            except np.linalg.LinAlgError:  # pragma: no cover - numerically degenerate
                continue
            if np.linalg.matrix_rank(design) < design.shape[1]:
                continue
            return solution, degree
        return None, None

    # -- evaluation ------------------------------------------------------
    def evaluate(self, position: Sequence[float], frequency_index: int = 0
                 ) -> np.ndarray:
        """Interpolated S-parameter matrix at ``position``."""
        point = np.asarray(position, dtype=float).ravel()
        if point.size != self.dimension:
            raise ValueError('position has the wrong number of axes')
        query = (point - self.low) / self.scale
        solution, degree = self._fit(query, frequency_index)
        if solution is None:
            distance = np.linalg.norm(self.normalised - query, axis=1)
            nearest = int(np.argmin(distance))
            return self.s[nearest, int(frequency_index)]
        design = self._design(query.reshape(1, -1), degree)
        value = (design @ solution).reshape(2, 2)
        return value


def build_interpolator(positions: np.ndarray, s: np.ndarray,
                       phase_axis: int = 0, level_policy: str = 'closest',
                       minimum_levels: int = 3):
    """Build the best interpolator for the given calibration set.

    A complete tensor grid gets standard second-order Lagrange scheme; anything else
    gets the scattered second-order polynomial fit.
    """
    layout = GridLayout.detect(positions, minimum_levels=minimum_levels)
    if layout is not None:
        return TensorInterpolator(positions, s, layout, phase_axis, level_policy)
    return ScatteredInterpolator(positions, s)


# --------------------------------------------------------------------------- #
# Response model and two-stage tuning
# --------------------------------------------------------------------------- #
class TunerResponseModel:
    """The tuner's interpolated response, and the search for a target impedance."""

    def __init__(self, positions: np.ndarray, s: np.ndarray,
                 frequencies: Sequence[float], termination: complex = 0j,
                 fixture: np.ndarray | None = None, phase_axis: int = 0,
                 level_policy: str = 'closest'):
        self.positions = np.atleast_2d(np.asarray(positions, dtype=float))
        self.s = np.asarray(s, complex)
        self.frequencies = np.asarray(frequencies, dtype=float)
        if self.s.shape[0] != self.positions.shape[0]:
            raise ValueError('positions and s must describe the same points')
        if self.s.shape[1] != len(self.frequencies):
            raise ValueError('the frequency count must match the s array')
        if np.any(np.diff(self.frequencies) <= 0):
            raise ValueError('frequencies must increase')
        self.termination = complex(termination)
        self.fixture = None if fixture is None else np.asarray(fixture, complex)
        self.interpolator = build_interpolator(self.positions, self.s, phase_axis,
                                               level_policy)

    # -- frequency handling ----------------------------------------------
    def frequency_slice(self, frequency: float) -> tuple[int, int, float]:
        """The two calibration slices that bracket a frequency, and the weight.

        A measurement taken on the instrument's own frequency grid has to be
        reduced the same way as the model, so both use this helper.
        """
        value = float(frequency)
        if not self.frequencies[0] <= value <= self.frequencies[-1]:
            raise ValueError('frequency outside the calibration band')
        upper = int(np.searchsorted(self.frequencies, value))
        if upper < len(self.frequencies) and self.frequencies[upper] == value:
            return upper, upper, 0.0
        lower = upper - 1
        span = self.frequencies[upper] - self.frequencies[lower]
        weight = (value - self.frequencies[lower]) / span
        return lower, upper, float(weight)

    def _frequency_slice(self, frequency: float) -> tuple[int, int, float]:
        return self.frequency_slice(frequency)

    def s_at(self, position: Sequence[float], frequency: float) -> np.ndarray:
        """Interpolated S-parameter matrix at a position."""
        lower, upper, weight = self._frequency_slice(frequency)
        low = self.interpolator.evaluate(position, lower)
        if weight == 0.0:
            return low
        high = self.interpolator.evaluate(position, upper)
        return low * (1.0 - weight) + high * weight

    def gamma_at(self, position: Sequence[float], frequency: float) -> complex:
        """Reflection coefficient the tuner presents at a position."""
        s = self.s_at(position, frequency)
        return complex(RF.dut_gamma(s[None, :, :], self.termination, self.fixture)[0])

    def measured_gammas(self, frequency: float) -> np.ndarray:
        """Reflection coefficient of every measured point."""
        lower, upper, weight = self._frequency_slice(frequency)
        gammas = np.asarray(
            RF.dut_gamma(self.s[:, lower, :, :], self.termination, self.fixture),
            dtype=complex)
        if weight:
            high = np.asarray(
                RF.dut_gamma(self.s[:, upper, :, :], self.termination, self.fixture),
                dtype=complex)
            gammas = gammas * (1.0 - weight) + high * weight
        return gammas

    # -- tuning ----------------------------------------------------------
    def _clip(self, position: np.ndarray, limits) -> np.ndarray:
        point = np.asarray(position, dtype=float).copy()
        if limits is None:
            return point
        for index, bounds in enumerate(limits):
            low, high = float(bounds[0]), float(bounds[1])
            point[index] = min(max(point[index], low), high)
        return point

    def general_search(self, target_gamma: complex, frequency: float, limits=None):
        """Stage one: the calibrated point that comes closest to the target.

        Points outside the declared travel box are ignored: the tuner may not
        propose a position the machine is not allowed to reach.
        """
        gammas = self.measured_gammas(frequency)
        if not len(gammas):
            raise ValueError('the calibration set is empty')
        candidates = np.ones(len(gammas), dtype=bool)
        if limits is not None:
            for index, bounds in enumerate(limits):
                low, high = float(bounds[0]), float(bounds[1])
                candidates &= (self.positions[:, index] >= low - 1e-12)
                candidates &= (self.positions[:, index] <= high + 1e-12)
        if not candidates.any():
            raise ValueError('no calibrated point lies inside the travel limits')
        distances = np.abs(gammas - complex(target_gamma))
        distances = np.where(candidates, distances, np.inf)
        index = int(np.argmin(distances))
        position = self.positions[index].copy()
        return {
            'stage': 'general_search',
            'index': index,
            'position': position,
            'gamma': complex(gammas[index]),
            'error': float(distances[index]),
        }

    def gradient_search(self, start: np.ndarray, target_gamma: complex,
                        frequency: float, limits=None, max_iterations: int = 60
                        ) -> dict:
        """Stage two: local search on the interpolated model.

        The objective is the vector distance between the interpolated reflection
        coefficient and the target.  The search is derivative-free because the
        interpolant is only piecewise smooth; it explores the same fine grid that
        the gradient search uses.
        """
        from scipy.optimize import minimize

        target = complex(target_gamma)
        initial = self._clip(np.asarray(start, dtype=float), limits)

        def objective(candidate):
            point = self._clip(np.asarray(candidate, dtype=float), limits)
            return float(abs(self.gamma_at(point, frequency) - target) ** 2)

        result = minimize(objective, initial, method='Powell',
                          bounds=[tuple(bounds) for bounds in limits] if limits else None,
                          options={'maxiter': max_iterations, 'xtol': 1e-4, 'ftol': 1e-12})
        point = self._clip(result.x, limits)
        gamma = self.gamma_at(point, frequency)
        return {
            'stage': 'gradient_search',
            'position': point,
            'gamma': complex(gamma),
            'error': float(abs(gamma - target)),
            'iterations': int(getattr(result, 'nit', 0)),
        }

    def fine_grid_search(self, centre: np.ndarray, target_gamma: complex,
                         frequency: float, limits=None, levels: int = 5,
                         span: float = 0.05) -> dict:
        """Stage three: a fine interpolation grid around the estimate."""
        target = complex(target_gamma)
        middle = np.asarray(centre, dtype=float)
        best_point = middle.copy()
        best_gamma = self.gamma_at(best_point, frequency)
        best_error = float(abs(best_gamma - target))
        if levels < 2:
            return {'stage': 'fine_grid', 'position': best_point,
                    'gamma': complex(best_gamma), 'error': best_error, 'samples': 1}
        offsets = np.linspace(-span, span, int(levels))
        axes = []
        for index in range(len(middle)):
            axes.append(middle[index] + offsets)
        grids = np.meshgrid(*axes, indexing='ij')
        samples = grids[0].size
        flat = [grids[axis].ravel() for axis in range(len(middle))]
        for candidate in zip(*flat):
            point = self._clip(np.asarray(candidate, dtype=float), limits)
            gamma = self.gamma_at(point, frequency)
            error = float(abs(gamma - target))
            if error < best_error:
                best_error = error
                best_gamma = gamma
                best_point = point
        return {'stage': 'fine_grid', 'position': best_point,
                'gamma': complex(best_gamma), 'error': best_error, 'samples': samples}

    def tune(self, target_gamma: complex, frequency: float, limits=None,
             method: str = 'two-stage', grid_levels: int = 5,
             grid_span: float = 0.05) -> dict:
        """Tune to a target impedance.

        ``method='general'`` stops after the general search, ``'gradient'`` adds the
        local search on the interpolated model, and ``'two-stage'`` (default) runs
        both and then refines on a fine grid.
        """
        target = complex(target_gamma)
        stages = []
        first = self.general_search(target, frequency, limits)
        stages.append(first)
        best = first
        if method in ('gradient', 'two-stage'):
            second = self.gradient_search(first['position'], target, frequency, limits)
            stages.append(second)
            if second['error'] < best['error']:
                best = second
        if method == 'two-stage':
            third = self.fine_grid_search(best['position'], target, frequency,
                                          limits, grid_levels, grid_span)
            stages.append(third)
            if third['error'] < best['error']:
                best = third
        return {
            'position': best['position'].copy(),
            'gamma': complex(best['gamma']),
            'error': float(best['error']),
            'method': method,
            'interpolator': self.interpolator.kind,
            'stages': [{k: v for k, v in stage.items() if k != 'position'}
                       | {'position': [float(x) for x in stage['position']]}
                       for stage in stages],
            'predicted': True,
        }


# --------------------------------------------------------------------------- #
# P0 additions: explicit interpolation-mode selection and error report
# --------------------------------------------------------------------------- #
def select_interpolation_mode(plan: str, positions: np.ndarray, *,
                              explicit: str | None = None) -> dict:
    """Decide tensor vs scattered interpolation explicitly (no silent fallback).

    ``plan=='cartesian'`` expects a complete tensor grid.  Completeness is
    checked as ``prod(len(np.unique(axis))) == number_of_points``.  When the
    grid is incomplete the result is *explicitly* degraded: ``degraded=True``,
    ``mode='scattered'`` and the Chinese ``reason`` explains why.  ``smith`` and
    ``gamma_rings`` plans use scattered interpolation without degradation.
    ``explicit`` overrides the mode ('tensor'/'scattered'); an illegal value
    raises ``ValueError``.
    """
    key = str(plan).strip().lower()
    if explicit is not None and explicit not in ('tensor', 'scattered'):
        raise ValueError(
            f"explicit 只能是 'tensor' 或 'scattered'，收到 {explicit!r}")

    arr = np.asarray(positions, dtype=float)
    if arr.ndim == 1:
        points = arr.reshape(-1, 1)
    elif arr.ndim == 2:
        points = arr
    elif arr.ndim > 2:
        points = arr.reshape(arr.shape[0], -1)
    else:
        points = np.empty((0, 0), dtype=float)
    n = int(points.shape[0])
    d = int(points.shape[1]) if points.ndim == 2 else 0
    product = 1
    finite = bool(n > 0 and d > 0 and np.isfinite(points).all())
    if finite:
        for column in range(d):
            product *= int(np.unique(points[:, column]).size)
    grid_complete = bool(finite and product == n)

    expected = 'tensor' if key == 'cartesian' else 'scattered'
    requested = explicit if explicit is not None else expected

    if explicit is not None:
        mode = explicit
        explicit_flag = True
        degraded = bool(mode == 'tensor' and not grid_complete)
        if degraded:
            reason = ('显式指定张量插值，但位置集不构成完整张量网格'
                      f'（各轴层数之积 {product} ≠ 点数 {n}），张量插值不可用')
        elif expected == 'tensor' and not grid_complete:
            reason = '显式指定散点插值；笛卡尔计划的张量网格本就不完整'
        else:
            reason = f'由调用方显式指定为 {mode} 插值'
    else:
        explicit_flag = False
        if key == 'cartesian':
            if grid_complete:
                mode, degraded = 'tensor', False
                reason = '笛卡尔计划且位置集为完整张量网格，采用二阶拉格朗日张量插值'
            else:
                mode, degraded = 'scattered', True
                reason = ('笛卡尔计划本应使用张量插值，但位置集不是完整张量网格'
                          f'（各轴层数之积 {product} ≠ 点数 {n}）；已显式降级为散点插值，'
                          '不能静默当作张量网格使用')
        else:
            mode, degraded = 'scattered', False
            reason = f'{key} 计划采用散点插值（不要求完整张量网格）'

    return {
        'mode': mode,
        'reason': reason,
        'degraded': bool(degraded),
        'grid_complete': grid_complete,
        'requested': requested,
        'explicit': bool(explicit_flag),
    }


def interpolation_error_report(positions: np.ndarray, s: np.ndarray, mode: str,
                               max_samples: int = 24) -> dict:
    """Leave-one-out error estimate for the chosen interpolation mode.

    Each sampled point is estimated from its nearest neighbours (inverse-
    distance weighted local estimate) with the point itself excluded; the
    absolute error between the estimate and the sample's response (|S11| or
    |Gamma|) is reported overall and split into three |Gamma|/|S11| layers.
    All numeric fields are finite floats.  Degenerate inputs (too few points,
    wrong dimensions, non-finite data) return a safe zeroed default rather than
    raising.  This is a diagnostic only and does not touch
    :func:`build_interpolator`.
    """
    safe = {
        'mode': str(mode),
        'samples': 0,
        'mean_error': 0.0,
        'max_error': 0.0,
        'per_layer': [],
    }
    try:
        arr = np.asarray(positions, dtype=float)
        if arr.ndim == 1:
            points = arr.reshape(-1, 1)
        elif arr.ndim == 2:
            points = arr
        elif arr.ndim > 2:
            points = arr.reshape(arr.shape[0], -1)
        else:
            return safe
        n, d = points.shape
        if n < 2 or d < 1:
            return safe
        vals = np.asarray(s)
        if vals.ndim == 0 or vals.shape[0] != n:
            return safe
        flat = vals.reshape(n, -1)
        response = np.asarray(np.abs(flat[:, 0]), dtype=float).ravel()
        response = np.nan_to_num(response, nan=0.0, posinf=0.0, neginf=0.0)

        clean = np.nan_to_num(points, nan=0.0, posinf=0.0, neginf=0.0)
        low = clean.min(axis=0)
        high = clean.max(axis=0)
        scale = high - low
        scale[scale <= 1e-12] = 1.0
        norm = (clean - low) / scale

        try:
            cap = int(max_samples)
        except (TypeError, ValueError):
            cap = n
        if cap < 1:
            cap = n
        count = min(n, cap)
        idx = np.unique(np.linspace(0, n - 1, count).astype(int))
        k = int(min(4, n - 1))
        errors: list[float] = []
        sampled_response: list[float] = []
        for i in idx:
            i = int(i)
            dist = np.linalg.norm(norm - norm[i], axis=1)
            dist[i] = np.inf
            neighbours = np.argsort(dist)[:k]
            weight = 1.0 / np.maximum(dist[neighbours], 1e-9)
            estimate = float(np.dot(weight, response[neighbours]) / weight.sum())
            error = abs(estimate - float(response[i]))
            if np.isfinite(error):
                errors.append(float(error))
                sampled_response.append(float(response[i]))
        if not errors:
            return safe
        errors_arr = np.asarray(errors, dtype=float)
        resp_arr = np.asarray(sampled_response, dtype=float)
        mean_error = float(errors_arr.mean())
        max_error = float(errors_arr.max())
        if not (np.isfinite(mean_error) and np.isfinite(max_error)):
            return safe
        rmin, rmax = float(resp_arr.min()), float(resp_arr.max())
        if not (np.isfinite(rmin) and np.isfinite(rmax)):
            return safe
        if rmax <= rmin:
            edges = np.array([rmin, rmin, rmin, rmin + 1.0], dtype=float)
        else:
            edges = np.linspace(rmin, rmax, 4)
        per_layer = []
        for layer in range(3):
            lo, hi = float(edges[layer]), float(edges[layer + 1])
            if layer == 2:
                mask = (resp_arr >= lo) & (resp_arr <= hi)
            else:
                mask = (resp_arr >= lo) & (resp_arr < hi)
            layer_errors = errors_arr[mask]
            per_layer.append({
                'layer': int(layer),
                'gamma_min': float(lo),
                'gamma_max': float(hi),
                'mean_error': float(layer_errors.mean()) if layer_errors.size else 0.0,
                'max_error': float(layer_errors.max()) if layer_errors.size else 0.0,
                'count': int(layer_errors.size),
            })
        return {
            'mode': str(mode),
            'samples': int(len(errors)),
            'mean_error': mean_error,
            'max_error': max_error,
            'per_layer': per_layer,
        }
    except Exception:
        return safe


__all__ += [
    'select_interpolation_mode',
    'interpolation_error_report',
]

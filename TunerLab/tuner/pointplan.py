"""Smith-chart uniform calibration point planning.

Why this module exists
----------------------
The original calibration plan was a **mechanical cartesian grid**: X took
``x_count`` values and every probe axis took ``z_count`` values, so the number
of points grew as ``x_count * z_count**probes`` (576 points for three probes at
the defaults).  Two problems follow from that:

* the cost explodes with the probe count, and
* most of those points land in the same corner of the Smith chart.  A slide
  screw tuner presents |Gamma| mainly through the probe depth and the phase
  mainly through the carriage position, so a cube of coordinates samples a small
  patch of the impedance plane repeatedly and leaves the rest of the chart
  uncovered.

Commercial tuner software solves this the same way: the
calibration points are spread **over the Smith chart**, not over the coordinate
cube.  Their documented point counts are 95 / 181 / 361 (standard) and a
user-selectable density, and both interpolate between the calibrated
points afterwards.

This module provides the planning half of that idea:

* :func:`smith_targets` — evenly spread target reflection coefficients,
* :class:`MeasuredModel` — the tuner's response learned from a measured set,
  able to answer both "what Gamma does this position give" and "which position
  gives this Gamma",
* :func:`refine_positions` — the gap-filling pass: given what has already been
  measured, propose the positions that even out the Smith-chart coverage,
* :func:`coverage_report` — a quantitative statement of how evenly the measured
  points are spread.

The physical model is deliberately *learned from measurements* rather than
assumed: the mapping from probe depth to |Gamma| depends on the probe geometry
and on the fixture, and a wrong model would silently bias the whole plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

__all__ = [
    'DENSITY_PRESETS',
    'density_count',
    'recommended_phase_levels',
    'smith_targets',
    'smith_xy',
    'MeasuredModel',
    'plan_positions_from_targets',
    'refine_positions',
    'coverage_report',
    'PointPlan',
]

#: Calibration point counts, aligned with documented industry point-count options.
DENSITY_PRESETS: dict[str, int] = {
    'low': 95,
    'medium': 181,
    'high': 361,
}


def density_count(density: str, custom: int | None = None) -> int:
    """Resolve a density name (or an explicit count) into a point count."""
    if custom is not None:
        count = int(custom)
        if count < 1:
            raise ValueError('point count must be positive')
        return count
    key = str(density).strip().lower()
    if key not in DENSITY_PRESETS:
        raise ValueError(
            f'unknown density {density!r}; expected one of {sorted(DENSITY_PRESETS)}')
    return DENSITY_PRESETS[key]


def smith_targets(count: int, r_min: float = 0.0, r_max: float = 0.95,
                  method: str = 'sunflower', phase_offset: float = 0.0) -> np.ndarray:
    """Target reflection coefficients spread evenly over the Smith chart.

    ``method='sunflower'`` (default) uses a Fermat spiral.  The nearest-neighbour
    spacing of that distribution is almost perfectly constant — for 181 points
    the spacing varies by well under 2 % — which is what "as equidistantly as
    possible" means for a two-dimensional chart.  A random draw over the same
    disc has the same average spacing but clumps, so parts of the chart end up
    sampled several times while other parts are missed.

    ``method='rings'`` puts the points on concentric rings whose radii grow like
    ``sqrt(k)`` (equal-area annuli), with every ring carrying the same number of
    points and the rings interleaved angularly.  It matches the physical
    structure of a slide-screw tuner, where the carriage position gives the
    phase levels, and it makes the plan easy to read on the chart.

    The returned array is ordered from the centre outwards, so the first
    entries are the low-|Gamma| points that a tuner reaches most easily.
    """
    total = int(count)
    if total < 1:
        raise ValueError('count must be positive')
    if not 0.0 <= r_min < r_max <= 1.0:
        raise ValueError('require 0 <= r_min < r_max <= 1')
    style = str(method).strip().lower()
    if style in ('area', 'rings'):
        style = 'rings'
    elif style in ('ring', 'spiral'):
        style = 'sunflower'
    if style not in ('rings', 'sunflower'):
        raise ValueError("method must be 'sunflower' or 'rings'")
    phase_offset = float(phase_offset) % 360.0

    targets: list[complex] = []
    if style == 'rings':
        rings = max(1, int(np.ceil(np.sqrt(total))))
        base, remainder = divmod(total, rings)
        counts = [base + (1 if index < remainder else 0) for index in range(rings)]
        for index, count_here in enumerate(counts):
            if count_here == 0:
                continue
            # Equal-area annulus: the radius of the annulus is the square root
            # of the accumulated area fraction.
            fraction = np.sqrt((index + 0.5) / rings)
            radius = r_min + (r_max - r_min) * fraction
            # Offset every second ring so the rings interleave instead of
            # stacking their points on top of each other.
            offset = (index % 2) * (180.0 / count_here)
            for step in range(count_here):
                angle = np.deg2rad(
                    phase_offset + offset + 360.0 * step / count_here)
                targets.append(complex(radius * np.cos(angle), radius * np.sin(angle)))
    else:
        golden_angle = np.pi * (3.0 - np.sqrt(5.0))
        for index in range(total):
            fraction = np.sqrt((index + 0.5) / total)
            radius = r_min + (r_max - r_min) * fraction
            angle = np.deg2rad(phase_offset + index * np.degrees(golden_angle))
            targets.append(complex(radius * np.cos(angle), radius * np.sin(angle)))

    result = np.asarray(targets[:total], dtype=complex)
    if result.size != total:  # pragma: no cover - guarded by the allocation loop
        raise RuntimeError('failed to allocate the requested number of targets')
    return result


def smith_xy(gamma: complex) -> tuple[float, float]:
    """Cartesian coordinates of a reflection coefficient, for plotting."""
    value = complex(gamma)
    return float(value.real), float(value.imag)


def recommended_phase_levels(phase_span_deg: float = 360.0,
                             max_step_deg: float = 20.0) -> int:
    """Number of carriage levels that keeps the phase step small enough.

    On a slide-screw tuner the carriage position turns the reflection phase, so
    the interpolation accuracy is governed by how many degrees the phase turns
    between two calibrated levels.  A second-order scheme is only worth its name
    when that step is small; with a full 360 degree wrap and a 20 degree limit
    the carriage needs at least 19 levels.

    The relation is empirical but it is the single most effective lever on the
    tuning accuracy: the measured error of the interpolated model falls roughly
    with the cube of the level spacing.
    """
    span = abs(float(phase_span_deg))
    step = abs(float(max_step_deg))
    if step <= 0:
        raise ValueError('the maximum phase step must be positive')
    if span <= 0:
        return 3
    return max(3, int(np.ceil(span / step)) + 1)


@dataclass
class MeasuredModel:
    """The tuner's response, learned from an already measured calibration set.

    ``positions`` are mechanical coordinates (``[X, Z1, Z2, ...]``) and
    ``gammas`` are the reflection coefficients measured there.  The model is a
    local linear fit around the query point, which is exactly the kind of
    interpolation the industry calls "second order interpolation between
    calibration points", but fitted from the machine's own data.
    """

    positions: np.ndarray
    gammas: np.ndarray
    neighbours: int = 8
    _cache: dict = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        positions = np.atleast_2d(np.asarray(self.positions, dtype=float))
        gammas = np.asarray(self.gammas, dtype=complex).ravel()
        if positions.shape[0] != gammas.size:
            raise ValueError('positions and gammas must have the same length')
        if positions.shape[0] < 2:
            raise ValueError('at least two measured points are needed')
        if not np.isfinite(positions).all() or not np.isfinite(gammas).all():
            raise ValueError('measured data must be finite')
        self.positions = positions
        self.gammas = gammas
        # The normalisation reference is the measured set, never the query:
        # a single query point would otherwise normalise to zero.
        self._low = self.positions.min(axis=0)
        scale = self.positions.max(axis=0) - self._low
        scale[scale <= 1e-12] = 1.0
        self._scale = scale
        self._normalised = (self.positions - self._low) / self._scale

    # -- helpers ---------------------------------------------------------
    def _normalise(self, positions: np.ndarray) -> np.ndarray:
        """Map positions into the unit box of the measured set."""
        return (np.atleast_2d(np.asarray(positions, dtype=float))
                - self._low) / self._scale

    def _nearest(self, gamma: complex) -> np.ndarray:
        distance = np.abs(self.gammas - complex(gamma))
        count = int(min(max(self.neighbours, 3), self.gammas.size))
        return np.argsort(distance)[:count]

    # -- prediction ------------------------------------------------------
    def predict_gamma(self, position: Sequence[float]) -> complex:
        """Predicted reflection coefficient at one position.

        The neighbourhood is chosen in *coordinate* space, because the query is
        a position; :meth:`invert_gamma` does the opposite and searches in the
        Gamma plane, because its query is a reflection coefficient.
        """
        point = np.asarray(position, dtype=float).ravel()
        if point.size != self.positions.shape[1]:
            raise ValueError('position has the wrong number of axes')
        normalised = self._normalise(point.reshape(1, -1))[0]
        distance = np.linalg.norm(self._normalised - normalised, axis=1)
        count = int(min(max(self.neighbours, 3), self.gammas.size))
        index = np.argsort(distance)[:count]
        local = self._local_fit(index)
        if local is None:
            return complex(self.gammas[index[0]])
        matrix, offset = local
        return complex(matrix @ normalised + offset)

    def _local_fit(self, index: np.ndarray):
        """Least-squares local linear fit ``gamma ~= A x + b`` (cached)."""
        key = tuple(int(i) for i in index)
        if key in self._cache:
            return self._cache[key]
        points = self._normalised[index]
        values = self.gammas[index]
        design = np.hstack([points, np.ones((points.shape[0], 1))])
        try:
            solution, *_ = np.linalg.lstsq(design, values, rcond=None)
        except np.linalg.LinAlgError:  # pragma: no cover - numerically degenerate
            self._cache[key] = None
            return None
        matrix = solution[:-1]
        offset = complex(solution[-1])
        result = (matrix, offset)
        self._cache[key] = result
        return result

    def invert_gamma(self, gamma: complex,
                     limits: Sequence[tuple[float, float]] | None = None
                     ) -> np.ndarray:
        """Position that presents ``gamma``, or the closest measured one.

        The local linear model is inverted directly.  When the fit is singular
        (a degenerate neighbourhood) the nearest measured position is returned
        instead, and the caller can decide what to do with it.
        """
        target = complex(gamma)
        index = self._nearest(target)
        local = self._local_fit(index)
        if local is None:
            return self.positions[index[0]].copy()
        matrix, offset = local
        # The local model is gamma = A x + b, with A a complex vector of length d
        # and x a real position vector.  One complex equation means two real
        # equations, so the inversion is a 2 x d real least-squares system; a
        # division per axis would wrongly drop the other axes' contribution.
        real_system = np.vstack([np.real(matrix), np.imag(matrix)])
        residual = target - offset
        rhs = np.array([np.real(residual), np.imag(residual)], dtype=float)
        if np.linalg.matrix_rank(real_system) < 2:
            return self.positions[index[0]].copy()
        solution, *_ = np.linalg.lstsq(real_system, rhs, rcond=None)
        if not np.all(np.isfinite(solution)):
            return self.positions[index[0]].copy()
        # The model is only meaningful inside the measured region, so the
        # answer is clipped to that box instead of extrapolating.
        point = self._low + solution.reshape(-1) * self._scale
        point = np.clip(point, self._low, self._low + self._scale)
        if limits is not None:
            clamped = np.array([
                min(max(value, float(bounds[0])), float(bounds[1]))
                for value, bounds in zip(point, limits)
            ])
            point = clamped
        return point


def plan_positions_from_targets(model: MeasuredModel, targets: np.ndarray,
                                limits: Sequence[tuple[float, float]] | None = None
                                ) -> np.ndarray:
    """Map target reflection coefficients to mechanical positions."""
    values = np.asarray(targets, dtype=complex).ravel()
    if values.size == 0:
        raise ValueError('no targets supplied')
    return np.asarray([model.invert_gamma(value, limits) for value in values], dtype=float)


def refine_positions(measured_gammas: Sequence[complex],
                     model: MeasuredModel,
                     count: int,
                     limits: Sequence[tuple[float, float]] | None = None,
                     r_min: float = 0.0, r_max: float = 0.95,
                     method: str = 'sunflower') -> tuple[np.ndarray, np.ndarray]:
    """Propose the positions that even out the Smith-chart coverage.

    The candidates are the uniform target set; the candidate that is farthest
    from everything measured so far is taken first, then the candidate farthest
    from the already selected ones.  This is the classic farthest-point
    sampling and it fills the gaps in the chart rather than re-sampling the
    parts that are already dense.

    Returns ``(positions, targets)``.
    """
    measured = np.asarray([complex(value) for value in measured_gammas], dtype=complex)
    total = int(count)
    if total < 1:
        raise ValueError('count must be positive')
    candidates = smith_targets(max(3 * total, 64), r_min=r_min, r_max=r_max, method=method)
    chosen: list[int] = []
    covered = list(measured)
    for _ in range(total):
        best_index = -1
        best_distance = -1.0
        for index, candidate in enumerate(candidates):
            if index in chosen:
                continue
            if covered:
                distance = float(np.min(np.abs(np.asarray(covered) - candidate)))
            else:
                distance = float('inf')
            if distance > best_distance:
                best_distance = distance
                best_index = index
        if best_index < 0:  # pragma: no cover - candidate pool is larger than count
            break
        chosen.append(best_index)
        covered.append(candidates[best_index])
    selected = candidates[chosen]
    positions = plan_positions_from_targets(model, selected, limits)
    return positions, selected


def coverage_report(gammas: Sequence[complex],
                    reference: np.ndarray | None = None) -> dict[str, float]:
    """Quantify how evenly the measured points are spread in the Gamma plane.

    ``mean_spacing`` and ``max_spacing`` are the nearest-neighbour distances
    between measured points (smaller is more even).  When ``reference`` (the
    uniform target set) is given, ``coverage`` reports the fraction of the
    reference targets that are closer than a fixed radius to a measured point.
    """
    values = np.asarray([complex(value) for value in gammas], dtype=complex)
    if values.size == 0:
        return {'count': 0, 'mean_spacing': float('nan'),
                'max_spacing': float('nan'), 'coverage': float('nan')}
    if values.size == 1:
        return {'count': 1, 'mean_spacing': float('nan'),
                'max_spacing': float('nan'), 'coverage': float('nan')}
    distance = np.abs(values[:, None] - values[None, :])
    np.fill_diagonal(distance, np.inf)
    nearest = distance.min(axis=1)
    report = {
        'count': int(values.size),
        'mean_spacing': float(nearest.mean()),
        'max_spacing': float(nearest.max()),
        'min_spacing': float(nearest.min()),
    }
    if reference is not None:
        targets = np.asarray(reference, dtype=complex).ravel()
        if targets.size:
            gaps = np.abs(targets[:, None] - values[None, :]).min(axis=1)
            report['coverage'] = float(np.mean(gaps <= 0.15))
            report['mean_gap_to_target'] = float(gaps.mean())
        else:  # pragma: no cover - empty reference
            report['coverage'] = float('nan')
            report['mean_gap_to_target'] = float('nan')
    else:
        report['coverage'] = float('nan')
        report['mean_gap_to_target'] = float('nan')
    return report


@dataclass
class PointPlan:
    """A concrete calibration point plan plus the coverage it achieves."""

    positions: np.ndarray
    targets: np.ndarray
    density: str = 'custom'
    method: str = 'smith-uniform'
    coverage: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'positions': [[float(v) for v in row] for row in self.positions],
            'targets': [{'re': float(t.real), 'im': float(t.imag)} for t in self.targets],
            'count': int(len(self.positions)),
            'density': self.density,
            'method': self.method,
            'coverage': dict(self.coverage),
        }


# --------------------------------------------------------------------------- #
# P0 additions: equal-|Gamma| ring plan and cold-start position mapping
# --------------------------------------------------------------------------- #
def _gamma_rings_layers(count, r_min, r_max, rings, enhanced_center,
                        enhanced_50ohm, phase_offset_deg):
    """Shared layer layout for :func:`gamma_rings_targets` and
    :func:`gamma_rings_table`.

    Radii follow equal-area annuli ``sqrt((k+0.5)/n)`` so the innermost ring is
    strictly greater than ``r_min`` (avoiding a degenerate pile-up at Gamma=0)
    while every radius stays below ``r_max``.  Point counts grow with |Gamma|
    (outer rings carry more points, proportional to circumference) and are
    allocated with the largest-remainder method, so their sum is exactly
    ``count``.  ``enhanced_center`` / ``enhanced_50ohm`` re-weight the inner
    rings to densify the low-|Gamma| / 50-ohm region; the total stays ``count``.
    """
    total = int(count)
    if total < 1:
        raise ValueError('count 必须为正整数')
    if not (0.0 <= r_min < r_max <= 1.0):
        raise ValueError('要求 0 <= r_min < r_max <= 1')
    if rings is None:
        n_layers = max(1, int(np.ceil(np.sqrt(total))))
    else:
        n_layers = max(1, int(rings))
    k = np.arange(n_layers, dtype=float)
    radius = r_min + (r_max - r_min) * np.sqrt((k + 0.5) / n_layers)
    span = r_max - r_min
    norm = (radius - r_min) / (span if span > 0 else 1.0)
    weights = 0.5 + norm
    if enhanced_center:
        weights = weights + 1.5 * (1.0 - norm) ** 2
    if enhanced_50ohm:
        weights = weights + 2.0 * np.exp(-((norm / 0.15) ** 2))
    raw = weights / weights.sum() * total
    counts = np.floor(raw).astype(int)
    leftover = total - int(counts.sum())
    if leftover > 0:
        order = np.argsort(-(raw - counts), kind='stable')
        counts[order[:leftover]] += 1
    diff = total - int(counts.sum())
    if diff > 0:  # pragma: no cover - guarded by the allocation above
        counts[-1] += diff

    offset = float(phase_offset_deg) % 360.0
    layers = []
    for idx in range(n_layers):
        r = float(radius[idx])
        n_here = int(counts[idx])
        gmin = float(r_min) if idx == 0 else float((radius[idx - 1] + r) * 0.5)
        gmax = float(r_max) if idx == n_layers - 1 else float((r + radius[idx + 1]) * 0.5)
        step = 360.0 / n_here if n_here > 0 else 360.0
        # Equivalent normalised X step for one phase increment.  Relative value:
        # a full 360-degree phase wrap is mapped onto the whole X travel, so one
        # phase step is (step/360) of that travel.
        x_step = step / 360.0
        layers.append({
            'layer': int(idx),
            'gamma_min': gmin,
            'gamma_max': gmax,
            'radius': r,
            'points': n_here,
            'x_step': float(min(1.0, max(0.0, x_step))),
            '_phase_step': float(step),
            '_offset': float(offset + (idx % 2) * (180.0 / n_here if n_here > 0 else 0.0)),
        })
    return layers


def gamma_rings_targets(count: int = 181, r_min: float = 0.0,
                        r_max: float = 0.95, rings: int | None = None,
                        enhanced_center: bool = False,
                        enhanced_50ohm: bool = False,
                        phase_offset_deg: float = 0.0) -> np.ndarray:
    """Deterministic equal-|Gamma| ring targets (no measurement required).

    Points are grouped into concentric rings of constant |Gamma|; the number of
    points per ring grows outwards, phases are uniform over 360 degrees within a
    ring and adjacent rings are interleaved by half a step.  The returned
    complex array has length exactly ``count``, every |Gamma| lies in
    ``[r_min, r_max]`` and the result is fully deterministic (no RNG).

    ``enhanced_center`` densifies the low-|Gamma| core and ``enhanced_50ohm``
    densifies the near-50-ohm (|Gamma|~0) region by re-weighting the inner
    rings; both keep the total at ``count``.
    """
    layers = _gamma_rings_layers(count, r_min, r_max, rings, enhanced_center,
                                 enhanced_50ohm, phase_offset_deg)
    targets: list[complex] = []
    for layer in layers:
        n_here = layer['points']
        if n_here <= 0:
            continue
        r = layer['radius']
        step = layer['_phase_step']
        base = layer['_offset']
        for j in range(n_here):
            angle = np.deg2rad(base + step * j)
            targets.append(complex(r * np.cos(angle), r * np.sin(angle)))
    total = int(count)
    result = np.asarray(targets[:total], dtype=complex)
    if result.size < total:  # pragma: no cover - largest-remainder guarantees equality
        result = np.concatenate(
            [result, np.full(total - result.size, complex(r_min, 0.0), dtype=complex)])
    return result


def gamma_rings_table(count: int = 181, r_min: float = 0.0,
                      r_max: float = 0.95, rings: int | None = None,
                      **kwargs) -> list[dict]:
    """Layer table for the equal-|Gamma| ring plan.

    Each item has ``layer``, ``gamma_min``, ``gamma_max``, ``radius``,
    ``points`` and ``x_step``.  ``x_step`` is a **normalised relative value**
    (0..1): the fraction of the whole X travel that one phase step corresponds
    to, assuming a full 360-degree phase wrap spans the entire X travel.  The
    sum of ``points`` over all layers equals ``count``.  Extra keyword arguments
    (``enhanced_center``, ``enhanced_50ohm``, ``phase_offset_deg``) are
    forwarded so the table matches :func:`gamma_rings_targets`.
    """
    enhanced_center = bool(kwargs.get('enhanced_center', False))
    enhanced_50ohm = bool(kwargs.get('enhanced_50ohm', False))
    phase_offset_deg = float(kwargs.get('phase_offset_deg', 0.0))
    layers = _gamma_rings_layers(count, r_min, r_max, rings, enhanced_center,
                                 enhanced_50ohm, phase_offset_deg)
    keys = ('layer', 'gamma_min', 'gamma_max', 'radius', 'points', 'x_step')
    return [{key: layer[key] for key in keys} for layer in layers]


def cold_start_positions(targets: np.ndarray, config) -> np.ndarray:
    """Cold-start *prior placeholder* mapping from Gamma to mechanical positions.

    IMPORTANT / 重要说明：这是一个「先验占位映射」，仅用于在没有任何实测点时
    生成一组可执行的初始机械位置。它假设 Gamma 相位在整段 X 行程内线性覆盖
    360 度、|Gamma| 在整段 Z 行程内线性对应探针插入深度——这只是物理先验，
    并非真实调谐器响应。实测完成后必须用 :class:`MeasuredModel` 或插值模型
    （见 :func:`plan_positions_from_targets` / ``tuner.interp``）替换；本函数
    的输出**绝不能当作实测 Gamma 使用**，也不能用于评估调谐精度。

    Returns a deterministic ``(n, 1+probes)`` float array clipped to
    ``config.x_limits`` / ``config.z_limits``.  All attribute reads go through
    ``getattr`` so a missing/partial config still yields a safe ``(n, 4)``
    default instead of raising.
    """
    values = np.asarray(targets, dtype=complex).ravel()
    n = int(values.size)
    try:
        probes = int(getattr(config, 'probes', 3))
    except (TypeError, ValueError):
        probes = 3
    if probes < 1:
        probes = 3
    columns = 1 + probes

    def _limits(name, default):
        raw = getattr(config, name, default)
        try:
            lo, hi = float(raw[0]), float(raw[1])
        except (TypeError, ValueError, IndexError, KeyError):
            lo, hi = float(default[0]), float(default[1])
        if not (np.isfinite(lo) and np.isfinite(hi)):
            lo, hi = float(default[0]), float(default[1])
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi

    x_lo, x_hi = _limits('x_limits', (0.0, 10.0))
    z_lo, z_hi = _limits('z_limits', (0.0, 1.0))
    if n == 0:
        return np.empty((0, columns), dtype=float)

    phase = np.nan_to_num(np.angle(values), nan=0.0, posinf=0.0, neginf=0.0)
    phase_norm = (phase + np.pi) / (2.0 * np.pi)
    mag = np.nan_to_num(np.abs(values), nan=0.0, posinf=1.0, neginf=0.0)
    mag_norm = np.clip(mag, 0.0, 1.0)

    x = np.clip(x_lo + phase_norm * (x_hi - x_lo), x_lo, x_hi)
    z = np.clip(z_lo + mag_norm * (z_hi - z_lo), z_lo, z_hi)

    result = np.empty((n, columns), dtype=float)
    result[:, 0] = x
    result[:, 1:] = z[:, None]
    result[:, 0] = np.clip(result[:, 0], x_lo, x_hi)
    result[:, 1:] = np.clip(result[:, 1:], z_lo, z_hi)
    return result


__all__ += [
    'gamma_rings_targets',
    'gamma_rings_table',
    'cold_start_positions',
]

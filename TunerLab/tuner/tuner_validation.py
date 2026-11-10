"""Validation of the interpolated tuning — the automated interpolation validation loop.

The validation loop works as follows: *the tuner is
commanded to tune a set of interpolated impedances for which the associated
S-parameters are measured from the calibrated VNA.  The error vector is calculated
and reported and if less than the generally accepted value, recommends
recalibration.*

This module implements that loop:

1. **Plan** positions that the interpolation *claims* to describe — between the
   calibrated levels, never on them, so the measurement really tests the
   interpolant instead of reproducing a measured point.
2. **Measure** at those positions with the calibrated VNA.
3. **Compare** the measured reflection coefficient with the model's prediction and
   report the error vector.
4. **Judge**: when the worst error exceeds the accepted tolerance, the report
   recommends a recalibration.

The error vector is reported in three complementary ways, because a single number
hides which part of the model is wrong:

* ``error`` — the vector distance ``|Γ_measured − Γ_model|`` in the reflection
  plane, the quantity standard references call the error vector;
* ``magnitude_error`` — how far the reflection magnitude is off, which is what a
  probe depth (gain) error produces;
* ``phase_error_deg`` — how far the reflection phase is off, which is what a
  carriage (phase) error produces.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import numpy as np

from .interp import TunerResponseModel
from .rf import RFNetworkEngine as RF

__all__ = [
    'DEFAULT_TOLERANCE',
    'STRATEGIES',
    'ValidationPoint',
    'plan_points',
    'evaluate',
    'summarise',
]

#: The accepted error vector.  Beyond this the interpolation is not trustworthy
#: and the tuner should be recalibrated.
DEFAULT_TOLERANCE = 0.08

#: How the validation points are chosen.
STRATEGIES = ('coverage', 'random')


@dataclass
class ValidationPoint:
    """One validation point and the outcome of its measurement."""

    index: int
    position: list[float]
    predicted_gamma: complex
    measured_gamma: complex
    error: float
    magnitude_error: float
    phase_error_deg: float
    within_tolerance: bool

    def to_dict(self) -> dict:
        return {
            'index': self.index,
            'position': list(self.position),
            'predicted_gamma': {'re': float(self.predicted_gamma.real),
                                'im': float(self.predicted_gamma.imag)},
            'measured_gamma': {'re': float(self.measured_gamma.real),
                               'im': float(self.measured_gamma.imag)},
            'error': float(self.error),
            'error_db': float(20.0 * np.log10(max(self.error, 1e-30))),
            'magnitude_error': float(self.magnitude_error),
            'phase_error_deg': float(self.phase_error_deg),
            'within_tolerance': bool(self.within_tolerance),
        }


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def _candidate_positions(model: TunerResponseModel, rng: np.random.Generator,
                         samples: int = 2000) -> np.ndarray:
    """Positions that lie *between* the calibrated levels."""
    interpolator = model.interpolator
    layout = getattr(interpolator, 'layout', None)
    if layout is not None:
        # A tensor grid: the cell centres are exactly the positions the
        # interpolation has to describe.
        axes = []
        for level in layout.levels:
            if len(level) < 2:
                return np.empty((0, layout.ndim))
            axes.append((level[:-1] + level[1:]) / 2.0)
        grids = np.meshgrid(*axes, indexing='ij')
        return np.column_stack([grid.ravel() for grid in grids])
    # Scattered data: sample inside the measured box and keep the points that
    # are far enough from every measured point to be genuinely interpolated.
    low = model.positions.min(axis=0)
    high = model.positions.max(axis=0)
    candidates = low + rng.random((samples, model.positions.shape[1])) * (high - low)
    distance = np.linalg.norm(
        (model.positions[None, :, :] - candidates[:, None, :]) /
        np.maximum(model.positions.max(axis=0) - model.positions.min(axis=0), 1e-12),
        axis=2).min(axis=1)
    threshold = max(float(np.median(distance)) * 0.5, 1e-6)
    return candidates[distance > threshold]


def plan_points(model: TunerResponseModel, count: int = 5,
                strategy: str = 'coverage', seed: int = 0,
                frequency: Optional[float] = None) -> np.ndarray:
    """Choose the positions to validate.

    ``strategy='coverage'`` picks the candidates whose predicted reflection
    coefficient is farthest from the calibrated points — the hardest places for
    the interpolant and therefore the most informative to verify.
    ``strategy='random'`` spreads the points evenly over the candidates.

    The selection is greedy farthest-point sampling in the reflection plane, so
    the points are never clustered.
    """
    total = int(count)
    if total < 1:
        raise ValueError('the number of validation points must be positive')
    style = str(strategy).strip().lower()
    if style not in STRATEGIES:
        raise ValueError(f'unknown strategy: {strategy!r}')
    rng = np.random.default_rng(int(seed))
    candidates = _candidate_positions(model, rng)
    if not len(candidates):
        raise ValueError('no interpolated positions are available for validation')
    if len(candidates) <= total:
        return candidates

    if frequency is None:
        frequency = float(model.frequencies[len(model.frequencies) // 2])
    measured = model.measured_gammas(frequency)

    if style == 'coverage':
        # Rank the candidates by how far they are from the calibrated set and
        # keep the hardest ones.
        predicted = np.asarray([model.gamma_at(point, frequency)
                                for point in candidates], dtype=complex)
        gaps = np.abs(predicted[:, None] - measured[None, :]).min(axis=1)
        order = np.argsort(-gaps)[:total]
        return candidates[order]

    # Evenly spread over the candidates.
    chosen = []
    remaining = list(range(len(candidates)))
    predicted = np.asarray([model.gamma_at(point, frequency)
                            for point in candidates], dtype=complex)
    while len(chosen) < total and remaining:
        if not chosen:
            pick = remaining.pop(0)
        else:
            distances = np.abs(predicted[remaining][:, None]
                               - predicted[chosen][None, :]).min(axis=1)
            pick = remaining[int(np.argmin(distances))]
            remaining.remove(pick)
        chosen.append(pick)
    return candidates[chosen]


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def evaluate(model: TunerResponseModel, points: Sequence[Sequence[float]],
             measure_s: Callable[[Sequence[float]], np.ndarray],
             tolerance: float = DEFAULT_TOLERANCE,
             frequency: Optional[float] = None,
             progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """Measure the validation points and compare them with the model.

    ``measure_s`` is called with a position and must return the measured
    S-parameter matrix (2x2, complex) at the validation frequency.
    """
    limit = float(tolerance)
    if limit < 0:
        raise ValueError('the tolerance must not be negative')
    if frequency is None:
        frequency = float(model.frequencies[len(model.frequencies) // 2])
    positions = [np.asarray(point, dtype=float).ravel() for point in points]
    if not positions:
        raise ValueError('no validation points supplied')

    checks: list[ValidationPoint] = []
    for index, position in enumerate(positions):
        predicted = model.gamma_at(position, frequency)
        measured_s = np.asarray(measure_s(position), dtype=complex)
        if measured_s.shape != (2, 2):
            raise ValueError('the measurement must return a 2x2 S-parameter matrix')
        measured = complex(RF.dut_gamma(
            measured_s[None, :, :], model.termination, model.fixture)[0])
        difference = measured - predicted
        magnitude_error = float(abs(abs(measured) - abs(predicted)))
        phase_error = float(np.degrees(np.angle(
            measured / predicted)) if abs(predicted) > 1e-15 else 0.0)
        error = float(abs(difference))
        checks.append(ValidationPoint(
            index=index,
            position=[float(v) for v in position],
            predicted_gamma=complex(predicted),
            measured_gamma=measured,
            error=error,
            magnitude_error=magnitude_error,
            phase_error_deg=phase_error,
            within_tolerance=bool(error <= limit)))
        if progress is not None:
            progress(index + 1, len(positions))

    report = summarise(checks, tolerance=limit, frequency=frequency)
    report['points'] = [check.to_dict() for check in checks]
    return report


def summarise(checks: Sequence[ValidationPoint], tolerance: float = DEFAULT_TOLERANCE,
              frequency: float = 0.0) -> dict:
    """Turn the individual checks into the overall verdict."""
    errors = np.asarray([check.error for check in checks], dtype=float)
    worst = float(errors.max()) if errors.size else float('nan')
    passed = bool(errors.size and worst <= tolerance)
    failed = [check.index for check in checks if not check.within_tolerance]

    # Which part of the model is off?  The magnitude error is a probe-depth
    # (gain) problem, the phase error a carriage (phase) problem, and the two
    # call for different remedies.
    magnitude = float(np.mean([check.magnitude_error for check in checks])) if checks else 0.0
    phase_plane = 0.0
    if checks:
        phase_plane = float(np.mean([
            abs(check.predicted_gamma) * np.radians(check.phase_error_deg)
            for check in checks]))
    if passed:
        message = (f'全部 {len(checks)} 个插值点的误差矢量均在容差 {tolerance:g} 之内'
                   f'（最大 {worst:.6f}），插值模型可信。')
    else:
        message = (f'{len(failed)} / {len(checks)} 个插值点超出容差 {tolerance:g}'
                   f'（最大误差矢量 {worst:.6f}），建议重新标定。')
        if magnitude > phase_plane:
            message += ('误差以幅度分量为主（幅度 {magnitude:.4f} vs 相位 {phase:.4f}）：'
                        '先怀疑标定点太稀、探针深度标度或增益不对。'
                        .format(magnitude=magnitude, phase=phase_plane))
        elif phase_plane > 0:
            message += ('误差以相位分量为主（幅度 {magnitude:.4f} vs 相位 {phase:.4f}）：'
                        '先怀疑 X 相位层数不足（每格相位跨度太大）或载物台行程标度不对。'
                        .format(magnitude=magnitude, phase=phase_plane))
    return {
        'kind': 'interpolation_validation',
        'tolerance': float(tolerance),
        'frequency_hz': float(frequency),
        'count': int(len(checks)),
        'max_error': worst,
        'mean_error': float(errors.mean()) if errors.size else float('nan'),
        'rms_error': float(np.sqrt((errors ** 2).mean())) if errors.size else float('nan'),
        'max_error_db': float(20.0 * np.log10(max(worst, 1e-30))),
        'mean_magnitude_error': magnitude,
        'mean_phase_error_plane': phase_plane,
        'passed': passed,
        'verdict': 'pass' if passed else 'recalibration_recommended',
        'failed_points': failed,
        'message': message,
        'rf_hardware_verified': False,
        'real_measurements': True,
    }

"""P0 frequency plan: four frequency modes and VNA application.

Pure standard library + numpy; never imports UI or hardware modules.
``build_frequency_plan`` produces a validated frequency grid, and
``apply_frequency_plan`` pushes it onto a VNA-like object.

Uniform modes are pushed through ``set_sweep(start, stop, points)``.  For
non-uniform list modes (``harmonic_list`` / ``arbitrary_list``) the exact grid
is installed with ``set_frequency_grid(freqs)`` when the VNA exposes it: the
driver then visits each requested frequency as a single-point sweep instead
of collapsing the list into an equal-step ``linspace``.  Analyzers that only
offer ``set_sweep`` keep the documented linear approximation; they are still
never left on a stale 5 GHz single point just because the operator picked the
"arbitrary list" mode.  VNA-like objects with no ``set_frequency_grid``
(mock analyzers, older callers) simply take the plan grid directly.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

__all__ = ['FREQUENCY_MODES', 'build_frequency_plan', 'apply_frequency_plan']

FREQUENCY_MODES = ('fo_list', 'freq_sweep', 'harmonic_list', 'arbitrary_list')

_UNIFORM_TOL = 1e-9


def _is_uniform(freqs, tol: float = _UNIFORM_TOL) -> bool:
    """Whether adjacent spacings are equal within a relative tolerance."""
    arr = np.asarray(freqs, dtype=float).ravel()
    if arr.size <= 2:
        return True
    diff = np.diff(arr)
    mean = float(diff.mean())
    if not np.isfinite(mean) or abs(mean) <= 0.0:
        return False
    rel = float(np.max(np.abs(diff - mean)) / abs(mean))
    return bool(rel <= tol)


def _validate(freqs, *, require_uniform: bool = False,
              label: str = '频率') -> np.ndarray:
    """Validate finite / positive / strictly-increasing / (optional) uniform."""
    arr = np.asarray(freqs, dtype=float).ravel()
    if arr.size == 0:
        raise ValueError(f'{label}列表不能为空')
    if not np.isfinite(arr).all():
        raise ValueError(f'{label}必须全部为有限数值（不能含 nan 或 inf）')
    if np.any(arr <= 0):
        raise ValueError(f'{label}必须全部为正数（大于 0 Hz）')
    if arr.size > 1:
        diff = np.diff(arr)
        if np.any(diff == 0):
            raise ValueError(f'{label}存在重复频点，必须严格递增')
        if np.any(diff < 0):
            raise ValueError(f'{label}必须严格递增（不允许递减）')
    if require_uniform and not _is_uniform(arr, _UNIFORM_TOL):
        raise ValueError(
            f'{label}序列不是等间隔（相邻步进相对误差超过 {_UNIFORM_TOL:g}）')
    return arr


def build_frequency_plan(mode: str = 'fo_list', *, fo_hz: float = 5e9,
                         start_hz: float | None = None,
                         stop_hz: float | None = None, points: int = 1,
                         harmonics: Sequence[int] | None = None,
                         frequencies_hz: Sequence[float] | None = None) -> dict:
    """Build a validated frequency plan for one of :data:`FREQUENCY_MODES`.

    ``fo_list`` is the default and is the single-frequency path: it always
    returns exactly ``[fo_hz]`` (5 GHz by default) with ``points == 1``, so the
    existing single-spot calibration workflow is unchanged.
    """
    key = str(mode).strip().lower()
    if key not in FREQUENCY_MODES:
        raise ValueError(f'未知频率模式 {mode!r}；应为 {FREQUENCY_MODES} 之一')

    fo = float(fo_hz)
    if not np.isfinite(fo) or fo <= 0:
        raise ValueError('fo_hz（中心频率）必须为有限正数')

    warnings: list[str] = []
    harmonic_numbers: list[int] = []

    if key == 'fo_list':
        freqs = np.asarray([fo], dtype=float)
        if int(points) != 1:
            warnings.append('fo_list 为单频点模式，已忽略 points 参数（强制 points=1）')
        _validate(freqs, require_uniform=True)

    elif key == 'freq_sweep':
        if frequencies_hz is not None:
            # An explicitly supplied sweep grid is accepted but must be uniform.
            seq = np.asarray(frequencies_hz, dtype=float).ravel()
            _validate(seq, require_uniform=True, label='扫频')
            freqs = seq
        else:
            if start_hz is None or stop_hz is None:
                raise ValueError(
                    'freq_sweep 需要 start_hz 与 stop_hz（或提供 frequencies_hz）')
            s, e = float(start_hz), float(stop_hz)
            n = int(points)
            if not (np.isfinite(s) and np.isfinite(e)):
                raise ValueError('start_hz / stop_hz 必须为有限数值')
            if s <= 0 or e <= 0:
                raise ValueError('start_hz / stop_hz 必须为正数（大于 0 Hz）')
            if n < 1:
                raise ValueError('points 必须 >= 1')
            if n == 1:
                if s != e:
                    warnings.append('points=1，扫频退化为单点，stop_hz 固定为 start_hz')
                freqs = np.asarray([s], dtype=float)
            else:
                if s >= e:
                    raise ValueError(
                        '多点扫频要求 0 < start_hz < stop_hz（非递增输入被拒绝）')
                freqs = np.linspace(s, e, n)
            _validate(freqs, require_uniform=True, label='扫频')

    elif key == 'harmonic_list':
        if harmonics is None:
            harmonic_seq = (1, 2, 3)
        else:
            harmonic_seq = tuple(int(v) for v in harmonics)
        if not harmonic_seq:
            raise ValueError('harmonics 不能为空')
        if any(v <= 0 for v in harmonic_seq):
            raise ValueError('谐波次数必须为正整数')
        harmonic_numbers = list(harmonic_seq)
        freqs = np.asarray([fo * v for v in harmonic_numbers], dtype=float)
        _validate(freqs, require_uniform=False, label='谐波频率')
        if not _is_uniform(freqs):
            warnings.append('谐波频率不等间隔；列表模式仅本地网格，仪器侧需逐点设置')

    else:  # arbitrary_list
        if frequencies_hz is None:
            raise ValueError('arbitrary_list 需要 frequencies_hz 显式频率列表')
        seq = np.asarray(frequencies_hz, dtype=float).ravel()
        if seq.size == 0:
            raise ValueError('frequencies_hz 不能为空')
        seq = np.sort(seq)
        _validate(seq, require_uniform=False, label='任意频率')
        freqs = seq
        if not _is_uniform(freqs):
            warnings.append('频率列表不等间隔；列表模式仅本地网格，仪器侧需逐点设置')

    # Final guard: every mode must yield a finite, positive, strictly increasing
    # (single point excepted) grid, as required by database.create.
    freqs = _validate(freqs, require_uniform=False, label='频率')
    return {
        'mode': key,
        'frequencies_hz': [float(v) for v in freqs],
        'start_hz': float(freqs[0]),
        'stop_hz': float(freqs[-1]),
        'points': int(freqs.size),
        'harmonic_numbers': harmonic_numbers,
        'uniform': bool(_is_uniform(freqs)),
        'warnings': warnings,
    }


def apply_frequency_plan(vna, plan: dict) -> None:
    """Apply a plan to a VNA-like object, instrument included.

    Uniform grids are driven through ``vna.set_sweep(start, stop, points)``:
    the bounds are the plan's first/last frequency and the point count equals
    the grid length, so a real VNA measures the requested band instead of
    staying on a stale 5 GHz single point.  A uniform list is reproduced
    exactly by that linear sweep.

    A NON-uniform grid (arbitrary/harmonic list with irregular spacing) must
    not be linearised: ``linspace(start, stop, n)`` would silently replace the
    operator's 1/2/4/8 GHz with 1/3.3/5.7/8 GHz and every stored sample would
    be labelled with the wrong frequency.  When the instrument driver exposes
    ``set_frequency_grid(freqs)`` it is used to install the exact list and the
    driver acquires each point with a single-point sweep.  Drivers without
    that capability (older callers, mocks) keep the documented linear
    approximation via ``set_sweep``.

    Objects with neither setter take the plan grid directly.  ``vna._sweep``
    is kept in sync when present so ``get_config()`` stays valid.
    """
    freqs = np.asarray(plan['frequencies_hz'], dtype=float).ravel()
    start = float(plan.get('start_hz', freqs[0] if freqs.size else 0.0))
    stop = float(plan.get('stop_hz', freqs[-1] if freqs.size else 0.0))
    points = int(plan.get('points', freqs.size if freqs.size else 1))
    uniform = bool(plan.get('uniform', _is_uniform(freqs)))
    grid_setter = getattr(vna, 'set_frequency_grid', None)
    if (not uniform and freqs.size > 1 and callable(grid_setter)):
        grid_setter(freqs)
    else:
        setter = getattr(vna, 'set_sweep', None)
        if callable(setter):
            setter(start, stop, points)
        else:
            vna.frequencies = freqs
    if hasattr(vna, '_sweep'):
        vna._sweep = {
            'start': float(freqs[0]),
            'stop': float(freqs[-1]),
            'points': int(freqs.size),
        }
    return None

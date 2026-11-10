"""P0 frequency plan: four frequency modes and VNA application.

Pure standard library + numpy; never imports UI or hardware modules.
``build_frequency_plan`` produces a validated frequency grid, and
``apply_frequency_plan`` pushes it onto a VNA-like object.

List modes (``harmonic_list`` / ``arbitrary_list``) may produce non-uniformly
spaced frequencies.  A standard SCPI linear sweep (START/STOP/POINTS) cannot
express an arbitrary list, so :func:`apply_frequency_plan` only maintains the
local frequency grid and the ``_sweep`` metadata for those modes; the
instrument side would need per-point frequency setting, which belongs to a
later stage.  It never sends a non-uniform SCPI sweep to an instrument.
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
    """Apply a plan to a VNA-like object.

    For ``fo_list`` / ``freq_sweep``: call ``vna.set_sweep(start, stop, points)``
    when available (real analyzers), otherwise only set ``vna.frequencies``
    (e.g. ``MockVNAController`` which has no ``set_sweep``).

    For ``harmonic_list`` / ``arbitrary_list``: set ``vna.frequencies`` directly
    and, when present, keep ``vna._sweep`` in sync (``{'start','stop','points'}``)
    so ``get_config()`` stays valid.  Limitation / 限制：列表模式的频率可能不等
    间隔，标准 SCPI 线性扫频无法表达，本函数只维护本地网格与元信息，**不向仪器
    下发不等间隔 SCPI**；仪器侧需要逐点设置频率，属后续阶段实现。
    """
    key = str(plan.get('mode', 'fo_list')).strip().lower()
    freqs = np.asarray(plan['frequencies_hz'], dtype=float).ravel()
    if key in ('fo_list', 'freq_sweep'):
        start = float(plan.get('start_hz', freqs[0] if freqs.size else 0.0))
        stop = float(plan.get('stop_hz', freqs[-1] if freqs.size else 0.0))
        points = int(plan.get('points', freqs.size if freqs.size else 1))
        setter = getattr(vna, 'set_sweep', None)
        if callable(setter):
            setter(start, stop, points)
        else:
            vna.frequencies = freqs
    else:
        vna.frequencies = freqs
        if hasattr(vna, '_sweep'):
            vna._sweep = {
                'start': float(freqs[0]),
                'stop': float(freqs[-1]),
                'points': int(freqs.size),
            }
    return None

"""Post-calibration library operations (P1-F14).

Standard practice keeps a calibrated library immutable and produces a *new* library for every
transformation: the source is never modified and the new library carries a fresh
Cal ID plus lineage back to the parent.  This module mirrors that discipline with
four operations, all of which ultimately call :func:`_derive` which writes a brand
new :class:`CalibrationDatabase` containing only the chosen points (so the geometry
and the S-matrices stay internally consistent).

Operations
----------
* ``truncate_by_gamma``  — keep only points whose |Γ| ≤ ``gmax`` (standard
  "drop the high-reflection tail" used when a region is ill-conditioned).
* ``truncate_by_travel`` — keep only points whose mechanical position stays inside
  per-axis travel bounds (the spec's "裁剪按机械行程上限").
* ``reverse_probe``      — mirror one axis about its mid-travel (a derived library
  for a flipped probe orientation); a coordinate transform, S untouched.
* ``split_by_frequency`` — one single-frequency library per frequency index.
* ``apply_fixture``      — embed or de-embed a 2-port fixture S2P across every point.

All functions return ``{'path', 'cal_id', 'points', 'parent_version'}``.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .database import CalibrationDatabase
from .models import Measurement, TunerConfig
from .rf import RFNetworkEngine as RF
from .metadata import normalise_metadata


def _derive(src_db, new_path, indices, reason, *, transform=None,
            fixture=None, direction=None, shift_matrix=None, z0=50.0,
            shift_direction='embed'):
    """Create a new library from a subset of the source's valid points.

    ``indices`` addresses the source point array.  ``transform`` (optional) maps a
    ``(m, 4)`` position array to a new one (applied to both ``positions`` and
    ``actual``).  ``fixture`` (optional 2x2 complex or ``(2,2)`` per-frequency
    array) cascades with every point's S when ``direction`` is set.
    ``shift_matrix`` (optional, P2) refers every point's S to a new reference
    plane with the given ABCD matrix; ``shift_direction`` says whether the
    network is put in (``embed``) or taken out (``deembed``).
    """
    data = src_db.load()
    meta = normalise_metadata(data['metadata'])
    config = TunerConfig(**meta['config'])
    frequencies = np.asarray(data['frequencies'], float)
    src_positions = np.asarray(data['positions'], float)
    src_actual = np.asarray(data.get('actual', data['positions']), float)
    src_s = np.asarray(data['s'], complex)
    valid = np.asarray(data['valid'], bool)
    indices = [int(i) for i in indices if 0 <= i < len(valid) and valid[i]]
    if not indices:
        raise ValueError('no valid source points selected')

    positions = src_positions[indices].copy()
    actual = src_actual[indices].copy()
    if transform is not None:
        positions = np.asarray(transform(positions), float)
        actual = np.asarray(transform(actual), float)

    target = CalibrationDatabase(new_path)
    exclude = {'version_id', 'created', 'schema_version', 'config', 'simulated', 'cal_id'}
    new_meta = {k: v for k, v in meta.items() if k not in exclude}
    new_meta.update(parent_version=meta.get('version_id'),
                    version_reason=str(reason),
                    operation='derive')

    target.create(config, frequencies, positions, new_meta)
    for k, src_idx in enumerate(indices):
        s_slice = src_s[src_idx]
        if fixture is not None and direction is not None:
            s_slice = _cascade_fixture(s_slice, fixture, frequencies, direction)
        if shift_matrix is not None:
            s_slice = _shift_reference(s_slice, shift_matrix, frequencies, z0,
                                       shift_direction)
        meas = Measurement(frequencies, s_slice, bool(meta.get('simulated', False)))
        target.write_point(k, actual[k], meas)
    for value in data.get('validation', {}).values():
        target.save_validation(value)
    return {
        'path': str(target.path),
        'cal_id': str(target.read_metadata().get('cal_id', '')),
        'points': len(indices),
        'parent_version': meta.get('version_id'),
    }


def _shift_reference(s, shift_matrix, frequencies, z0=50.0,
                     direction='deembed'):
    """Refer one point's S-parameters to the new reference plane.

    ``shift_matrix`` is the 2×2 S-matrix of the network between the DUT and
    the measurement plane — one matrix, or one per frequency.
    """
    from .reference_shift import shift_s_parameters
    s = np.asarray(s, complex)
    if s.ndim == 4:           # (1, f, 2, 2)
        s = s[0]
    matrix = np.asarray(shift_matrix, dtype=complex)
    out = np.empty_like(s)
    for j in range(s.shape[0]):
        fx = matrix
        if matrix.ndim == 3:
            fx = matrix[j]
        out[j] = shift_s_parameters(s[j], fx, direction=direction)
    return out


def _cascade_fixture(s, fixture, frequencies, direction):
    """Cascade a 2x2 fixture with point S at the library's frequency band."""
    s = np.asarray(s, complex)
    if s.ndim == 4:           # (1, f, 2, 2)
        s = s[0]
    f = np.asarray(frequencies, float)
    out = np.empty_like(s)
    for j in range(s.shape[0]):
        fx = fixture
        if getattr(fixture, 'ndim', 0) == 3:
            fx = fixture[j]
        if direction == 'embed':
            out[j] = RF.cascade(fx, s[j])
        elif direction == 'deembed':
            out[j] = RF.cascade_deembed(fx, s[j])
        else:
            out[j] = s[j]
    return out


def _gammas_of(src_db, frequency=None):
    """|Γ| of every valid source point at ``frequency`` (or band midpoint)."""
    from .search import GammaSearchEngine
    data = src_db.load()
    if frequency is None:
        frequency = float(np.asarray(data['frequencies'], float)[
            len(data['frequencies']) // 2])
    engine = GammaSearchEngine(data, z0=50.0)
    g = engine.gammas(frequency)
    valid = np.flatnonzero(np.asarray(data['valid'], bool))
    return valid, np.abs(g)


def truncate_by_gamma(src_db, new_path, gmax, reason='truncate by |Gamma|',
                      frequency=None) -> dict:
    """Keep valid points with |Γ| ≤ ``gmax``.  ``gmax`` must be finite (< 1)."""
    gmax = float(gmax)
    if not np.isfinite(gmax) or gmax <= 0 or gmax >= 1:
        raise ValueError('gmax must be in (0, 1)')
    valid, mags = _gammas_of(src_db, frequency)
    keep = [int(i) for i, m in zip(valid, mags) if m <= gmax]
    return _derive(src_db, new_path, keep, reason)


def truncate_by_travel(src_db, new_path, axis_bounds, reason='truncate by travel',
                       axis_names=('X', 'Z1', 'Z2', 'Z3')) -> dict:
    """Keep valid points whose mechanical position is inside ``axis_bounds``.

    ``axis_bounds`` maps an axis name (or integer index) to ``(lo, hi)``.  Any
    point leaving the declared travel on at least one axis is dropped.
    """
    data = src_db.load()
    valid = np.flatnonzero(np.asarray(data['valid'], bool))
    actual = np.asarray(data.get('actual', data['positions']), float)
    keep = []
    for i in valid:
        pos = actual[i]
        ok = True
        for key, (lo, hi) in axis_bounds.items():
            ax = key if isinstance(key, int) else axis_names.index(key)
            if not (float(lo) <= pos[ax] <= float(hi)):
                ok = False
                break
        if ok:
            keep.append(int(i))
    return _derive(src_db, new_path, keep, reason)


def reverse_probe(src_db, new_path, axis, reason='reverse probe',
                  axis_names=('X', 'Z1', 'Z2', 'Z3')) -> dict:
    """Mirror one axis about its mid-travel; a derived coordinate library.

    The S-parameters are unchanged (the RF response at the new mechanical pose is
    what a flipped-probe orientation would see).  Returns a new Cal ID.
    """
    ax = axis if isinstance(axis, int) else axis_names.index(axis)
    data = src_db.load()
    valid = np.flatnonzero(np.asarray(data['valid'], bool))
    positions = np.asarray(data['positions'], float)
    lo = float(positions[:, ax].min())
    hi = float(positions[:, ax].max())
    mid = 0.5 * (lo + hi)

    def transform(arr):
        arr = np.asarray(arr, float).copy()
        arr[:, ax] = 2 * mid - arr[:, ax]
        return arr

    return _derive(src_db, new_path, valid, reason, transform=transform)


def split_by_frequency(src_db, out_dir, reason='split by frequency') -> list[dict]:
    """One new single-frequency library per frequency index.

    Multi-frequency libraries only; for a single-frequency source a single
    derived library is returned.  Returns a list of per-frequency summaries.
    """
    src_db = CalibrationDatabase(src_db.path)
    data = src_db.load()
    freqs = np.asarray(data['frequencies'], float)
    valid = np.flatnonzero(np.asarray(data['valid'], bool))
    src_s = np.asarray(data['s'], complex)
    meta = normalise_metadata(data['metadata'])
    config = TunerConfig(**meta['config'])
    out = []
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    for j, f in enumerate(freqs):
        positions = np.asarray(data['positions'], float)
        actual = np.asarray(data.get('actual', data['positions']), float)
        target = CalibrationDatabase(d / f'freq_{j:03d}_{int(f):d}.h5')
        exclude = {'version_id', 'created', 'schema_version', 'config',
                   'simulated', 'cal_id'}
        new_meta = {k: v for k, v in meta.items() if k not in exclude}
        new_meta.update(parent_version=meta.get('version_id'),
                        version_reason=f'{reason} (f={f:.6f} Hz)',
                        operation='split_frequency',
                        frequency_list=[float(f)])
        target.create(config, np.array([f]), positions, new_meta)
        for k, src_idx in enumerate(valid):
            meas = Measurement(np.array([f]), src_s[src_idx, j:j + 1],
                               bool(meta.get('simulated', False)))
            target.write_point(k, actual[src_idx], meas)
        out.append({
            'path': str(target.path),
            'cal_id': str(target.read_metadata().get('cal_id', '')),
            'frequency_hz': float(f),
            'points': len(valid),
            'parent_version': meta.get('version_id'),
        })
    return out


def apply_fixture(src_db, new_path, fixture_s2p_path, direction='embed',
                  reason='apply fixture') -> dict:
    """Embed (cascade) or de-embed (reverse cascade) a 2-port fixture S2P.

    ``direction='embed'`` refers the measured tuner to the external fixture plane;
    ``direction='deembed'`` pushes the reference back through the fixture.  The
    fixture is read at the library frequencies (nearest sample, no interpolation
    between samples is performed — matching the de-embedding caveat).
    """
    from .reference_planes import read_s2p_frequencies
    if direction not in ('embed', 'deembed'):
        raise ValueError("direction must be 'embed' or 'deembed'")
    fixture = _read_fixture_2x2(fixture_s2p_path)
    data = src_db.load()
    frequencies = np.asarray(data['frequencies'], float)
    f_s2p = read_s2p_frequencies(fixture_s2p_path)
    fixture_band = _resample_fixture(fixture, f_s2p, frequencies, direction)
    valid = np.flatnonzero(np.asarray(data['valid'], bool))
    return _derive(src_db, new_path, valid, reason,
                   fixture=fixture_band, direction=direction)


def _read_fixture_2x2(path):
    """Read a 2x2 complex S-matrix from a Touchstone .s2p (first data row)."""
    p = Path(path)
    text = p.read_text(encoding='utf-8', errors='replace')
    ri = True
    scale = 1.0
    s11 = s21 = s12 = s22 = 0j
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('!'):
            continue
        if line.startswith('#'):
            u = line[1:].strip().upper()
            if u.startswith('GHZ'):
                scale = 1e9
            elif u.startswith('MHZ'):
                scale = 1e6
            elif u.startswith('KHZ'):
                scale = 1e3
            ri = not u.startswith('DB') and not u.startswith('MA')
            continue
        parts = line.split()
        try:
            nums = [float(x) for x in parts[1:]]
        except (ValueError, IndexError):
            continue
        if len(nums) >= 8:
            if ri:
                s11 = complex(nums[0], nums[1]); s21 = complex(nums[2], nums[3])
                s12 = complex(nums[4], nums[5]); s22 = complex(nums[6], nums[7])
            else:
                s11 = _ma_to_c(nums[0], nums[1]); s21 = _ma_to_c(nums[2], nums[3])
                s12 = _ma_to_c(nums[4], nums[5]); s22 = _ma_to_c(nums[6], nums[7])
            break
    return np.array([[s11, s12], [s21, s22]], dtype=complex)


def _ma_to_c(mag, ang_deg):
    from math import radians
    return mag * np.exp(1j * radians(ang_deg))


def _resample_fixture(fixture, f_s2p, frequencies, direction):
    """Build a per-frequency fixture array aligned to ``frequencies``."""
    f_s2p = np.asarray(f_s2p, float)
    if f_s2p.size <= 1:
        return np.tile(fixture, (len(frequencies), 1, 1))
    out = np.empty((len(frequencies), 2, 2), dtype=complex)
    for j, f in enumerate(frequencies):
        k = int(np.argmin(np.abs(f_s2p - f)))
        out[j] = fixture
    return out

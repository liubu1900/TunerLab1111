"""Calibration Browser backend: library discovery and A/B comparison (P1-F12).

A standard *Calibration Browser* lists every calibrated library, shows its 14-field
header and Smith point cloud, and can diff two libraries.  This module supplies
the pure helpers the desktop page and the web API reuse: scanning a directory for
``*.h5`` libraries and summarising a pair (point count, |Γ|max, frequency match,
mean |ΔΓ| when the grids coincide).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .database import CalibrationDatabase
from .metadata import normalise_metadata
from .rf import RFNetworkEngine as RF
from .search import GammaSearchEngine


def _library_summary(db: CalibrationDatabase) -> dict:
    """14-field header + coverage statistics for one library, no point cloud."""
    meta = normalise_metadata(db.read_metadata())
    data = db.load()
    valid = int(np.count_nonzero(np.asarray(data['valid'], bool)))
    total = len(np.asarray(data['valid'], bool))
    freqs = np.asarray(data['frequencies'], float)
    summary = {
        'path': str(db.path),
        'name': db.path.name,
        'cal_id': meta.get('cal_id', ''),
        'schema_version': int(meta.get('schema_version', 1)),
        'simulated': bool(meta.get('simulated', False)),
        'cal_type': meta.get('cal_type', ''),
        'reference_plane': meta.get('reference_plane', ''),
        'cal_method': meta.get('cal_method', ''),
        'density': meta.get('density', ''),
        'frequency_list_hz': [float(v) for v in freqs],
        'frequency_ghz': [float(v / 1e9) for v in freqs],
        'created': float(meta.get('created', 0.0)),
        'sha256': meta.get('sha256', ''),
        'points_valid': valid,
        'points_total': total,
        'layers': int(_count_layers(data)),
    }
    gmax = _gamma_max(data)
    summary['gamma_max'] = gmax
    summary['gamma_max_vswr'] = (None if gmax is None else (1 + gmax) / (1 - gmax))
    return summary


def _count_layers(data) -> int:
    """Approximate the number of equal-|Γ| rings from distinct |actual| radii."""
    try:
        actual = np.asarray(data.get('actual', data['positions']), float)
        valid = np.flatnonzero(np.asarray(data['valid'], bool))
        if not len(valid):
            return 0
        radii = np.linalg.norm(actual[valid][:, 1:], axis=1)  # Z1..Z3
        return int(len(np.unique(np.round(radii, 3))))
    except Exception:  # noqa: BLE001 - layout is purely cosmetic
        return 0


def _gamma_max(data) -> float | None:
    try:
        engine = GammaSearchEngine(data, z0=50.0)
        g = engine.gammas(float(np.asarray(data['frequencies'], float)[0]))
        m = np.abs(g)
        return float(m.max()) if len(m) else None
    except Exception:  # noqa: BLE001 - an empty / degenerate library has no max
        return None


def scan_libraries(directory, recursive: bool = False) -> list[dict]:
    """List every ``*.h5`` calibration library under ``directory``.

    Libraries that fail to open (not a calibration file, corrupt) are skipped and
    reported under ``errors`` so the browser can still show the healthy ones.
    """
    d = Path(directory)
    pattern = '**/*.h5' if recursive else '*.h5'
    errors = []
    summaries = []
    for path in sorted(d.glob(pattern)):
        try:
            summaries.append(_library_summary(CalibrationDatabase(path)))
        except Exception as exc:  # noqa: BLE001 - never let one bad file break the list
            errors.append({'path': str(path), 'error': f'{type(exc).__name__}: {exc}'})
    return {'libraries': summaries, 'errors': errors, 'directory': str(d)}


def compare_libraries(path_a, path_b, frequency_hz=None) -> dict:
    """Diff two calibration libraries.

    Reports whether their frequency grids match (a prerequisite for a point-wise
    |ΔΓ|), the point-count and |Γ|max deltas, and — when the grids and point sets
    coincide — the mean / max |ΔΓ| between the two libraries' gammas at the chosen
    (or first common) frequency.
    """
    a = CalibrationDatabase(path_a).load()
    b = CalibrationDatabase(path_b).load()
    fa = np.asarray(a['frequencies'], float)
    fb = np.asarray(b['frequencies'], float)
    freq_match = bool(np.array_equal(fa, fb))
    if frequency_hz is None:
        f = float(fa[len(fa) // 2]) if len(fa) else 0.0
    else:
        f = float(frequency_hz)
    result = {
        'path_a': str(path_a),
        'path_b': str(path_b),
        'frequency_hz': f,
        'freq_match': freq_match,
        'points_a': int(np.count_nonzero(a['valid'])),
        'points_b': int(np.count_nonzero(b['valid'])),
        'points_delta': int(np.count_nonzero(b['valid'])
                           - np.count_nonzero(a['valid'])),
        'gamma_max_a': _gamma_max(a),
        'gamma_max_b': _gamma_max(b),
    }
    if freq_match and a['positions'].shape == b['positions'].shape:
        try:
            ga = GammaSearchEngine(a, z0=50.0).gammas(f)
            gb = GammaSearchEngine(b, z0=50.0).gammas(f)
            diff = np.abs(ga - gb)
            result['mean_delta_gamma'] = float(diff.mean())
            result['max_delta_gamma'] = float(diff.max())
            result['diffable'] = True
        except Exception as exc:  # noqa: BLE001
            result['diffable'] = False
            result['diff_error'] = f'{type(exc).__name__}: {exc}'
    else:
        result['diffable'] = False
        result['diff_error'] = ('frequency grids or point sets differ'
                                if not freq_match else 'point sets differ')
    return result

"""Repeatability verification automation (P1-F20).

A standard *Repeatability Verification* menu drives the tuner to the same target N
times and reports how tightly the returned reflection coefficient clusters.  This
module is the pure analysis behind that flow: feed it the list of measured
results and it returns the mean / σ / worst-case |ΔΓ|, a repeatability figure of
merit in dB, and a separate tally for the high-|Γ| band (where interpolation and
mechanical hysteresis are worst — the historical A→B→A test showed the return leg
off by 0.034 while a same-position re-measure was within 7e-5).

The function is deliberately side-effect free: the caller decides whether the
result is written to the library (via ``save_validation``) or only shown in the UI.

:func:`auto_repeatability_measurements` is the one function here that *does*
move: it drives the A→B→A cycle and hands the measurements back to the pure
analysis.  It lives here so the desktop page and the web API share exactly the
same cycle, the same labels and the same hysteresis definition.
"""
from __future__ import annotations

import numpy as np

from .rf import RFNetworkEngine as RF

__all__ = ['analyze_repeatability', 'auto_repeatability_measurements',
           'build_repeatability_report']
def _as_gamma(record: dict) -> complex:
    """The reflection coefficient of one measurement record.

    Two shapes are accepted, because both are in use: the internal one
    (``gamma`` as a complex number) and the JSON one the web API receives
    (``gamma_re`` / ``gamma_im``).  Keeping both here means a caller does not
    have to know which consumer it is feeding.
    """
    if 'gamma' in record:
        return complex(record['gamma'])
    if 'gamma_re' in record and 'gamma_im' in record:
        return complex(float(record['gamma_re']), float(record['gamma_im']))
    raise ValueError('measurement needs either "gamma" or "gamma_re"/"gamma_im"')


def analyze_repeatability(measurements, *, z0=50.0, tolerance=0.05,
                          target_gamma=None, high_gamma_threshold=0.9) -> dict:
    """Analyse N repeat measurements of (nominally) the same target.

    ``measurements`` is a sequence of dicts, each carrying the reflection
    coefficient — either as ``gamma`` (a complex number) or as the
    ``gamma_re`` / ``gamma_im`` pair the web API sends.  Optional keys
    ``position`` (mechanical pose), ``target_gamma`` (the commanded target) and
    ``label`` / ``point_index`` are carried through into the per-row table.

    Returns a JSON-serialisable report:
    ``n``, ``mean_delta_gamma``, ``std_delta_gamma``, ``max_delta_gamma``,
    ``reset_dB`` (``-20*log10(max |ΔΓ|)``), ``within_tolerance`` count,
    ``high_gamma`` sub-statistics, and ``rows``.
    """
    if measurements is None:
        raise ValueError('measurements are required')
    rows = []
    for idx, m in enumerate(measurements):
        gamma = _as_gamma(m)
        target = (complex(m['target_gamma']) if m.get('target_gamma') is not None
                  else (complex(target_gamma) if target_gamma is not None else gamma))
        delta = abs(gamma - target)
        mag = abs(gamma)
        rows.append({
            'index': int(m.get('index', idx)),
            'label': str(m.get('label', f'#{idx}')),
            'point_index': (int(m['point_index']) if m.get('point_index') is not None else None),
            'gamma_real': gamma.real,
            'gamma_imag': gamma.imag,
            'gamma_mag': float(mag),
            'target_real': target.real,
            'target_imag': target.imag,
            'delta_gamma': float(delta),
            'position': ([float(v) for v in m['position']]
                         if m.get('position') is not None else None),
            'high_gamma': bool(mag >= high_gamma_threshold),
        })
    if not rows:
        raise ValueError('no measurements supplied')
    deltas = np.array([r['delta_gamma'] for r in rows], float)
    n = len(rows)
    mean = float(deltas.mean())
    std = float(deltas.std(ddof=0)) if n > 1 else 0.0
    maxd = float(deltas.max())
    worst = rows[int(np.argmax(deltas))]
    reset_db = float(-20.0 * np.log10(maxd)) if maxd > 0 else float('inf')
    within = int(np.count_nonzero(deltas <= float(tolerance)))

    hi = [r for r in rows if r['high_gamma']]
    hi_stats = None
    if hi:
        hd = np.array([r['delta_gamma'] for r in hi], float)
        hi_stats = {
            'n': len(hi),
            'mean_delta_gamma': float(hd.mean()),
            'max_delta_gamma': float(hd.max()),
            'reset_dB': (float(-20.0 * np.log10(hd.max()))
                         if hd.max() > 0 else float('inf')),
        }

    return {
        'n': n,
        'z0': float(z0),
        'tolerance': float(tolerance),
        'mean_delta_gamma': mean,
        'std_delta_gamma': std,
        'max_delta_gamma': maxd,
        'reset_dB': reset_db,
        'worst': worst,
        'within_tolerance': within,
        'all_within_tolerance': bool(within == n),
        'high_gamma_threshold': float(high_gamma_threshold),
        'high_gamma': hi_stats,
        'rows': rows,
    }


def build_repeatability_report(measurements, *, z0=50.0, tolerance=0.05,
                               target_gamma=None, frequency_hz=None,
                               high_gamma_threshold=0.9) -> dict:
    """Wrap :func:`analyze_repeatability` into a saveable validation report."""
    stats = analyze_repeatability(
        measurements, z0=z0, tolerance=tolerance,
        target_gamma=target_gamma, high_gamma_threshold=high_gamma_threshold)
    report = {
        'kind': 'repeatability_verification',
        'frequency_hz': (float(frequency_hz) if frequency_hz is not None else None),
        'z0': float(z0),
        'tolerance': float(tolerance),
        'n': stats['n'],
        'mean_delta_gamma': stats['mean_delta_gamma'],
        'std_delta_gamma': stats['std_delta_gamma'],
        'max_delta_gamma': stats['max_delta_gamma'],
        'reset_dB': stats['reset_dB'],
        'all_within_tolerance': stats['all_within_tolerance'],
        'high_gamma': stats['high_gamma'],
        'rows': stats['rows'],
    }
    return report


def auto_repeatability_measurements(motion, vna, position_a, position_b, *,
                                    repeats: int = 3, frequency_hz=None,
                                    target_gamma=None) -> dict:
    """Drive the A→B→A cycle and collect the reflection coefficients.

    One *cycle* is three legs: **A** (the reference position), **B** (a second
    position, so the machine has to travel) and **A** again (the return leg).
    The return leg is the interesting one — it measures how much of the
    position error the machine *keeps* after going away and coming back, which
    is the hysteresis the historical A→B→A test was designed to expose.

    The function moves the machine.  It therefore expects a motion object that
    enforces the safety rules itself (homed axes, declared travel box,
    encoder-feedback check); the caller is responsible for asking the operator
    first.

    Returns a dictionary with:

    ``measurements``
        A list of ``{'gamma', 'position', 'label', 'leg'}`` records, ready for
        :func:`analyze_repeatability`.
    ``hysteresis``
        The A→B→A number: for every cycle, |Γ(return) − Γ(first A)|.
    ``reference_gamma``
        The Γ of the very first A measurement — the point every other
        measurement is compared against.
    """
    if repeats < 1:
        raise ValueError('repeats must be >= 1')
    a = np.asarray(position_a, float)
    b = np.asarray(position_b, float)
    if a.shape != b.shape:
        raise ValueError('positions A and B must have the same shape')

    measurements: list[dict] = []
    reference_gamma = None
    hysteresis: list[dict] = []

    for cycle in range(1, int(repeats) + 1):
        # A (reference) -> B (travel) -> A (return)
        for leg, position in (('A', a), ('B', b), ('A-return', a)):
            motion.move_to(position)
            measurement = vna.measure()
            frequencies = np.asarray(measurement.frequencies, float)
            gammas = RF.dut_gamma(np.asarray(measurement.s, complex))
            if frequency_hz is None:
                frequency = float(frequencies[len(frequencies) // 2])
            else:
                frequency = float(frequency_hz)
                if not (frequencies[0] <= frequency <= frequencies[-1]):
                    raise ValueError(
                        f'target frequency {frequency / 1e9:.6f} GHz is outside '
                        f'the instrument grid '
                        f'{frequencies[0] / 1e9:.6f}–{frequencies[-1] / 1e9:.6f} GHz')
            gamma = complex(
                float(np.interp(frequency, frequencies, gammas.real)),
                float(np.interp(frequency, frequencies, gammas.imag)))
            if leg == 'A' and reference_gamma is None:
                reference_gamma = gamma
            label = (f'A#{cycle}' if leg == 'A'
                     else f'B#{cycle}' if leg == 'B'
                     else f'A#{cycle}-返回')
            measurements.append({
                'gamma': gamma,
                'position': [float(v) for v in position],
                'label': label,
                'leg': leg,
                'cycle': cycle,
                'target_gamma': target_gamma,
            })
        # The return leg of this cycle against the very first A measurement.
        returned = measurements[-1]['gamma']
        hysteresis.append({
            'cycle': cycle,
            'label': measurements[-1]['label'],
            'delta_gamma': float(abs(complex(returned) - complex(reference_gamma))),
        })

    return {
        'measurements': measurements,
        'hysteresis': hysteresis,
        'reference_gamma': reference_gamma,
        'repeats': int(repeats),
        'frequency_hz': (float(frequency_hz) if frequency_hz is not None else None),
    }

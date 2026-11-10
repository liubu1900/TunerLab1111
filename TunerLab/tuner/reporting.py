"""Predicted-vs-measured deviation report and Smith-chart formatting (P1-F19/F24).

The *target–measured comparison card* (F-19) needs more than a single |ΔΓ|: it
wants the predicted and measured Γ / Z / VSWR / return-loss laid out side by side,
the VSWR deviation, and a classification of how trustworthy the result is at the
reach |Γ| band (Standard practice notes the high-|Γ| region carries the largest interpolation
and mechanical error).  :func:`deviation_report` is the pure builder used by both
the web handler and the desktop card.

:func:`gamma_band`, :func:`format_gamma`, :func:`format_complex` and
:func:`vswr_from_gamma` are the shared formatting helpers (F-24) so the desktop and
the web console never disagree about how a number is shown.
"""
from __future__ import annotations

import numpy as np

from .rf import RFNetworkEngine as RF


def gamma_band(magnitude: float) -> str:
    """Classify a |Γ| into standard reachability bands."""
    mag = float(magnitude)
    if mag < 0.5:
        return 'low'
    if mag < 0.9:
        return 'mid'
    return 'high'


def vswr_from_gamma(gamma) -> float | None:
    """VSWR from a reflection coefficient; ``None`` at |Γ|=1 (infinite)."""
    mag = abs(complex(gamma))
    if mag >= 1.0 - 1e-12:
        return None
    return (1.0 + mag) / (1.0 - mag)


def format_gamma(gamma) -> str:
    """``+0.754∠+42.5°`` style label for a reflection coefficient."""
    g = complex(gamma)
    mag = abs(g)
    ang = np.degrees(np.angle(g))
    return f'{mag:+.4f}∠{ang:+.1f}°'


def format_complex(z) -> str:
    """``6.8 + j31.0`` impedance label."""
    z = complex(z)
    sign = '+' if z.imag >= 0 else '-'
    return f'{z.real:.3f} {sign} j{abs(z.imag):.3f}'


def _metrics(gamma, z0):
    g = complex(gamma)
    mag = abs(g)
    phase = float(np.degrees(np.angle(g)))
    vswr = vswr_from_gamma(g)
    rl = (0.0 if mag == 0 else float(-20.0 * np.log10(mag))) if mag < 1 else 0.0
    z = complex(RF.gamma_to_z(g, z0))
    return {
        'gamma_mag': float(mag),
        'gamma_phase_deg': float(phase),
        'gamma_label': format_gamma(g),
        'vswr': (None if vswr is None else float(vswr)),
        'return_loss_db': rl,
        'z_real': float(z.real),
        'z_imag': float(z.imag),
        'z_label': format_complex(z),
        'r_norm': float(z.real / z0),
        'x_norm': float(z.imag / z0),
    }


def deviation_report(predicted_gamma, measured_gamma, z0=50.0,
                     tolerance=0.08) -> dict:
    """Side-by-side predicted vs measured with VSWR deviation and band grading."""
    predicted = _metrics(predicted_gamma, z0)
    measured = _metrics(measured_gamma, z0)
    delta_r = measured['z_real'] - predicted['z_real']
    delta_x = measured['z_imag'] - predicted['z_imag']
    vector_error = float(abs(complex(measured_gamma) - complex(predicted_gamma)))
    band = gamma_band(measured['gamma_mag'])
    # Standard practice recommends relaxing the cut at high |Γ|; surface the suggestion but do
    # not silently change the operator's tolerance.
    suggested_tolerance = float(tolerance)
    if band == 'high':
        suggested_tolerance = max(float(tolerance), 0.08)
    report = {
        'z0': float(z0),
        'tolerance': float(tolerance),
        'suggested_tolerance': suggested_tolerance,
        'gamma_band': band,
        'vector_error': vector_error,
        'within_tolerance': bool(vector_error <= float(tolerance)),
        'impedance_delta_r': float(delta_r),
        'impedance_delta_x': float(delta_x),
        'impedance_delta_r_percent': (
            100.0 * delta_r / predicted['z_real']
            if abs(predicted['z_real']) > 1e-9 else None),
        'impedance_delta_x_percent': (
            100.0 * delta_x / predicted['z_imag']
            if abs(predicted['z_imag']) > 1e-9 else None),
        'vswr_delta': (
            measured['vswr'] - predicted['vswr']
            if measured['vswr'] is not None and predicted['vswr'] is not None
            else None),
        'predicted': predicted,
        'measured': measured,
    }
    return report

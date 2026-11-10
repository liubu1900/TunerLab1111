"""One Smith-chart vocabulary for the whole desktop app (F-24).

Before this module every page formatted a reflection coefficient, an
impedance and a VSWR slightly differently: the Smith page used four
decimals for Γ and ``Z = a + bj`` for the impedance, the tuning page used a
signed ``+0.1234+0.5678j`` inline form, and the validation report used raw
floats.  An operator comparing a card from one page with a card from another
had to re-read the number format before trusting a comparison.

Everything now goes through the helpers below, which are thin wrappers over
:mod:`tuner.reporting` — the module that already owns the math — plus the
presentation rules that only the UI cares about:

* Γ is always shown as magnitude and phase (``+0.754∠+42.5°``) with the
  rectangular form available next to it.
* Impedance always carries its unit and its sign explicitly
  (``6.800 + j31.000 Ω``); the ``Z₀`` reference is stated once per card.
* VSWR is dimensionless and shown with two decimals; an infinite VSWR (|Γ|=1)
  is rendered as ``∞`` instead of a bare ``inf``.
* Return loss is in dB and never negative infinity at |Γ|=0.
* The reachability band uses a common vocabulary (``low`` / ``mid`` /
  ``high``) and always ships a Chinese label so the operator never has to
  remember the English key.
"""
from __future__ import annotations

import math

from tuner.reporting import (
    format_complex,
    format_gamma,
    gamma_band,
    vswr_from_gamma,
)

__all__ = [
    'BAND_LABELS',
    'SmithSpec',
    'band_label',
    'fmt_gamma',
    'fmt_gamma_rect',
    'fmt_impedance',
    'fmt_return_loss',
    'fmt_vswr',
    'gamma_to_z',
    'normalized_coordinates',
]


#: standard reachability bands, in the operator's language.
BAND_LABELS = {
    'low': '低 |Γ| 区（阻抗接近 Z₀，易达标）',
    'mid': '中 |Γ| 区（常规可达）',
    'high': '高 |Γ| 区（容差建议放宽到 0.08）',
}


def band_label(band: str) -> str:
    """Chinese label for a standard reachability band key."""
    return BAND_LABELS.get(str(band), str(band))


def gamma_to_z(gamma, z0: float = 50.0) -> complex:
    """Impedance from a reflection coefficient (single import point)."""
    from tuner.rf import gamma_to_z as _gamma_to_z
    return complex(_gamma_to_z(complex(gamma), float(z0)))


def normalized_coordinates(z, z0: float = 50.0) -> tuple[float, float]:
    """``(r, x)`` — the Smith-chart coordinates of an impedance."""
    value = complex(z)
    return (value.real / float(z0), value.imag / float(z0))


def fmt_gamma(gamma) -> str:
    """``+0.754∠+42.5°`` — the canonical reflection-coefficient label."""
    return format_gamma(complex(gamma))


def fmt_gamma_rect(gamma) -> str:
    """``+0.5000+0.2000j`` — the rectangular form, for exact reading."""
    g = complex(gamma)
    sign_im = '+' if g.imag >= 0 else '-'
    return f'{g.real:+.4f}{sign_im}{abs(g.imag):.4f}j'


def fmt_impedance(z, unit: str = 'Ω') -> str:
    """``6.800 + j31.000 Ω`` — the canonical impedance label."""
    return format_complex(complex(z)) + f' {unit}'


def fmt_vswr(vswr) -> str:
    """VSWR with two decimals; ``∞`` when the reflection is total."""
    if vswr is None:
        return '—'
    value = float(vswr)
    if math.isinf(value) or math.isnan(value):
        return '∞'
    return f'{value:.2f}'


def fmt_return_loss(db) -> str:
    """Return loss in dB; ``∞`` at a perfect match."""
    if db is None:
        return '—'
    value = float(db)
    if math.isinf(value) or math.isnan(value):
        return '∞'
    if value > 999.0:
        return '∞'
    return f'{value:.2f} dB'


class SmithSpec:
    """A per-page bundle that formats every Smith quantity the same way.

    The page keeps one :class:`SmithSpec` and calls it for every number it
    prints, so a card on the Smith page and a card on the tuning page are
    formatted by the same code.
    """

    def __init__(self, z0: float = 50.0, tolerance: float = 0.08):
        self.z0 = float(z0)
        self.tolerance = float(tolerance)

    # -- single quantities ------------------------------------------------
    def gamma(self, gamma) -> str:
        return fmt_gamma(gamma)

    def gamma_rect(self, gamma) -> str:
        return fmt_gamma_rect(gamma)

    def impedance(self, z) -> str:
        return fmt_impedance(z)

    def vswr(self, gamma) -> str:
        """VSWR of a Γ; ``∞`` at total reflection.

        ``tuner.reporting.vswr_from_gamma`` returns ``None`` at |Γ|=1 because
        the value is mathematically infinite, but the operator needs to see
        *that* it is infinite rather than "no value" — so the limit is computed
        here and rendered as ``∞``.
        """
        if gamma is None:
            return '—'
        magnitude = abs(complex(gamma))
        if magnitude >= 1.0 - 1e-12:
            return '∞'
        return fmt_vswr(vswr_from_gamma(complex(gamma)))

    def return_loss(self, gamma) -> str:
        if gamma is None:
            return '—'
        mag = abs(complex(gamma))
        if mag <= 0.0:
            return fmt_return_loss(float('inf'))
        if mag >= 1.0:
            return fmt_return_loss(0.0)
        return fmt_return_loss(-20.0 * math.log10(mag))

    def band(self, gamma) -> str:
        if gamma is None:
            return '—'
        return band_label(gamma_band(abs(complex(gamma))))

    def band_key(self, gamma) -> str:
        if gamma is None:
            return '—'
        return gamma_band(abs(complex(gamma)))

    # -- cards -------------------------------------------------------------
    def metrics(self, gamma) -> dict:
        """Every derived quantity for one Γ, formatted and ready to display."""
        g = complex(gamma)
        z = gamma_to_z(g, self.z0)
        r, x = normalized_coordinates(z, self.z0)
        return {
            'gamma_label': self.gamma(g),
            'gamma_rect': self.gamma_rect(g),
            'gamma_mag': abs(g),
            'gamma_phase_deg': math.degrees(math.atan2(g.imag, g.real)),
            'z_label': self.impedance(z),
            'z_real': z.real,
            'z_imag': z.imag,
            'z0': self.z0,
            'r_norm': r,
            'x_norm': x,
            'smith_coords': f'r={r:.3f}, x={x:.3f}',
            'vswr': self.vswr(g),
            'return_loss': self.return_loss(g),
            'band_key': self.band_key(g),
            'band_label': self.band(g),
        }

    def comparison_card(self, predicted, measured) -> dict:
        """Target-vs-measured card (F-19) rendered in the canonical units."""
        from tuner.reporting import deviation_report
        report = deviation_report(complex(predicted), complex(measured),
                                   z0=self.z0, tolerance=self.tolerance)
        report['gamma_band'] = self.band_key(measured)
        report['gamma_band_label'] = band_label(report['gamma_band'])
        report['predicted_card'] = self.metrics(predicted)
        report['measured_card'] = self.metrics(measured)
        report['vswr_delta_text'] = (
            '—' if report.get('vswr_delta') is None
            else f"{report['vswr_delta']:+.3f}")
        report['vector_error_text'] = f"{report['vector_error']:.6f}"
        report['within_text'] = '达标' if report['within_tolerance'] else '超差'
        return report

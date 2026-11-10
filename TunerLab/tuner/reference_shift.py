"""Reference-plane shifting with a T-matrix (P2).

A calibration library records the network it measured *at the reference plane it
was taken at*.  Moving the reference plane — from the probe tip to the wafer,
from the connector to the waveguide flange — is the classical load-pull
operation, and it is expressed with the **T-matrix** of the line or network
that lies between the two planes.

What is actually needed
-----------------------
The library stores a full 2×2 S-matrix per point, and the reflection the
operator cares about is the DUT's ``Γ``.  Shifting the plane therefore means
one of two things:

``deembed``
    The network (a probe tip, a fixture, a line) lies between the DUT and the
    measurement plane and is already part of what was measured.  Taking it out
    refers the library to the DUT's own plane.

``embed``
    The network is not yet part of the measurement and has to be put in — the
    library is referred to the plane on the far side of it.

Both are expressed as an **S-parameter cascade** with the network's own
2×2 S-matrix, which is the algebra the rest of the project already proves
(:meth:`tuner.rf.RFNetworkEngine.cascade` and ``cascade_deembed``).  No
ABCD↔S conversion is needed, and therefore no sign-convention trap.

Two ways to describe the network:

* **a line** — length, characteristic impedance, velocity factor and loss.
  A matched line of length *l* has ``S21 = e^(−γl)`` and ``S11 = S22 = 0``,
  so the DUT's Γ is rotated by ``e^(−2jβl)`` — the familiar round-trip phase.
* **a fixture** — an existing ``.s2p``, read directly.  Its samples are
  **not** interpolated: a library frequency without a fixture sample is an
  abort, the same rule the de-embedding gate uses (F-10).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .rf import RFNetworkEngine as RF

__all__ = [
    'abcd_from_line',
    'line_s_parameters',
    'read_s2p',
    'reference_shift_report',
    'shift_gamma',
    'shift_s_parameters',
]


#: Speed of light in vacuum (m/s).
C0 = 299_792_458.0


def abcd_from_line(length_m, frequency_hz, z0=50.0, velocity_factor=1.0,
                   alpha_np_per_m=0.0):
    """The ABCD matrix of a matched transmission line.

    Kept for reference and for the one-port preview: the ABCD matrix maps the
    *device plane → the measurement plane*, so ``Z_in = (A·Z_L + B)/(C·Z_L + D)``
    is the impedance the instrument sees through the line.

    ``length_m`` is the one-way length between the device and the measurement
    plane.  ``velocity_factor`` is the phase velocity relative to the speed of
    light (0.66 for a typical coax).  ``alpha_np_per_m`` is the attenuation in
    nepers per metre; 0.0 is lossless.
    """
    frequency = np.atleast_1d(np.asarray(frequency_hz, float))
    length = float(length_m)
    if length < 0:
        raise ValueError('length must not be negative')
    if not 0.0 < float(velocity_factor) <= 1.0:
        raise ValueError('velocity_factor must be in (0, 1]')
    z0 = float(z0)
    if z0 <= 0:
        raise ValueError('z0 must be positive')

    beta = 2.0 * np.pi * frequency / (C0 * float(velocity_factor))
    gamma = float(alpha_np_per_m) + 1j * beta
    exponent = gamma * length
    cosh = np.cosh(exponent)
    sinh = np.sinh(exponent)
    matrix = np.zeros((len(frequency), 2, 2), dtype=complex)
    matrix[:, 0, 0] = cosh
    matrix[:, 0, 1] = z0 * sinh
    matrix[:, 1, 0] = sinh / z0
    matrix[:, 1, 1] = cosh
    return matrix


def line_s_parameters(length_m, frequency_hz, z0=50.0, velocity_factor=1.0,
                      alpha_np_per_m=0.0):
    """The 2×2 S-matrix of a matched transmission line.

    ``S11 = S22 = 0`` and ``S21 = S12 = e^(−γl)`` for a line whose
    characteristic impedance equals the system impedance.
    """
    frequency = np.atleast_1d(np.asarray(frequency_hz, float))
    length = float(length_m)
    if length < 0:
        raise ValueError('length must not be negative')
    if not 0.0 < float(velocity_factor) <= 1.0:
        raise ValueError('velocity_factor must be in (0, 1]')
    z0 = float(z0)
    if z0 <= 0:
        raise ValueError('z0 must be positive')

    beta = 2.0 * np.pi * frequency / (C0 * float(velocity_factor))
    gamma = float(alpha_np_per_m) + 1j * beta
    transmission = np.exp(-gamma * length)
    matrix = np.zeros((len(frequency), 2, 2), dtype=complex)
    matrix[:, 0, 1] = transmission
    matrix[:, 1, 0] = transmission
    return matrix


# ----------------------------------------------------------------------
# Touchstone
# ----------------------------------------------------------------------
def read_s2p(path):
    """Read a Touchstone ``.s2p`` into ``(frequencies, s_matrices)``.

    Only the formats this project writes are supported — RI or MA, two ports,
    one frequency per line.  The unit line decides the frequency scale.
    """
    p = Path(path)
    text = p.read_text(encoding='utf-8', errors='replace')
    unit_scale = 1.0
    is_ri = True
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('!'):
            continue
        if stripped.startswith('#'):
            option = stripped[1:].strip().upper()
            if option.startswith('GHZ'):
                unit_scale = 1e9
            elif option.startswith('MHZ'):
                unit_scale = 1e6
            elif option.startswith('KHZ'):
                unit_scale = 1e3
            is_ri = not option.startswith('DB') and not option.startswith('MA')
            continue
        parts = stripped.split()
        try:
            numbers = [float(value) for value in parts[1:]]
        except (ValueError, IndexError):
            continue
        if len(numbers) < 8:
            continue
        if is_ri:
            s11 = complex(numbers[0], numbers[1])
            s21 = complex(numbers[2], numbers[3])
            s12 = complex(numbers[4], numbers[5])
            s22 = complex(numbers[6], numbers[7])
        else:
            s11 = _ma_to_complex(numbers[0], numbers[1])
            s21 = _ma_to_complex(numbers[2], numbers[3])
            s12 = _ma_to_complex(numbers[4], numbers[5])
            s22 = _ma_to_complex(numbers[6], numbers[7])
        rows.append((parts[0], np.array([[s11, s12], [s21, s22]], complex)))
    if not rows:
        raise ValueError(f'no usable two-port data in {p}')
    frequencies = np.array([float(row[0]) * unit_scale for row in rows], float)
    matrices = np.stack([row[1] for row in rows])
    order = np.argsort(frequencies)
    return frequencies[order], matrices[order]


def _ma_to_complex(magnitude, angle_deg):
    radians = np.deg2rad(angle_deg)
    return complex(magnitude * np.cos(radians), magnitude * np.sin(radians))


def s2p_at_frequencies(path, frequencies, tol_hz=1.0):
    """The fixture's S-matrix at each library frequency.

    A library frequency without a fixture sample within ``tol_hz`` is an
    **abort**: the fixture is not interpolated between its samples, because an
    interpolated de-embedding network silently corrupts every point that
    depends on it (the usual rule, also enforced by the F-10 gate).
    """
    fixture_frequencies, matrices = read_s2p(path)
    wanted = np.atleast_1d(np.asarray(frequencies, float))
    result = np.zeros((len(wanted), 2, 2), dtype=complex)
    missing = []
    for index, frequency in enumerate(wanted):
        distance = np.abs(fixture_frequencies - frequency)
        nearest = int(np.argmin(distance))
        if distance[nearest] > tol_hz:
            missing.append(float(frequency))
            continue
        result[index] = matrices[nearest]
    if missing:
        raise ValueError(
            'S2P is not interpolated between frequencies; missing '
            f'{len(missing)} library frequency(ies): '
            + ', '.join(f'{value / 1e9:.6f} GHz' for value in missing[:8]))
    return result


# ----------------------------------------------------------------------
# The shift itself
# ----------------------------------------------------------------------
def shift_s_parameters(s, network_s, direction='deembed'):
    """Move a 2×2 S-matrix between the two planes.

    ``network_s`` is the 2×2 S-matrix of the network between the DUT and the
    measurement plane (one matrix, or one per frequency).

    ``direction='deembed'``
        Take the network out: the input is what was measured, the output is
        the DUT referred to its own plane.
    ``direction='embed'``
        Put the network in: the input is the DUT's own S-parameters, the
        output is what the instrument would see through the network.
    """
    matrix = np.asarray(s, dtype=complex)
    network = np.asarray(network_s, dtype=complex)
    if direction == 'deembed':
        if network.ndim == 3:
            out = np.empty_like(matrix)
            for index in range(matrix.shape[0]):
                out[index] = RF.cascade_deembed(network[index], matrix[index])
            return out
        return RF.cascade_deembed(network, matrix)
    if direction != 'embed':
        raise ValueError("direction must be 'embed' or 'deembed'")
    if network.ndim == 3:
        out = np.empty_like(matrix)
        for index in range(matrix.shape[0]):
            out[index] = RF.cascade(network[index], matrix[index])
        return out
    return RF.cascade(network, matrix)


def shift_gamma(gamma, network_s, z0=50.0, direction='deembed'):
    """Move a reflection coefficient between the two planes.

    The one-port preview of the same operation: the load is represented by its
    Γ, the network is cascaded with it and the resulting reflection is the Γ
    at the other plane.
    """
    g = np.asarray(gamma, dtype=complex)
    network = np.asarray(network_s, dtype=complex)
    load = np.zeros(g.shape + (2, 2), dtype=complex)
    load[..., 0, 0] = g
    if direction == 'deembed':
        shifted = shift_s_parameters(load, network, direction='deembed')
    elif direction == 'embed':
        shifted = shift_s_parameters(load, network, direction='embed')
    else:
        raise ValueError("direction must be 'embed' or 'deembed'")
    return shifted[..., 0, 0]


def reference_shift_report(src_db, new_path, network_s, reason='reference shift',
                           reference_plane='', z0=50.0, direction='deembed'):
    """Write a shifted copy of a calibration library.

    The source library is never touched: the new one carries the reference
    plane it is now referred to and the lineage of its parent, the same
    discipline the other derivations follow (F-14).
    """
    from .library_ops import _derive
    data = src_db.load()
    frequencies = np.asarray(data['frequencies'], float)
    matrix = np.asarray(network_s, dtype=complex)
    if matrix.ndim == 2:
        matrix = np.repeat(matrix[None, :, :], len(frequencies), axis=0)
    if len(matrix) != len(frequencies):
        raise ValueError('the shift matrix must cover every library frequency')

    valid = np.flatnonzero(np.asarray(data['valid'], bool))
    shifted = _derive(src_db, new_path, valid, reason,
                      shift_matrix=matrix, z0=float(z0),
                      shift_direction=direction)
    if reference_plane:
        # The header has to say which plane the numbers are now referred to —
        # otherwise the next reader takes them for the parent's plane.
        from .database import CalibrationDatabase
        CalibrationDatabase(new_path).update_metadata(
            reference_plane=str(reference_plane))
        shifted['reference_plane'] = str(reference_plane)
    return shifted

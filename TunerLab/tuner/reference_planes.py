"""Reference planes and S2P de-embedding coverage validation (P1-F10).

Standard practice distinguishes six calibration reference planes.  Each plane
defines *where* the measured S-parameters are referred to, and whether a
de-embedding S2P fixture can be applied.  The tuner library header already
carries ``reference_plane`` and ``deembed_s2p`` schema-v2 fields; this module
supplies the controlled vocabulary and the frequency-coverage gate that the
premise checklist (F-03) reuses.

Key rule (standard §7 / de-embedding caveat): an S2P fixture is **not** interpolated
between samples.  If a calibration frequency has no fixture sample within
tolerance the de-embedding is mathematically undefined and the run must abort —
exactly like the historical ``S2P data is not interpolated between frequencies``
error.  See :func:`check_s2p_coverage`.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

#: The six standard calibration reference planes.  ``deembed`` marks whether a
#: two-port S2P fixture may legally be attached at that plane (a 2x2 network
#: refers the measured tuner response to the plane).
REFERENCE_PLANES = (
    {
        'id': 'standard',
        'name': 'Standard (Standalone)',
        'deembed': False,
        'description': 'Tuner characterized as a standalone two-port; no fixture '
                       'network between DUT and tuner.',
    },
    {
        'id': 'on_wafer',
        'name': 'On-Wafer',
        'deembed': True,
        'description': 'Probes on a wafer; probe-tip de-embedding S2P may be '
                       'applied to refer to the device under test.',
    },
    {
        'id': 'pc_board',
        'name': 'PC-board',
        'deembed': True,
        'description': 'Fixture printed circuit board between DUT and tuner; '
                       'board S2P de-embedding supported.',
    },
    {
        'id': 'connectorized',
        'name': 'Connectorized',
        'deembed': True,
        'description': 'Coaxial connectors / launchers; connector S2P may be '
                       'de-embedded to the connector reference plane.',
    },
    {
        'id': 'waveguide',
        'name': 'Waveguide',
        'deembed': True,
        'description': 'Waveguide transition; transition S2P may be de-embedded '
                       'to the waveguide flange.',
    },
    {
        'id': 'in_situ',
        'name': 'In-Situ',
        'deembed': True,
        'description': 'Tuner embedded in the actual measurement path (e.g. an '
                       'amplifier); in-situ de-embedding supported but requires '
                       'the full path fixture to avoid singular math.',
    },
)

#: Lookup by id for cheap validation.
REFERENCE_PLANE_IDS = tuple(p['id'] for p in REFERENCE_PLANES)


def is_valid_reference_plane(reference_plane: str) -> bool:
    """Return ``True`` when ``reference_plane`` is one of the six known ids."""
    return str(reference_plane or '').strip().lower() in REFERENCE_PLANE_IDS


def reference_plane_meta(reference_plane: str) -> dict | None:
    """Return the plane dict for ``reference_plane`` id, or ``None`` if unknown."""
    key = str(reference_plane or '').strip().lower()
    for plane in REFERENCE_PLANES:
        if plane['id'] == key:
            return dict(plane)
    return None


def parse_s2p_freqs(s2p_text: str) -> np.ndarray:
    """Extract the frequency column (Hz) from a Touchstone .s2p file.

    Only the **first numeric column** (frequency) of every data row is read;
    comment lines (starting with ``!`` or ``#``) and blank lines are ignored.
    Returns a strictly-increasing ``float`` array in Hz, or an empty array when
    no data rows are found.  Units are converted: ``HZ``/``GHZ``/``MHZ``/``KHZ``
    per the most recent ``#`` option line (default Hz).
    """
    freqs: list[float] = []
    scale = 1.0  # default Hz
    for raw in (s2p_text or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            token = line[1:].strip().upper()
            if token.startswith('HZ'):
                scale = 1.0
            elif token.startswith('GHZ'):
                scale = 1e9
            elif token.startswith('MHZ'):
                scale = 1e6
            elif token.startswith('KHZ'):
                scale = 1e3
            continue
        if line.startswith('!'):
            continue
        parts = line.split()
        try:
            freqs.append(float(parts[0]) * scale)
        except (ValueError, IndexError):
            continue
    return np.asarray(sorted(freqs), float)


def read_s2p_frequencies(path) -> np.ndarray:
    """Read the frequency column of an .s2p file on disk (see :func:`parse_s2p_freqs`)."""
    p = Path(path)
    if not p.is_file():
        return np.array([], dtype=float)
    try:
        return parse_s2p_freqs(p.read_text(encoding='utf-8', errors='replace'))
    except OSError:
        return np.array([], dtype=float)


def check_s2p_coverage(s2p_freqs, library_freqs, tol_hz: float = 1.0) -> dict:
    """Verify every library calibration frequency has a fixture sample.

    ``s2p_freqs`` / ``library_freqs`` may be lists, arrays or paths.  When a path
    is given it is read with :func:`read_s2p_frequencies`.  A library frequency
    is *covered* when some fixture sample lies within ``tol_hz`` of it; otherwise
    that frequency is reported as ``missing`` (de-embedding would require an
    interpolation that standard practice explicitly forbids).

    Returns a JSON-serialisable report with ``passed`` / ``missing`` /
    ``covered`` / ``s2p_count`` / ``library_count``.
    """
    def _as_freqs(value):
        if isinstance(value, (str, Path)):
            return read_s2p_frequencies(value)
        if value is None:
            # No library loaded yet: an empty list, not a NaN placeholder that
            # would later be reported as a "missing frequency".
            return np.zeros(0, dtype=float)
        return np.asarray(value, dtype=float).ravel()

    s2p = _as_freqs(s2p_freqs)
    library = _as_freqs(library_freqs)
    if library.size == 0:
        return {
            'passed': False,
            's2p_count': int(s2p.size),
            'library_count': 0,
            'covered': 0,
            'missing': [],
            'reason': 'no library frequencies to check against; '
                      'open or create a calibration library first',
        }
    if s2p.size == 0:
        return {
            'passed': False,
            's2p_count': 0,
            'library_count': int(library.size),
            'covered': 0,
            'missing': [float(v) for v in library],
            'reason': 'no fixture S2P frequencies available',
        }
    missing = []
    covered = 0
    for f in library:
        if np.any(np.abs(s2p - float(f)) <= tol_hz):
            covered += 1
        else:
            missing.append(float(f))
    return {
        'passed': len(missing) == 0,
        's2p_count': int(s2p.size),
        'library_count': int(library.size),
        'covered': covered,
        'missing': missing,
        'reason': (None if not missing
                   else 'S2P is not interpolated between frequencies; '
                        f'missing {len(missing)} library frequency(ies)'),
    }


def validate_reference_setup(reference_plane: str, deembed_s2p: str,
                             library_freqs, tol_hz: float = 1.0) -> dict:
    """Combine the reference-plane vocabulary check with the S2P coverage gate.

    Used by the premise checklist (F-03) and the calibration wizard (F-09).
    Returns ``{'reference_plane_ok', 'deembed_allowed', 'coverage'}``.
    """
    meta = reference_plane_meta(reference_plane)
    plane_ok = meta is not None
    deembed_allowed = bool(meta and meta['deembed']) if plane_ok else False
    coverage = None
    if deembed_s2p:
        coverage = check_s2p_coverage(deembed_s2p, library_freqs, tol_hz=tol_hz)
    return {
        'reference_plane_ok': plane_ok,
        'reference_plane_name': (meta['name'] if meta else None),
        'deembed_allowed': deembed_allowed,
        'deembed_s2p': str(deembed_s2p or ''),
        'coverage': coverage,
    }


def _json_safe(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)

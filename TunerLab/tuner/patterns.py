"""Target patterns for the tuner (P2 — the Pattern Editor).

A standard *Pattern Editor* is where an operator describes **where the tuner should
go** before it goes there: a circle of constant |Γ|, a radial sweep, or a set of
points picked by hand on the Smith chart.  The pattern is then handed to the
tuner as a list of target impedances, so a whole sequence can be run without
the operator touching the machine between the points.

A pattern is a small, self-describing JSON file:

.. code::

    {
      "name": "0.8 圆 · 8 点",
      "kind": "circle",
      "parameters": {"radius": 0.8, "phase_start": 0.0, "phase_stop": 360.0,
                     "count": 8},
      "points": [[0.8, 0.0], [0.5657, 0.5657], ...],
      "created": 1762612345.0,
      "z0": 50.0
    }

``points`` is always the *resolved* list of |Γ| pairs, so a pattern can be
reproduced exactly even if the generator changes later.  The generators here are
pure functions of their parameters, and the file keeps both.

Patterns live next to the calibrations, in ``Documents/TunerLab/patterns``, so
they are visible to the operator and survive a reinstall.
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

__all__ = [
    'PATTERN_KINDS',
    'delete_pattern',
    'generate_points',
    'list_patterns',
    'load_pattern',
    'pattern_dir',
    'save_pattern',
]


#: The kinds the editor can produce.
PATTERN_KINDS = ('points', 'circle', 'radial')

_created_lock = threading.Lock()
_last_created = 0.0


def _creation_time() -> float:
    # Python 3.12 on Windows can return the same wall clock value for rapid
    # saves. Keep the existing JSON timestamp format and order tied saves by
    # the next representable value, including concurrent web requests.
    global _last_created
    with _created_lock:
        _last_created = max(time.time(), math.nextafter(_last_created, math.inf))
        return _last_created


def pattern_dir(base=None) -> Path:
    """Where the pattern files live."""
    if base is not None:
        return Path(base)
    from .appdirs import pattern_dir as _policy
    return _policy()


def generate_points(kind: str, parameters: dict) -> list[complex]:
    """The points a generator produces.

    ``points``
        The operator's own list — ``parameters['points']`` is a list of
        ``[re, im]`` pairs (or ``{'re':…, 'im':…}``).
    ``circle``
        A constant-|Γ| circle: ``radius``, ``phase_start``, ``phase_stop``
        (degrees) and ``count``.
    ``radial``
        A radial sweep at one phase: ``r_from``, ``r_to``, ``phase`` (degrees)
        and ``count``.
    """
    kind = str(kind or 'points').strip().lower()
    if kind not in PATTERN_KINDS:
        raise ValueError(f'unknown pattern kind: {kind!r}')

    if kind == 'points':
        raw = parameters.get('points') or []
        return [_as_gamma(item) for item in raw]

    if kind == 'circle':
        radius = float(parameters.get('radius', 0.5))
        start = float(parameters.get('phase_start', 0.0))
        stop = float(parameters.get('phase_stop', 360.0))
        count = int(parameters.get('count', 8))
        if count < 1:
            raise ValueError('count must be at least 1')
        if radius < 0:
            raise ValueError('radius must not be negative')
        span = stop - start
        # A full turn must not repeat the first point.
        divisor = count if abs(span - 360.0) < 1e-9 else max(1, count - 1)
        return [radius * complex(math.cos(math.radians(start + span * i / divisor)),
                                 math.sin(math.radians(start + span * i / divisor)))
                for i in range(count)]

    # radial
    r_from = float(parameters.get('r_from', 0.0))
    r_to = float(parameters.get('r_to', 0.8))
    phase = float(parameters.get('phase', 0.0))
    count = int(parameters.get('count', 8))
    if count < 1:
        raise ValueError('count must be at least 1')
    angle = math.radians(phase)
    step = (r_to - r_from) / max(1, count - 1) if count > 1 else 0.0
    return [complex((r_from + step * i) * math.cos(angle),
                    (r_from + step * i) * math.sin(angle))
            for i in range(count)]


def _as_gamma(value) -> complex:
    """One Γ from whatever shape a caller used.

    A complex number, a ``[re, im]`` pair (the JSON form the web API sends)
    and a ``{'re':…, 'im':…}`` mapping are all accepted.
    """
    if isinstance(value, complex):
        return value
    if isinstance(value, dict):
        return complex(float(value.get('re', 0.0)), float(value.get('im', 0.0)))
    if isinstance(value, (list, tuple)):
        re = float(value[0]) if len(value) > 0 else 0.0
        im = float(value[1]) if len(value) > 1 else 0.0
        return complex(re, im)
    return complex(value)


def save_pattern(name: str, kind: str, parameters: dict, points=None,
                 base=None, z0: float = 50.0) -> dict:
    """Write a pattern file and return the stored record."""
    name = str(name or '').strip()
    if not name:
        raise ValueError('a pattern needs a name')
    if points is None:
        points = generate_points(kind, parameters)
    points = [_as_gamma(value) for value in points]
    record = {
        'name': name,
        'kind': str(kind or 'points'),
        'parameters': dict(parameters or {}),
        'points': [[value.real, value.imag] for value in points],
        'count': len(points),
        'z0': float(z0),
        'created': _creation_time(),
    }
    directory = pattern_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f'{_safe_name(name)}.json'
    target.write_text(json.dumps(record, indent=2, ensure_ascii=False),
                      encoding='utf-8')
    record['path'] = str(target)
    return record


def _safe_name(name: str) -> str:
    """A file name that is safe on every platform."""
    keep = []
    for char in str(name):
        keep.append(char if char.isalnum() or char in '-_' else '_')
    text = ''.join(keep).strip('_') or 'pattern'
    return text[:80]


def list_patterns(base=None) -> list[dict]:
    """Every stored pattern, newest first."""
    directory = pattern_dir(base)
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(directory.glob('*.json')):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except Exception:  # noqa: BLE001 - one broken file must not hide the rest
            continue
        record['path'] = str(path)
        records.append(record)
    records.sort(key=lambda item: float(item.get('created', 0.0)), reverse=True)
    return records


def load_pattern(path) -> dict:
    """Read one pattern file."""
    target = Path(path)
    record = json.loads(target.read_text(encoding='utf-8'))
    record['path'] = str(target)
    return record


def delete_pattern(path) -> bool:
    """Remove a pattern file; returns whether it existed."""
    target = Path(path)
    if not target.is_file():
        return False
    target.unlink()
    return True


def pattern_points(record: dict) -> list[complex]:
    """The resolved Γ values of a stored pattern."""
    return [_as_gamma(item) for item in (record.get('points') or [])]

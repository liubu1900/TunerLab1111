"""In-process, thread-safe record of the *real* VNA calibration status.

Pure standard library: no PySide6, no h5py, no numpy, no UI.  This module is
the single source of truth consumed by the VNA drivers when they stamp a
``Measurement`` with calibration provenance (F-04).  It never talks to an
instrument on its own; :func:`validate_via_scpi` is an explicitly read-only
probe that issues only ``?`` queries and swallows every failure.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, fields, replace
from typing import Optional

#: Default validity window for a verified calibration, in days.
DEFAULT_MAX_AGE_DAYS = 7.0


@dataclass
class VnaCalibrationState:
    """Snapshot of the operator-confirmed VNA calibration."""

    method: str = ''
    cal_kit: str = ''
    timestamp: Optional[float] = None
    reference_plane: str = ''
    verified: bool = False
    note: str = ''


_lock = threading.RLock()
_state = VnaCalibrationState()

_FIELD_NAMES = frozenset(f.name for f in fields(VnaCalibrationState))


def get_state() -> VnaCalibrationState:
    """Return a copy of the current state (callers cannot mutate the store)."""
    with _lock:
        return replace(_state)


def set_state(**kwargs) -> VnaCalibrationState:
    """Update whitelisted fields and return a copy of the new state.

    Unknown keys raise :class:`ValueError` rather than being silently dropped,
    so a typo cannot produce a "verified" record with the wrong field name.
    """
    unknown = set(kwargs) - _FIELD_NAMES
    if unknown:
        raise ValueError(f'unknown VNA calibration field(s): {sorted(unknown)}')
    with _lock:
        global _state
        _state = replace(_state, **kwargs)
        return replace(_state)


def clear() -> VnaCalibrationState:
    """Reset to a pristine, unverified state and return a copy of it."""
    with _lock:
        global _state
        _state = VnaCalibrationState()
        return replace(_state)


def as_dict() -> dict:
    """Return the current state as a plain dict."""
    with _lock:
        return asdict(_state)


def age_days() -> Optional[float]:
    """Age of the calibration timestamp in days, or ``None`` if unset.

    A future-dated timestamp (clock skew) is clamped to zero.
    """
    with _lock:
        ts = _state.timestamp
    if ts is None:
        return None
    try:
        delta = (time.time() - float(ts)) / 86400.0
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0.0, delta)


def is_valid(max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> bool:
    """``True`` only when verified, timestamped and within the validity window."""
    with _lock:
        verified = _state.verified
    age = age_days()
    if not verified or age is None:
        return False
    try:
        return bool(age <= float(max_age_days))
    except (TypeError, ValueError):
        return False


def _reason(snapshot: VnaCalibrationState, age: Optional[float], valid: bool) -> str:
    if (snapshot.timestamp is None and not snapshot.verified
            and not snapshot.method and not snapshot.cal_kit
            and not snapshot.reference_plane):
        return '无 VNA 校准记录'
    if not snapshot.verified:
        return 'VNA 校准未验证'
    if snapshot.timestamp is None or age is None:
        return '缺少校准时间戳'
    if not valid:
        return f'校准已超过 7 天（{age:.1f} 天）'
    return '校准有效'


def status_report() -> dict:
    """Human-readable status summary for the UI / logs."""
    with _lock:
        snapshot = replace(_state)
    age = age_days()
    valid = is_valid()
    return {
        'verified': bool(snapshot.verified),
        'valid': bool(valid),
        'method': snapshot.method,
        'cal_kit': snapshot.cal_kit,
        'timestamp': snapshot.timestamp,
        'age_days': age,
        'reference_plane': snapshot.reference_plane,
        'reason': _reason(snapshot, age, valid),
    }


def validate_via_scpi(vna) -> dict:
    """Best-effort, read-only SCPI probe of the instrument's correction state.

    Only ``?`` queries are ever sent; no write/configuration command is issued.
    Any exception (no such method, transport error, unsupported query, ...) is
    caught and reported as ``{'queried': False, 'reason': ...}``.
    """
    if vna is None:
        return {'queried': False, 'reason': '未提供 VNA 对象'}
    query = getattr(vna, 'query', None)
    if not callable(query):
        query = getattr(vna, 'query_ascii_values', None)
    if not callable(query):
        return {'queried': False, 'reason': 'VNA 对象不支持只读查询接口'}

    results: dict = {}
    for command in (':SENS1:CORR:STAT?', ':SENS1:CORR:DATA?'):
        try:
            results[command] = query(command)
        except Exception as exc:  # noqa: BLE001 - never surface instrument errors
            return {'queried': False, 'reason': f'SCPI 查询失败：{exc}'}
    return {'queried': True, 'reason': '已读取校准状态', 'queries': results}

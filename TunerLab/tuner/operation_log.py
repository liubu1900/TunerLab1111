"""Process-wide operation log.

Every operator-visible action is recorded here: axis motion, homing, scale
self-checks, parameter writes, emergency stops, instrument connections and the
calibration run.  The log is the audit trail for a session — when a machine
misbehaves, the question is always "what was commanded, in which order, and
what did the controller say back?".

Design constraints:

* **Thread safe.**  HTTP handlers run on different threads of the threading
  server and the calibration worker runs on its own thread.
* **Bounded.**  The in-memory buffer is a ring buffer, so a long session cannot
  exhaust memory.
* **Optional on-disk trail.**  A JSONL sink can be attached so the log
  survives a restart.  Writing to disk never blocks the caller for longer than
  a single ``write`` call.
* **No secrets.**  Only the operator's own actions are recorded; the log never
  contains credentials.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

__all__ = ['LogEntry', 'OperationLog', 'operation_log', 'CATEGORIES']

#: Canonical log categories, so the UI can group entries consistently.
CATEGORIES = ('通讯', '运动', '回零', '参数', '急停', '标度', '矢网', '校准', '系统')

#: Severity levels, ordered from least to most serious.
LEVELS = ('info', 'warn', 'error')


@dataclass(frozen=True)
class LogEntry:
    """One recorded operation."""

    seq: int
    timestamp: str
    time_text: str
    category: str
    action: str
    detail: str = ''
    level: str = 'info'
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            'seq': self.seq,
            'timestamp': self.timestamp,
            'time_text': self.time_text,
            'category': self.category,
            'action': self.action,
            'detail': self.detail,
            'level': self.level,
            'data': dict(self.data),
        }


class OperationLog:
    """Thread-safe bounded operation log with an optional JSONL sink."""

    def __init__(self, capacity: int = 2000, sink: str | Path | None = None):
        if capacity < 1:
            raise ValueError('capacity must be positive')
        self.capacity = int(capacity)
        self._entries: deque[LogEntry] = deque(maxlen=self.capacity)
        self._lock = threading.RLock()
        self._seq = 0
        self._sink: Path | None = None
        if sink is not None:
            self.set_sink(sink)

    # -- configuration ---------------------------------------------------
    @property
    def sink(self) -> Path | None:
        return self._sink

    def set_sink(self, path: str | Path | None) -> Path | None:
        """Attach (or detach) a JSONL file that receives every new entry."""
        with self._lock:
            self._sink = Path(path) if path is not None else None
            if self._sink is not None:
                self._sink.parent.mkdir(parents=True, exist_ok=True)
            return self._sink

    # -- writing ---------------------------------------------------------
    def log(self, category: str, action: str, detail: str = '', level: str = 'info',
            **data: Any) -> LogEntry:
        """Record one operation and return the stored entry."""
        if level not in LEVELS:
            raise ValueError(f'unknown log level: {level!r}')
        now = datetime.now()
        with self._lock:
            self._seq += 1
            entry = LogEntry(
                seq=self._seq,
                timestamp=now.isoformat(timespec='milliseconds'),
                time_text=now.strftime('%H:%M:%S.%f')[:-3],
                category=str(category),
                action=str(action),
                detail=str(detail),
                level=level,
                data={k: _jsonable(v) for k, v in data.items()},
            )
            self._entries.append(entry)
            sink = self._sink
        if sink is not None:
            self._write_line(sink, entry)
        return entry

    def info(self, category: str, action: str, detail: str = '', **data: Any) -> LogEntry:
        return self.log(category, action, detail, 'info', **data)

    def warn(self, category: str, action: str, detail: str = '', **data: Any) -> LogEntry:
        return self.log(category, action, detail, 'warn', **data)

    def error(self, category: str, action: str, detail: str = '', **data: Any) -> LogEntry:
        return self.log(category, action, detail, 'error', **data)

    def exception(self, category: str, action: str, exc: BaseException,
                  **data: Any) -> LogEntry:
        """Record a failed operation, keeping the exception type and message."""
        return self.log(category, action, f'{type(exc).__name__}: {exc}', 'error',
                        error_type=type(exc).__name__, **data)

    @staticmethod
    def _write_line(sink: Path, entry: LogEntry) -> None:
        try:
            with sink.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + '\n')
        except OSError:
            # A broken log sink must never take down the operation it describes.
            pass

    # -- reading ---------------------------------------------------------
    def recent(self, limit: int = 200, category: str | None = None,
               level: str | None = None, since_seq: int = 0) -> list[dict[str, Any]]:
        """Return the most recent entries, newest last.

        ``since_seq`` returns only entries newer than the given sequence number,
        which lets a polling client ask for "what happened since I last looked".
        """
        count = int(limit)
        if count < 1:
            return []
        with self._lock:
            items: Iterable[LogEntry] = reversed(self._entries)
            selected: list[LogEntry] = []
            for entry in items:
                if entry.seq <= since_seq:
                    continue
                if category is not None and entry.category != category:
                    continue
                if level is not None and entry.level != level:
                    continue
                selected.append(entry)
                if len(selected) >= count:
                    break
        selected.reverse()
        return [entry.to_dict() for entry in selected]

    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> int:
        """Drop the in-memory entries; the on-disk sink is left untouched."""
        with self._lock:
            removed = len(self._entries)
            self._entries.clear()
            return removed

    def export(self, path: str | Path) -> Path:
        """Write the whole buffer to a JSON file."""
        target = Path(path)
        with self._lock:
            payload = [entry.to_dict() for entry in self._entries]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding='utf-8')
        return target


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of arbitrary values into JSON-serialisable data."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, 'tolist'):
        try:
            return _jsonable(value.tolist())
        except Exception:  # noqa: BLE001
            pass
    return str(value)


#: The single log shared by the desktop console and the web console.
operation_log = OperationLog()

"""Serialise every call into ``zauxdll`` behind one re-entrant lock.

The controller handle is a single UDP session plus a small amount of state
inside the vendor DLL.  TunerLab talks to it from two threads at once:

* the motion page's status timer, on the GUI thread, every 300 ms;
* the move itself, on a worker thread, for as long as the axis is travelling;
* the *Stop* / *Emergency stop* buttons, deliberately on the GUI thread so they
  preempt a running move.

Nothing in the vendor documentation promises that concurrent calls on one
handle are safe, and a mismatch inside the DLL shows up as an access violation
— a silent process death with no Python traceback.  Every call is therefore
routed through one lock.

The lock is taken per **call**, never around a whole move: ``wait_idle`` is
implemented here as a loop that re-takes the lock on each poll, so the GUI can
keep reading positions while an axis is moving instead of freezing for the
entire travel.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

__all__ = ['LockedLink', 'dll_lock']

#: One lock for the whole process: the DLL keeps process-wide state as well.
_LOCK = threading.RLock()


def dll_lock() -> threading.RLock:
    """The lock guarding every vendor DLL call (re-entrant)."""
    return _LOCK


class LockedLink:
    """Transparent proxy that serialises the wrapped link's DLL calls.

    Attributes are forwarded unchanged; callables are wrapped so the lock is
    held for the duration of one call.  ``wait_idle`` is the exception: it is
    long-running, so it is re-implemented as a polling loop that takes the lock
    once per poll.
    """

    #: Methods that must not hold the lock for their whole duration.
    _LONG_RUNNING = frozenset({'wait_idle'})

    def __init__(self, link):
        object.__setattr__(self, '_link', link)
        object.__setattr__(self, '_lock', _LOCK)

    # -- attribute plumbing ----------------------------------------------
    def __getattr__(self, name: str) -> Any:
        value = getattr(self._link, name)
        if name in self._LONG_RUNNING:
            return self._wrap_wait_idle()
        if callable(value) and not isinstance(value, type):
            return self._wrap(name, value)
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith('_'):
            object.__setattr__(self, name, value)
        else:
            setattr(self._link, name, value)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f'LockedLink({self._link!r})'

    # -- wrappers --------------------------------------------------------
    def _wrap(self, name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        lock = self._lock
        real = self._link

        def call(*args, **kwargs):
            with lock:
                return getattr(real, name)(*args, **kwargs)

        call.__name__ = name
        call.__doc__ = getattr(function, '__doc__', None)
        return call

    def _wrap_wait_idle(self) -> Callable[..., bool]:
        """``wait_idle`` that releases the lock between polls."""
        link = self._link
        lock = self._lock

        def wait_idle(axes, timeout: float) -> bool:
            deadline = time.monotonic() + float(timeout)
            axis_list = list(axes)
            while time.monotonic() < deadline:
                with lock:
                    idle = all(link.if_idle(axis) for axis in axis_list)
                if idle:
                    return True
                time.sleep(0.02)
            return False

        wait_idle.__name__ = 'wait_idle'
        wait_idle.__doc__ = getattr(type(link).wait_idle, '__doc__', None)
        return wait_idle

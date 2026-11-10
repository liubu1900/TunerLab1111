"""Mock-only motion backend and the (optional) shared-X safe motion planner.

No motor transport is opened by this module. Hardware adapters must implement
this contract and enforce physical interlocks independently of the UI.

``TunerConfig.independent_axes`` selects the motion model. It defaults to
``True``: every axis is independent, no axis move is conditioned on another
axis' position, and :class:`MotionPlanner` issues one direct move. Setting it
to ``False`` restores the legacy retract-before-X interlock and the staged
retract -> X -> Z plan. Single-axis guards (soft limits, homing, emergency
stop, encoder verification) are never disabled by this switch.
"""
from __future__ import annotations
import threading
import time
import numpy as np
from .models import TunerConfig


class MotionError(RuntimeError):
    """Controller state or safety interlock prevented motion."""


class MotionTimeout(MotionError):
    """A move did not reach idle within its timeout budget.

    Unlike a limit trip or an encoder mismatch, a timeout does not mean the
    machine is unsafe to command -- it usually means the axis was given too
    little time (a slow long traverse) or stalled momentarily.  Callers such as
    the calibration loop may clear the software fault latch and retry.
    """


class MotionController:
    """Deterministic virtual controller; positive Mock Z inserts a probe.

    ``move_duration`` can inject a finite motion delay for timeout/stop tests.
    ``history`` records completed moves, never attempted or timed-out moves.
    Resetting an emergency stop requires homing again.
    """
    simulated = True

    def __init__(self, config: TunerConfig, move_duration: float = 0.):
        if not config.simulated:
            raise ValueError('Mock controller requires simulated=True; no real hardware backend configured')
        if not np.isfinite(move_duration) or move_duration < 0:
            raise ValueError('move_duration must be nonnegative and finite')
        self.config = config
        self.move_duration = float(move_duration)
        self.initialized = False
        self.homed = False
        #: Axes the controller reports as successfully homed.
        self.homed_axes: set[str] = set()
        self.stopped = False
        self._position = np.array([config.x_limits[0]] + [config.safe_z] * config.probes, dtype=float)
        self.history = []
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

    def initialize(self):
        with self._lock:
            self.initialized = True
        return self.read_position()

    def _ready(self, require_home=True):
        if not self.initialized:
            raise MotionError('initialize controller before motion')
        if self.stopped or self._stop_event.is_set():
            raise MotionError('emergency stop is latched')
        if require_home and not self.homed:
            raise MotionError('home controller before motion')

    def _wait(self, timeout):
        timeout = self.config.motion_timeout if timeout is None else float(timeout)
        if not np.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be positive and finite')
        start = time.monotonic()
        while time.monotonic() - start < self.move_duration:
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                self.homed = False
                raise TimeoutError('motion timed out; re-home before further movement')
            if self._stop_event.wait(min(.005, remaining)):
                self.homed = False
                raise MotionError('motion interrupted by emergency stop')
        self._ready(require_home=False)

    def home(self):
        with self._lock:
            self._ready(require_home=False)
            # Mock home emulates the controller bringing every axis to its
            # reference position; this is a homing action, not an interlock.
            safe = self._position.copy()
            safe[1:] = self.config.safe_z
            self._wait(None)
            self._position = safe
            self.history.append(safe.copy())
            home = safe.copy()
            home[0] = self.config.x_limits[0]
            self._wait(None)
            self._position = home
            self.history.append(home.copy())
            self.homed = True
            self.homed_axes = set(self.config.axes)
            return home.copy()

    def read_position(self):
        with self._lock:
            return self._position.copy()

    def move_to(self, position, timeout=None):
        target = self.config.validate_position(position).copy()
        with self._lock:
            self._ready()
            # Legacy interlock: only consulted when the configuration opts back in.
            if not self.config.independent_axes and target[0] != self._position[0]:
                if not (np.allclose(self._position[1:], self.config.safe_z, atol=1e-12, rtol=0)
                        and np.allclose(target[1:], self.config.safe_z, atol=1e-12, rtol=0)):
                    raise MotionError('shared X travel requires all probes retracted at both endpoints; use MotionPlanner')
            self._wait(timeout)
            self._position = target
            self.history.append(target.copy())
            return target.copy()

    def jog(self, axis, delta):
        if axis not in self.config.axes:
            raise ValueError(f'unknown axis: {axis}')
        target = self.read_position()
        target[self.config.axes.index(axis)] += float(delta)
        return self.move_to(target)

    def retract(self):
        target = self.read_position()
        target[1:] = self.config.safe_z
        return self.move_to(target)

    def emergency_stop(self):
        # Must not acquire motion lock: a running move needs this signal immediately.
        self.stopped = True
        self.homed = False
        self.homed_axes.clear()
        self._stop_event.set()

    def reset_emergency(self):
        with self._lock:
            if not self.initialized:
                raise MotionError('initialize controller first')
            self._stop_event.clear()
            self.stopped = False
            self.homed = False
            self.homed_axes.clear()


MockMotionController = MotionController


class MotionPlanner:
    """Move to a target, with an optional retract → X → Z staged plan.

    With ``config.independent_axes`` (the default) the planner issues exactly
    one direct move and never commands a probe axis on X's behalf. With
    ``independent_axes=False`` it restores the legacy three-stage plan
    (retract probes, translate X, insert probes) under one timeout budget.
    """
    def __init__(self, config: TunerConfig, controller: MotionController):
        self.config = config
        self.controller = controller

    def move_to(self, position, timeout=None):
        target = self.config.validate_position(position).copy()
        budget = self.config.motion_timeout if timeout is None else float(timeout)
        if not np.isfinite(budget) or budget <= 0:
            raise ValueError('timeout must be positive and finite')
        start = time.monotonic()
        def move(p):
            remaining = budget - (time.monotonic() - start)
            if remaining <= 0:
                raise TimeoutError('safe motion plan timed out')
            return self.controller.move_to(p, timeout=remaining)
        if self.config.independent_axes:
            return move(target)
        current = self.controller.read_position()
        if target[0] != current[0]:
            retracted = current.copy()
            retracted[1:] = self.config.safe_z
            if not np.array_equal(retracted, current):
                move(retracted)
            translated = retracted.copy()
            translated[0] = target[0]
            move(translated)
        return move(target)

    def __getattr__(self, name):
        # Allow workflows to use a planner wherever the motion contract is expected.
        return getattr(self.controller, name)

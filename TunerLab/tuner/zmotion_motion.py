"""Real-hardware motion backend built on the Zmotion driver.

This module adapts :class:`tuner.zmotion.ZmotionController` to the ``MotionController``
contract that :class:`tuner.motion.MotionPlanner` and the calibration code already
speak. The shared X axis and the three independent probe axes keep the same ordering as
the mock backend, so a calibration recorded with one backend can be replayed with the
other as long as the axis configuration matches.

Safety properties implemented here:

* every position is checked against the configured soft limits before it is sent,
* a move waits for the controller to report idle and then verifies the read-back
  position within tolerance, so a blocked axis is reported instead of assumed,
* the emergency stop latches and must be explicitly reset, and
* homing is a separate explicit action, never a side effect of connecting.

By default (``TunerConfig.independent_axes=True``) the axes have no coupling:
X may be commanded regardless of probe positions and X homing does not wait
for the probes. Set ``independent_axes=False`` to restore the legacy
retract-before-X interlock. These are per-axis guards only -- removing the
software interlock does not make a physical collision impossible, and a
hardware interlock is still required.
"""
from __future__ import annotations

import math
import time
from typing import Sequence

import numpy as np

from .axis_params import AxisParams, tuner_axes
from .models import TunerConfig
from .motion import MotionError, MotionTimeout
from .zmotion import ZmotionController, ZmotionError
from .zmotion_errors import (
    decode_axis_status, describe_axis_status, describe_controller_error,
    describe_stop_reason, has_fault,
)

__all__ = ['ZmotionMotionController', 'build_tuner_config']

#: Position read-back tolerance in user units (mm).
DEFAULT_POSITION_TOLERANCE = 0.002

#: Quantisation allowance for the encoder cross-check, in encoder counts.
#:
#: ``MPOS`` is an integer count (on this machine 1 count = 0.01 mm at a feedback
#: scale of 100), while ``DPOS`` is a millimetre float.  A commanded displacement
#: smaller than one count is simply invisible to the encoder.  Grid points such as
#: 1/3 mm are read back by the controller as 0.3333 mm, so the next move starts with
#: a ~3.3e-5 mm residue that must not be reported as a scale mismatch.
ENCODER_QUANTISATION_COUNTS = 1.0


def build_tuner_config(axis_params: dict[str, AxisParams],
                       x_limits: tuple[float, float] | None = None,
                       z_limits: tuple[float, float] | None = None,
                       safe_z: float | None = None,
                       axis_count: int | None = None,
                       independent_axes: bool = True) -> TunerConfig:
    """Derive the shared :class:`TunerConfig` from the per-axis parameters.

    The mock backend stores one pair of limits for all probe axes; the real machine is
    configured the same way here so that both backends expose an identical contract.

    ``x_limits`` / ``z_limits`` override the controller's own soft limits.  That matters
    for the calibration: the controller ships with the factory soft limits
    (+/- 200 000 000), which are far wider than the machine's real travel, so a
    calibration plan built from them would ask for absurd positions.  Passing the
    operator's declared travel box keeps every calibration point inside the range the
    operator declared, while the controller's soft limits remain the outer backstop.

    ``independent_axes`` defaults to ``True`` (no retract-before-X interlock);
    pass ``False`` to restore the legacy coupled-axis behaviour.
    """
    if axis_count is None:
        axis_count = len(axis_params)
    axes = tuner_axes(axis_count)
    limits = {}
    for name in axes:
        params = axis_params[name]
        limits[name] = (params.rs_limit, params.fs_limit)
    controller_x_limits = limits['X']
    controller_z_limits = limits[axes[1]]
    for name in axes[1:]:
        current = limits[name]
        controller_z_limits = (min(controller_z_limits[0], current[0]),
                               max(controller_z_limits[1], current[1]))
    resolved_x = tuple(x_limits) if x_limits is not None else controller_x_limits
    resolved_z = tuple(z_limits) if z_limits is not None else controller_z_limits
    resolved_safe = float(safe_z) if safe_z is not None else 0.0
    return TunerConfig(
        probes=len(axes) - 1,
        x_limits=resolved_x,
        z_limits=resolved_z,
        safe_z=resolved_safe,
        fixed_offsets=tuple(0.0 for _ in axes[1:]),
        simulated=False,
        independent_axes=independent_axes,
    )


class ZmotionMotionController:
    """Motion backend driving the axes of one tuner model through a Zmotion controller.

    The controller exposes ``X``, ``Z1``, ``Z2`` and ``Z3`` in that order, matching
    :attr:`tuner.axis_params.CONTROLLER_AXES`. Which of them belong to the tuner is
    decided by ``axis_count``: a 3-axis tuner uses the first three, a 4-axis tuner all
    four. The controller axis *indices* never change, so a stored calibration stays
    valid when the model is switched.
    """

    simulated = False

    def __init__(self, config: TunerConfig, link: ZmotionController,
                 axis_params: dict[str, AxisParams] | None = None,
                 position_tolerance: float = DEFAULT_POSITION_TOLERANCE,
                 axis_count: int | None = None):
        self.config = config
        self.link = link
        self.axis_params = axis_params or {}
        self.position_tolerance = float(position_tolerance)
        self.initialized = False
        self.homed = False
        #: Axes the controller reports as successfully homed.
        self.homed_axes: set[str] = set()
        self.stopped = False
        #: Latched after a motion fault (timeout, limit or feedback mismatch).
        #: Motion is blocked until :meth:`reset_fault` is called, so a position
        #: the software can no longer trust is never acted on again blindly.
        self.faulted = False
        #: Per-axis scale self-check state: 'unknown' / 'measured' / 'verified'.
        self.scale_state: dict[str, str] = {}
        self._stop_event = None
        if axis_count is None:
            axis_count = len(config.axes)
        #: The axes that belong to this tuner model.
        self.axes = tuner_axes(axis_count)

    # -- lifecycle -------------------------------------------------------
    def initialize(self):
        """Connect to the controller and read the current positions.

        Homing is deliberately *not* performed here: it moves the machine and must be
        an explicit operator decision. The controller does however remember whether each
        axis was homed, so that state is adopted instead of being guessed. Homing is
        tracked per axis, because one axis being unhomed must not block the others.
        """
        if not self.link.connected:
            self.link.open()
        self.initialized = True
        self.refresh_home_state()
        return self.read_position()

    def refresh_home_state(self) -> set[str]:
        """Adopt the controller's per-axis homing state."""
        self.homed_axes = {name for index, name in enumerate(self.axes)
                           if self.link.home_status(index) == 1}
        self.homed = len(self.homed_axes) == len(self.axes)
        return set(self.homed_axes)

    def scale_is_consistent(self, axis: str) -> bool:
        """Whether one commanded unit on ``axis`` is one millimetre.

        The core adapter reads the encoder in the controller's own counts, so
        ``feedback_scale`` *is* the commanded-to-measured ratio: 1.0 means the
        axis travels exactly as far as it is told, and a value far from 1.0
        means a calibration grid would sweep the machine by that factor.

        The desktop application overrides this method, because it reads the
        encoder in millimetres and therefore stores a different quantity in
        ``feedback_scale``.  Callers must ask the controller rather than read
        the field themselves.
        """
        params = self.axis_params.get(axis)
        if params is None:
            return True
        return abs(float(params.feedback_scale) - 1.0) <= 0.05

    def all_axes_homed(self) -> bool:
        """``True`` when the controller reports a successful homing on every axis."""
        if not self.link.connected:
            return False
        return all(self.link.home_status(index) == 1 for index in range(len(self.axes)))

    def close(self) -> None:
        """Release the controller link."""
        self.link.close()
        self.initialized = False

    def _require_ready(self, require_home: bool = True, axes: Sequence[str] | None = None) -> None:
        if not self.initialized:
            raise MotionError('initialize the controller before motion')
        if self.stopped:
            raise MotionError('emergency stop is latched; reset it and re-home before moving')
        if self.faulted:
            raise MotionError('a motion fault is latched; reset it before moving')
        if not require_home:
            return
        # The controller is the source of truth for the homing state. Homing can be
        # performed externally (e.g. in RTsys) without the software's knowledge, so we
        # re-adopt the controller's per-axis ``home_status`` before every move instead
        # of trusting a stale snapshot taken at ``initialize()``. The fault latch is the
        # one case where the software deliberately distrusts the controller, so it is
        # never overridden here.
        if not self.faulted:
            self.refresh_home_state()
        # Only the axes that are actually commanded have to be homed. An empty list
        # means "no axis has to be homed", so it must not fall back to all axes.
        wanted = list(self.axes) if axes is None else list(axes)
        unhomed = [name for name in wanted if name not in self.homed_axes]
        if unhomed:
            raise MotionError(
                f'home the axes before motion; not homed: {", ".join(unhomed)}')

    # -- axis parameters -------------------------------------------------
    def apply_axis_parameters(self, axis_params: dict[str, AxisParams]) -> None:
        """Write the configured parameters to the controller.

        Parameters are written before any motion is allowed, so the machine always runs
        with the speed and acceleration values the operator has saved.
        """
        if not self.link.connected:
            raise MotionError('controller is not connected')
        for name in self.axes:
            params = axis_params.get(name)
            if params is None:
                continue
            index = self.axes.index(name)
            self.link.set_atype(index, params.atype)
            self.link.set_units(index, params.units)
            self.link.set_speed(index, params.speed)
            self.link.set_accel(index, params.accel)
            self.link.set_decel(index, params.decel)
            self.link.set_creep(index, params.creep)
            self.link.set_jog_speed(index, params.jog_speed)
            self.link.set_invert_step(index, params.invert_step)
            self.link.set_datum_in(index, params.datum_in)
            self.link.set_home_wait(index, params.home_wait)
            self.link.set_fs_limit(index, params.fs_limit)
            self.link.set_rs_limit(index, params.rs_limit)
        self.axis_params = dict(axis_params)

    def read_axis_parameters(self) -> dict[str, dict[str, float]]:
        """Read the controller's current axis parameters, grouped per axis."""
        if not self.link.connected:
            raise MotionError('controller is not connected')
        result: dict[str, dict[str, float]] = {}
        for index, name in enumerate(self.axes):
            result[name] = {
                'feedback_scale': float(
                    self.axis_params[name].feedback_scale if name in self.axis_params else 1.0),
                'atype': float(self.link.get_atype(index)),
                'units': self.link.get_units(index),
                'speed': self.link.get_speed(index),
                'accel': self.link.get_accel(index),
                'decel': self.link.get_decel(index),
                'creep': self.link.get_creep(index),
                'jog_speed': self.link.get_jog_speed(index),
                'invert_step': float(self.link.get_invert_step(index)),
                'datum_in': float(self.link.get_datum_in(index)),
                'fs_limit': self.link.get_fs_limit(index),
                'rs_limit': self.link.get_rs_limit(index),
            }
        return result

    # -- motion ----------------------------------------------------------
    def home(self, timeout: float = 120.0):
        """Home every axis of this tuner model, with no order dependency.

        The commands are issued together and the controller reports idle for
        all axes; no axis is gated on another axis' homing state. A timeout or
        a failed axis clears the homing state so a half-homed machine is never
        reported as ready.
        """
        self._require_ready(require_home=False)
        if self.stopped:
            raise MotionError('emergency stop is latched; reset it before homing')
        if self.faulted:
            raise MotionError('a motion fault is latched; reset it before homing')
        for index, name in enumerate(self.axes):
            params = self.axis_params.get(name)
            mode = params.datum_mode if params else 1
            self.link.home_axis(index, mode)
        axes = list(range(len(self.axes)))
        if not self.link.wait_idle(axes, timeout):
            self.homed_axes.clear()
            self.homed = False
            raise MotionError('homing did not finish within the timeout')
        failed = [name for index, name in enumerate(self.axes)
                  if self.link.home_status(index) != 1]
        if failed:
            self.homed_axes.clear()
            self.homed = False
            raise MotionError(f'homing failed on axis(es): {", ".join(failed)}')
        self.refresh_home_state()
        return self.read_position()

    def home_axis_safe(self, axis: str, timeout: float = 120.0) -> np.ndarray:
        """Home a single axis with the same guards as :meth:`home`.

        The emergency stop and any latched fault must be clear and the axis
        must belong to this tuner model. With ``independent_axes=True`` (the
        default) X homing is not gated on the probe axes; with
        ``independent_axes=False`` the legacy "home every probe before X"
        dependency is enforced.
        """
        if axis not in self.axes:
            raise ValueError(f'unknown axis: {axis}')
        self._require_ready(require_home=False)
        if self.stopped:
            raise MotionError('emergency stop is latched; reset it before homing')
        if self.faulted:
            raise MotionError('a motion fault is latched; reset it before homing')
        if not self.config.independent_axes and axis == 'X':
            unreferenced = [name for name in self.axes
                            if name != 'X' and name not in self.homed_axes]
            if unreferenced:
                raise MotionError(
                    f'home every probe axis before X: not homed: '
                    f'{", ".join(unreferenced)}')
        index = self.axes.index(axis)
        params = self.axis_params.get(axis)
        mode = params.datum_mode if params else 1
        self.link.home_axis(index, mode)
        if not self.link.wait_idle([index], timeout):
            self.homed_axes.discard(axis)
            self.homed = len(self.homed_axes) == len(self.axes)
            raise MotionError(f'{axis} homing did not finish within the timeout')
        if self.link.home_status(index) != 1:
            self.homed_axes.discard(axis)
            self.homed = len(self.homed_axes) == len(self.axes)
            raise MotionError(f'homing failed on {axis}')
        self.homed_axes.add(axis)
        self.homed = len(self.homed_axes) == len(self.axes)
        return self.read_position()

    def _latch_fault(self, message: str) -> MotionError:
        """Record an unrecoverable motion fault and invalidate the homing state.

        After a timeout, limit trip or feedback mismatch the commanded position
        can no longer be trusted, so the axes are marked unhomed and motion is
        blocked until :meth:`reset_fault` clears the latch.
        """
        self.faulted = True
        self.homed_axes.clear()
        self.homed = False
        return MotionError(message)

    def _latch_fault_timeout(self, budget: float) -> MotionTimeout:
        """A timeout latches the fault like any other motion fault, but raises
        :class:`MotionTimeout` so a caller can retry instead of aborting.

        The latch is still raised (the commanded position can no longer be
        trusted) -- the retry is the caller's choice, and it must clear the
        latch via :meth:`reset_fault` before re-issuing the move.
        """
        self.faulted = True
        self.homed_axes.clear()
        self.homed = False
        return MotionTimeout(
            f'motion timed out after {budget:g}s before the axes reported idle; '
            're-home the axes before moving again')

    def read_position(self) -> np.ndarray:
        """Current commanded position of all axes in user units."""
        if not self.link.connected:
            raise MotionError('controller is not connected')
        return np.array([self.link.get_dpos(index) for index in range(len(self.axes))],
                        dtype=float)

    def read_feedback_position(self) -> np.ndarray:
        """Current encoder feedback position of all axes in user units."""
        if not self.link.connected:
            raise MotionError('controller is not connected')
        return np.array([self.link.get_mpos(index) for index in range(len(self.axes))],
                        dtype=float)

    def move_to(self, position, timeout=None):
        """Move the axes to an absolute position and verify the result.

        The controller reports the commanded position, so a blocked axis that never
        reaches its target is detected by comparing the read-back with the request.

        By default the axes are independent: X may change while the probes are
        inserted, and no probe axis is commanded on X's behalf. When
        ``config.independent_axes`` is False, the legacy interlock instead
        requires every probe to be retracted at both endpoints of an X move.
        Physical collision avoidance is then the operator's/hardware's job.
        """
        target = self.config.validate_position(position).copy()
        current = self.read_position()
        # Only axes that actually have to travel are commanded. Issuing a zero-length
        # move to an axis that is not homed, or that carries an alarm, serves no purpose
        # and is exactly where a machine gets hurt.
        moving = [index for index in range(len(self.axes))
                  if abs(target[index] - current[index]) > 1e-9]
        self._require_ready(axes=[self.axes[index] for index in moving])
        if not moving:
            return current
        if not self.config.independent_axes and target[0] != current[0]:
            probes_at_safe = np.allclose(current[1:], self.config.safe_z, atol=1e-9, rtol=0)
            target_at_safe = np.allclose(target[1:], self.config.safe_z, atol=1e-9, rtol=0)
            if not (probes_at_safe and target_at_safe):
                raise MotionError(
                    'shared X travel requires all probes retracted at both endpoints')
        axes = list(moving)
        start_feedback = self.read_feedback_position()
        for index in moving:
            self.link.move_absolute(index, float(target[index]))
        budget = self.config.motion_timeout if timeout is None else float(timeout)
        if not self.link.wait_idle(axes, budget):
            self.stop_all()
            raise self._latch_fault_timeout(budget)
        actual = self.read_position()
        if not np.allclose(actual, target, atol=self.position_tolerance, rtol=0):
            raise MotionError(
                f'position read-back mismatch: requested {target.tolist()}, '
                f'reported {actual.tolist()}')
        # Verify against the encoder, which is the only independent witness of the
        # physical displacement.
        end_feedback = self.read_feedback_position()
        measured = end_feedback - start_feedback
        commanded = target - current
        for index in moving:
            name = self.axes[index]
            params = self.axis_params.get(name)
            scale = params.feedback_scale if params else 1.0
            expected = commanded[index] * scale
            # ``measured``/``expected`` are encoder counts, but
            # ``position_tolerance`` is in millimetres: convert it before comparing.
            # One count of quantisation covers sub-count grid residues (e.g. a 1/3 mm
            # grid point read back as 0.3333 mm).  A genuine scale mismatch still
            # exceeds the 5 % band and is rejected.
            tolerance = max(self.position_tolerance * scale,
                            abs(expected) * 0.05,
                            ENCODER_QUANTISATION_COUNTS)
            if abs(measured[index] - expected) > tolerance:
                self.stop_all()
                raise self._latch_fault(
                    f'{name} encoder feedback does not match the commanded displacement: '
                    f'commanded {commanded[index]:.4f}, encoder moved {measured[index]:.4f} '
                    f'(expected {expected:.4f} at feedback scale {scale:g}). '
                    'The command scale and the feedback scale do not match.')
        return actual

    def jog(self, axis: str, delta: float):
        """Move one axis by a signed distance."""
        if axis not in self.axes:
            raise ValueError(f'unknown axis: {axis}')
        self._require_ready(axes=[axis])
        current = self.read_position()
        target = current.copy()
        target[self.axes.index(axis)] += float(delta)
        return self.move_to(target)

    def move_axis_to(self, axis: str, position: float, timeout=None) -> float:
        """Move a single axis to an absolute position.

        Only the requested axis is commanded, so the other axes stay exactly where they
        are. This is what the manual control page uses. With independent axes (the
        default) X is no longer gated on the probe positions; set
        ``independent_axes=False`` to restore that interlock.

        The result is verified against the **encoder feedback**, not against the
        commanded position: ``DPOS`` always equals the command by definition, so
        comparing it proves nothing. When the controller's command scale and its
        feedback scale disagree, the encoder shows the real displacement and the move
        is rejected.
        """
        if axis not in self.axes:
            raise ValueError(f'unknown axis: {axis}')
        self._require_ready(axes=[axis])
        index = self.axes.index(axis)
        target = float(position)
        params = self.axis_params.get(axis)
        limits = (params.rs_limit, params.fs_limit) if params else self.config.z_limits
        if not limits[0] <= target <= limits[1]:
            raise MotionError(
                f'{axis} target {target} is outside the configured limits {limits}')
        if not self.config.independent_axes and axis == 'X':
            current = self.read_position()
            if not np.allclose(current[1:], self.config.safe_z, atol=1e-9, rtol=0):
                raise MotionError(
                    'shared X travel requires all probes retracted; retract the probes first')
        start_dpos = self.link.get_dpos(index)
        start_feedback = self.link.get_mpos(index)
        commanded = target - start_dpos
        scale = params.feedback_scale if params else 1.0
        self.link.move_absolute(index, target)
        budget = self.config.motion_timeout if timeout is None else float(timeout)
        if not self.link.wait_idle([index], budget):
            self.stop_all()
            raise self._latch_fault_timeout(budget)
        end_dpos = self.link.get_dpos(index)
        end_feedback = self.link.get_mpos(index)
        measured = end_feedback - start_feedback
        expected = commanded * scale
        # See ``move_to``: compare in encoder counts, allowing one count of
        # quantisation for sub-count commanded displacements.
        tolerance = max(self.position_tolerance * scale,
                        abs(expected) * 0.05,
                        ENCODER_QUANTISATION_COUNTS)
        if abs(measured - expected) > tolerance:
            self.stop_all()
            raise self._latch_fault(
                f'{axis} encoder feedback does not match the commanded displacement: '
                f'commanded {commanded:.4f}, encoder moved {measured:.4f} '
                f'(expected {expected:.4f} at feedback scale {scale:g}). '
                'The command scale and the feedback scale do not match; run the scale '
                'self-check and correct UNITS before moving this axis again.')
        if abs(end_dpos - target) > self.position_tolerance:
            self.stop_all()
            raise self._latch_fault(
                f'{axis} commanded position read-back mismatch: requested {target}, '
                f'reported {end_dpos}')
        return end_feedback

    def probe_axis_scale(self, axis: str, distance: float = 0.005,
                         timeout: float = 30.0) -> dict[str, float]:
        """Measure how far an axis really travels per commanded unit.

        Performs a small forward move and returns to the start, then reports the ratio
        between the encoder displacement and the commanded displacement. A ratio of 1.0
        means the axis' ``UNITS`` matches its feedback scale; a ratio of 84 means the
        axis travels 84 times further than the commanded number suggests, which is a
        configuration error that must be corrected before production motion.

        The result also carries the correction for that error: with ``UNITS`` set to
        ``pulses per user unit`` and a measured ratio of ``r`` user units of travel per
        commanded unit, the value that makes one commanded unit equal one millimetre is
        ``UNITS / r``.  Writing that value back makes the controller's user unit a
        millimetre, which is what the calibration grid and the soft limits assume.

        The probe distance is in *commanded* units, so with a mis-scaled axis the
        physical travel is proportionally larger. Keep it small.
        """
        if axis not in self.axes:
            raise ValueError(f'unknown axis: {axis}')
        if not math.isfinite(distance) or abs(distance) <= 0:
            raise ValueError('probe distance must be a finite, non-zero number')
        self._require_ready(axes=[axis])
        index = self.axes.index(axis)
        start_dpos = self.link.get_dpos(index)
        start_feedback = self.link.get_mpos(index)
        if not (math.isfinite(start_dpos) and math.isfinite(start_feedback)):
            raise MotionError(f'{axis} start position is not finite; cannot probe scale')
        self.link.move_absolute(index, start_dpos + distance)
        if not self.link.wait_idle([index], timeout):
            self.stop_all()
            raise self._latch_fault(
                f'{axis} scale probe did not finish within the timeout')
        end_feedback = self.link.get_mpos(index)
        measured = end_feedback - start_feedback
        # Always return the axis to where it started, but do not let a failure on
        # the return trip hide the measured ratio from the caller.
        try:
            self.link.move_absolute(index, start_dpos)
            self.link.wait_idle([index], timeout)
        except Exception:  # noqa: BLE001 - the ratio below is still valid
            pass
        commanded = abs(distance)
        signed = measured / distance if distance else float('nan')
        ratio = abs(signed)
        units = float(self.link.get_units(index))
        consistent = bool(np.isfinite(ratio)) and ratio > 0 and abs(ratio - 1.0) <= 0.05
        suggested = (units / ratio) if (not consistent and np.isfinite(ratio)
                                        and ratio > 0) else units
        self.scale_state[axis] = 'measured'
        return {
            'axis': axis,
            'commanded': commanded,
            'feedback': measured,
            'signed_ratio': float(signed),
            'ratio': float(ratio),
            'units': units,
            'suggested_units': float(round(suggested, 3)),
            'consistent': consistent,
            'scale_state': self.scale_state[axis],
            'start_dpos': start_dpos,
            'restored_dpos': self.link.get_dpos(index),
        }

    def set_axis_units(self, axis: str, units: float) -> float:
        """Write the pulse equivalent (``UNITS``) of one axis to the controller.

        Changing ``UNITS`` changes how the controller converts user units into pulses,
        so the coordinate system moves with it: the axis has to be homed again before
        it may be used.
        """
        if axis not in self.axes:
            raise ValueError(f'unknown axis: {axis}')
        value = float(units)
        if value <= 0:
            raise ValueError('UNITS must be positive')
        if not math.isfinite(value):
            raise ValueError('UNITS must be finite')
        self.link.set_units(self.axes.index(axis), value)
        # The pulse equivalent was just changed, so the previously measured scale
        # no longer applies; it must be re-probed before this axis is trusted.
        self.scale_state[axis] = 'measured'
        return value

    def stop_axis(self, axis: str) -> None:
        """Cancel motion on one axis."""
        if axis not in self.axes:
            raise ValueError(f'unknown axis: {axis}')
        self.link.stop_axis(self.axes.index(axis))

    def stop_all(self) -> None:
        """Cancel motion on every axis."""
        self.link.stop_axes(range(len(self.axes)))

    def retract(self):
        """Move the three probe axes back to the safe Z position."""
        self._require_ready(axes=list(self.axes[1:]))
        target = self.read_position()
        target[1:] = self.config.safe_z
        return self.move_to(target)

    def emergency_stop(self) -> None:
        """Stop all axes and latch the emergency state.

        The controller performs a rapid stop; the latch additionally prevents the
        software from issuing further motion until it is reset.
        """
        self.link.emergency_stop()
        self.stopped = True
        self.faulted = True
        self.homed = False
        self.homed_axes.clear()

    def reset_emergency(self) -> None:
        """Clear the emergency latch. Homing must be repeated afterwards."""
        self.stopped = False
        self.homed = False
        self.homed_axes.clear()

    def reset_fault(self) -> None:
        """Clear a latched motion fault. Homing must be repeated afterwards."""
        self.faulted = False
        self.homed = False
        self.homed_axes.clear()

    # -- diagnostics -----------------------------------------------------
    def axis_status_snapshot(self, detailed: bool = False) -> list[dict[str, float]]:
        """Idle flag, positions, status words and decoded flags per axis.

        ``AXISSTATUS`` is a bit field whose bits the vendor documents; the
        ``AXIS_STOPREASON`` parameter latches the same information about the
        last stop.  Both are decoded here so the operator sees *what* the
        controller complains about instead of an opaque decimal number.

        ``detailed=True`` adds the drive enable flag and the measured speed.
        It costs extra round trips, so the fast status poll leaves it off.
        """
        if not self.link.connected:
            raise MotionError('controller is not connected')
        info = self.link.read_all_axis_info(len(self.axes))
        snapshot = []
        for index, name in enumerate(self.axes):
            status = int(info['status'][index])
            stop_reason = int(self.link.axis_stop_reason(index))
            dpos = float(info['dpos'][index])
            mpos = float(info['mpos'][index])
            entry: dict[str, float] = {
                'axis': name,
                'idle': int(info['idle'][index]),
                'dpos': dpos,
                'mpos': mpos,
                'following_error': dpos - mpos,
                'status': status,
                'status_text': describe_axis_status(status),
                'status_flags': decode_axis_status(status),
                'status_fault': has_fault(status),
                'stop_reason': stop_reason,
                'stop_reason_text': describe_stop_reason(stop_reason),
                'home_status': float(self.link.home_status(index)),
                'homed': self.link.home_status(index) == 1,
            }
            if detailed:
                entry['enable'] = int(self.link.axis_enable(index))
                entry['speed'] = float(self.link.measured_speed(index))
            snapshot.append(entry)
        return snapshot

    def read_controller_errors(self) -> dict[str, object]:
        """Read the controller's own error number and describe it.

        The query is issued on demand only, because an unknown Basic command
        can itself raise a controller error and would otherwise pollute the
        status reading.
        """
        if not self.link.connected:
            raise MotionError('controller is not connected')
        result = self.link.read_error_code()
        code = result.get('code')
        return {
            'code': code,
            'supported': bool(result.get('supported')),
            'command': result.get('command'),
            'raw': result.get('raw', ''),
            'text': (describe_controller_error(code) if result.get('supported')
                     else '控制器未提供错误码查询（固件不支持或任务无错误）'),
            'axes': self.axis_status_snapshot(detailed=False),
        }

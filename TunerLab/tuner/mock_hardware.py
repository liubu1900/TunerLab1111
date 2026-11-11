"""A virtual Zmotion backend for the hardware console.

The motion console normally drives a real Zmotion controller.  That is not
always possible: the machine may be on another bench, under construction, or
the operator may want to rehearse a procedure before touching the hardware.

This module provides a *virtual controller* that implements the same contract
as :class:`tuner.zmotion_motion.ZmotionMotionController`, so every page of the
console works unchanged against it.  It behaves like the real machine in the
ways that matter for the operator:

* the axis count follows the active tuner model (3-axis or 4-axis),
* homing is per axis and an axis that is not homed blocks only itself,
* the scale self-check reports a consistent ratio of 1.0 unless a fault is
  injected, and
* the emergency stop latches and has to be reset, exactly like the real one.

The virtual controller can also *inject faults* so that the operator can
practise reading the diagnostic pages — for example reproducing the axis-22
alarm signal that the real Z2 axis reported during commissioning.

Nothing in this module touches hardware; it is pure Python plus NumPy.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

from .axis_params import AxisParams, tuner_axes
from .models import TunerConfig
from .motion import MotionController, MotionError
from .zmotion_errors import (
    decode_axis_status, describe_axis_status, describe_stop_reason, has_fault,
)

__all__ = ['MockZmotionLink', 'MockZmotionController', 'build_virtual_config']

#: Status bit the real machine reported on the faulty Z2 axis (bit 22).
ALARM_INPUT_BIT = 22

#: Status bit that marks the axis as not homed (bit 3, remote drive error).
REMOTE_DRIVE_ERROR_BIT = 3


def build_virtual_config(axis_params: dict[str, AxisParams],
                         axis_count: int | None = None) -> TunerConfig:
    """A configuration for the virtual controller.

    The travel box is deliberately generous (the same 0–10 mm / 0–1 mm box the
    Mock calibration studio uses) so that a rehearsal of the calibration flow
    behaves like the real one.
    """
    if axis_count is None:
        axis_count = len(axis_params)
    axes = tuner_axes(axis_count)
    return TunerConfig(
        probes=len(axes) - 1,
        x_limits=(0.0, 10.0),
        z_limits=(0.0, 1.0),
        safe_z=0.0,
        fixed_offsets=tuple(0.0 for _ in axes[1:]),
        simulated=True,
    )


class MockZmotionLink:
    """A stand-in for :class:`tuner.zmotion.ZmotionController`.

    It exposes the small part of the driver interface the console actually
    uses, so the pages cannot tell the difference between a virtual and a
    real controller.
    """

    def __init__(self, axes: Sequence[str], ip: str = 'virtual://zmotion',
                 firmware: str = 'VIRTUAL-2.9600', axis_base: int = 0):
        self.axes = tuple(axes)
        self.ip = ip
        self.firmware = firmware
        self.connected = False
        #: Physical controller axis numbers owned by this tuner.  The link's
        #: dictionaries are keyed by physical axis, like the real driver.
        self.axis_base = int(axis_base)
        physical = range(self.axis_base, self.axis_base + len(self.axes))
        #: Per-axis status words; zero means "healthy".
        self.status_words: dict[int, int] = {index: 0 for index in physical}
        #: Per-axis stop reasons.
        self.stop_reasons: dict[int, int] = {index: 0 for index in physical}
        #: Per-axis homing state.
        self.homed: dict[int, bool] = {index: False for index in physical}
        #: Per-axis measured speed (virtual units per second).
        self.speeds: dict[int, float] = {index: 0.0 for index in physical}
        #: Per-axis drive enable flag.
        self.enabled: dict[int, int] = {index: 1 for index in physical}
        #: The controller error number reported by the virtual controller.
        self.error_code: int | None = None
        #: Positions are a *logical* vector; ``_backend`` replaces it with the
        #: mock motion backend's array.
        self._positions = np.zeros(len(self.axes), dtype=float)

    def retarget(self, axes: Sequence[str], axis_base: int) -> None:
        """Move this link to another tuner's axis block.

        The link models the *controller*, so it stays the same object when the
        other tuner takes over.  Only the axis block it answers for changes.
        """
        fields = ('axes', 'axis_base', 'status_words', 'stop_reasons', 'homed',
                  'speeds', 'enabled', '_positions')
        banks = getattr(self, '_banks', {})
        banks[self.axis_base] = {field: getattr(self, field) for field in fields}
        if axis_base in banks:
            for field, value in banks[axis_base].items():
                setattr(self, field, value)
        else:
            fresh = MockZmotionLink(axes, axis_base=axis_base)
            for field in fields:
                setattr(self, field, getattr(fresh, field))
        self._banks = banks

    def _logical(self, index: int) -> int:
        return int(index) - self.axis_base

    # -- lifecycle -------------------------------------------------------
    def open(self, ip: str | None = None, timeout: float = 3.0,
             retries: int = 1) -> int:
        if ip:
            self.ip = ip
        self.connected = True
        return 0

    def close(self) -> None:
        self.connected = False

    def search(self, ip: str = '', timeout_ms: int = 1000) -> bool:
        return True

    # -- axis state ------------------------------------------------------
    def home_status(self, index: int) -> int:
        return 1 if self.homed.get(index, False) else 0

    def home_axis(self, index: int, mode: int = 1) -> None:
        if self.status_words.get(index, 0):
            # A faulted axis refuses the homing cycle, like the real one.
            return
        self.homed[index] = True

    def wait_idle(self, axes: Sequence[int], timeout: float = 10.0) -> bool:
        return True

    def get_dpos(self, index: int) -> float:
        return self._positions[self._logical(index)]

    def get_mpos(self, index: int) -> float:
        return self._positions[self._logical(index)]

    def axis_stop_reason(self, index: int) -> int:
        return int(self.stop_reasons.get(index, 0))

    def axis_enable(self, index: int) -> int:
        return int(self.enabled.get(index, 1))

    def measured_speed(self, index: int) -> float:
        return float(self.speeds.get(index, 0.0))

    def read_all_axis_info(self, count: int) -> dict[str, np.ndarray]:
        """Bulk info for physical axes ``0..count-1`` (see the real driver)."""
        idle = np.ones(count, dtype=int)
        dpos = np.zeros(count, dtype=float)
        mpos = np.zeros(count, dtype=float)
        status = np.zeros(count, dtype=int)
        for logical in range(len(self.axes)):
            physical = self.axis_base + logical
            if physical >= count:
                continue
            dpos[physical] = self.get_dpos(physical)
            mpos[physical] = self.get_mpos(physical)
            status[physical] = int(self.status_words.get(physical, 0))
        return {'idle': idle, 'dpos': dpos, 'mpos': mpos, 'status': status}

    def read_error_code(self) -> dict[str, object]:
        return {
            'code': self.error_code,
            'supported': self.error_code is not None,
            'command': '?ERRCODE',
            'raw': '' if self.error_code is None else str(self.error_code),
        }

    # Positions are held per link instance (see ``__init__``), never shared
    # between the two virtual tuners.


class MockZmotionController:
    """A virtual controller with the contract of the real motion backend.

    The interface mirrors :class:`tuner.zmotion_motion.ZmotionMotionController`
    method for method, so the console pages need no special case.
    """

    simulated = True

    def __init__(self, config: TunerConfig,
                 axis_params: dict[str, AxisParams] | None = None,
                 axis_count: int | None = None,
                 motion: MotionController | None = None,
                 *, axis_base: int = 0, op_base: int = 0,
                 link: 'MockZmotionLink | None' = None):
        self.config = config
        if axis_count is None:
            axis_count = len(config.axes)
        self.axes = tuner_axes(axis_count)
        self.axis_params = dict(axis_params or {})
        self.axis_base = int(axis_base)
        self.op_base = int(op_base)
        self.initialized = False
        self.homed = False
        self.homed_axes: set[str] = set()
        self.stopped = False
        self.faulted = False
        self.scale_state: dict[str, str] = {}
        #: The scale mismatch the virtual controller reports. 1.0 means
        #: "the command scale and the feedback scale agree".
        self.feedback_ratio = 1.0
        self._motion = motion
        # One controller drives both virtual tuners, so the link is reused
        # when the other tuner takes over.  A fresh link would make the
        # console look as if the controller had been re-opened.
        if link is not None:
            self.link = link
            self.link.retarget(self.axes, self.axis_base)
        else:
            self.link = MockZmotionLink(self.axes, axis_base=self.axis_base)

    # -- logical <-> physical axis mapping (mirrors ZmotionMotionController) --
    @property
    def physical_axes(self) -> tuple[int, ...]:
        return tuple(self.axis_base + index for index in range(len(self.axes)))

    def physical_axis(self, axis: str | int) -> int:
        if isinstance(axis, str):
            try:
                logical = self.axes.index(axis)
            except ValueError as exc:
                raise ValueError(
                    f'unknown axis {axis!r}; expected one of {self.axes}') from exc
        else:
            logical = int(axis)
            if not 0 <= logical < len(self.axes):
                raise ValueError(f'logical axis index {logical} out of range')
        return self.axis_base + logical

    def output(self, axis: str | int) -> int:
        name = axis if isinstance(axis, str) else self.axes[int(axis)]
        configured = getattr(self.axis_params.get(name), 'home_output', -1)
        if configured >= 0:
            return configured
        raise MotionError(f'{name} 回零输出未配置，请先核对接线并保存参数')

    def axis_map(self) -> dict[str, int]:
        return {name: self.physical_axis(name) for name in self.axes}

    # -- lifecycle -------------------------------------------------------
    def initialize(self):
        self.link.open()
        self.initialized = True
        self.refresh_home_state()
        return self.read_position()

    def refresh_home_state(self) -> set[str]:
        self.homed_axes = {name for index, name in enumerate(self.axes)
                           if self.link.home_status(self.physical_axis(index)) == 1}
        self.homed = len(self.homed_axes) == len(self.axes)
        return set(self.homed_axes)

    def all_axes_homed(self) -> bool:
        return all(self.link.home_status(axis) == 1
                   for axis in self.physical_axes)

    def scale_is_consistent(self, axis: str) -> bool:
        """The simulated axes always travel exactly as far as they are told."""
        return True

    def close(self) -> None:
        self.link.close()
        self.initialized = False

    def _require_ready(self, require_home: bool = True,
                       axes: Sequence[str] | None = None) -> None:
        check = getattr(self, 'installation_check', None)
        if check:
            check(axes)
        if not self.initialized:
            raise MotionError('initialize the controller before motion')
        if self.stopped:
            raise MotionError('emergency stop is latched; reset it and re-home before moving')
        if self.faulted:
            raise MotionError('a motion fault is latched; reset it before moving')
        if not require_home:
            return
        wanted = list(self.axes) if axes is None else list(axes)
        unhomed = [name for name in wanted if name not in self.homed_axes]
        if unhomed:
            raise MotionError(
                f'home the axes before motion; not homed: {", ".join(unhomed)}')

    # -- fault injection -------------------------------------------------
    def inject_fault(self, axis: str, bit: int = ALARM_INPUT_BIT) -> int:
        """Raise a status bit on one axis so the diagnostics can be practised.

        ``bit=22`` reproduces the "alarm signal input" that the real Z2 axis
        reported during commissioning.
        """
        if axis not in self.axes:
            raise ValueError(f'unknown axis: {axis}')
        index = self.physical_axis(axis)
        self.link.status_words[index] = int(
            self.link.status_words.get(index, 0)) | (1 << int(bit))
        self.link.stop_reasons[index] = self.link.status_words[index]
        return int(self.link.status_words[index])

    def clear_faults(self) -> None:
        """Remove every injected fault."""
        for index in self.link.status_words:
            self.link.status_words[index] = 0
            self.link.stop_reasons[index] = 0

    # -- axis parameters -------------------------------------------------
    def apply_axis_parameters(self, axis_params: dict[str, AxisParams]) -> None:
        self.axis_params.update(axis_params)

    def read_axis_parameters(self) -> dict[str, dict[str, float]]:
        result: dict[str, dict[str, float]] = {}
        for name in self.axes:
            params = self.axis_params.get(name)
            result[name] = {
                'feedback_scale': float(params.feedback_scale) if params else 1.0,
                'atype': float(params.atype) if params else 4.0,
                'units': float(params.units) if params else 10000.0,
                'home_wait': float(params.home_wait) if params else 2.0,
                'speed': float(params.speed) if params else 10.0,
                'accel': float(params.accel) if params else 10.0,
                'decel': float(params.decel) if params else 10.0,
                'creep': float(params.creep) if params else 100.0,
                'jog_speed': float(params.jog_speed) if params else 100.0,
                'invert_step': float(params.invert_step) if params else 0.0,
                'datum_in': float(params.datum_in) if params else -1.0,
                'fs_limit': float(params.fs_limit) if params else 2e8,
                'rs_limit': float(params.rs_limit) if params else -2e8,
            }
        return result

    # -- motion ----------------------------------------------------------
    def _backend(self) -> MotionController:
        """The Mock motion backend that carries the virtual positions."""
        if self._motion is None:
            motion = MotionController(self.config)
            motion.initialize()
            self._motion = motion
            self.link._positions = motion._position  # type: ignore[attr-defined]
        return self._motion

    def home(self, timeout: float = 120.0):
        self._require_ready(require_home=False)
        if self.stopped:
            raise MotionError('emergency stop is latched; reset it before homing')
        if self.faulted:
            raise MotionError('a motion fault is latched; reset it before homing')
        motion = self._backend()
        motion.home()
        for index in self.physical_axes:
            self.link.homed[index] = True
        self.refresh_home_state()
        return self.read_position()

    def home_axis_safe(self, axis: str, timeout: float = 120.0) -> np.ndarray:
        """Home a single axis with the same guards as :meth:`home`.

        Mirrors :meth:`ZmotionMotionController.home_axis_safe`: the emergency
        stop and any latched fault must be clear and the axis must belong to
        this tuner model. With independent axes (the default) X homing is not
        gated on the probe axes.
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
        motion = self._backend()
        logical = self.axes.index(axis)
        motion._position[logical] = motion.config.x_limits[0] if axis == 'X' \
            else motion.config.safe_z  # type: ignore[attr-defined]
        self.link.homed[self.physical_axis(axis)] = True
        self.homed_axes.add(axis)
        self.homed = len(self.homed_axes) == len(self.axes)
        return self.read_position()

    def reset_fault(self) -> None:
        """Clear a latched motion fault. Homing must be repeated afterwards."""
        self.faulted = False
        self.homed = False
        self.homed_axes.clear()

    def read_position(self) -> np.ndarray:
        motion = self._backend()
        return np.asarray(motion.read_position(), dtype=float)

    def read_feedback_position(self) -> np.ndarray:
        motion = self._backend()
        # The virtual feedback agrees with the command unless a scale fault
        # was injected.
        return np.asarray(motion.read_position(), dtype=float) * self.feedback_ratio

    def move_to(self, position, timeout=None):
        target = self.config.validate_position(position).copy()
        motion = self._backend()
        result = motion.move_to(target, timeout=timeout)
        self.link._positions = motion._position  # type: ignore[attr-defined]
        return np.asarray(result, dtype=float)

    def jog(self, axis: str, delta: float):
        if axis not in self.axes:
            raise ValueError(f'unknown axis: {axis}')
        self._require_ready(axes=[axis])
        current = self.read_position()
        target = current.copy()
        target[self.axes.index(axis)] += float(delta)
        return self.move_to(target)

    def move_axis_to(self, axis: str, position: float, timeout=None) -> float:
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
        motion = self._backend()
        current = motion.read_position()
        delta = target - current[index]
        result = motion.move_to(current + np.eye(len(self.axes))[index] * delta)
        self.link._positions = motion._position  # type: ignore[attr-defined]
        return float(self.read_feedback_position()[index])

    def probe_axis_scale(self, axis: str, distance: float = 0.005,
                         timeout: float = 30.0) -> dict[str, float]:
        """Measure the commanded-to-feedback ratio of one axis.

        The virtual controller is consistent by default (ratio 1.0).  Setting
        :attr:`feedback_ratio` to another value reproduces a mis-scaled axis.
        """
        if axis not in self.axes:
            raise ValueError(f'unknown axis: {axis}')
        self._require_ready(axes=[axis])
        index = self.axes.index(axis)
        start = float(self.read_position()[index])
        end = start + float(distance)
        self.move_axis_to(axis, end)
        measured = (end - start) * self.feedback_ratio
        self.move_axis_to(axis, start)
        ratio = abs(measured) / abs(distance) if distance else float('nan')
        params = self.axis_params.get(axis)
        units = float(params.units) if params else 10000.0
        consistent = abs(ratio - 1.0) <= 0.05
        suggested = (units / ratio) if (not consistent and ratio
                                        and np.isfinite(ratio) and ratio > 0) else units
        return {
            'axis': axis,
            'commanded': abs(float(distance)),
            'feedback': measured,
            'ratio': ratio,
            'units': units,
            'suggested_units': float(round(suggested, 3)),
            'consistent': consistent,
            'start_dpos': start,
            'restored_dpos': float(self.read_position()[index]),
        }

    def set_axis_units(self, axis: str, units: float) -> float:
        if axis not in self.axes:
            raise ValueError(f'unknown axis: {axis}')
        value = float(units)
        if value <= 0:
            raise ValueError('UNITS must be positive')
        params = self.axis_params.get(axis)
        if params is not None:
            updated = params.to_dict()
            updated['units'] = value
            self.axis_params[axis] = AxisParams(**updated)
        return value

    def stop_axis(self, axis: str) -> None:
        if axis not in self.axes:
            raise ValueError(f'unknown axis: {axis}')

    def stop_all(self) -> None:
        self._backend()

    def retract(self):
        self._require_ready(axes=list(self.axes[1:]))
        target = self.read_position()
        target[1:] = self.config.safe_z
        return self.move_to(target)

    def emergency_stop(self) -> None:
        self.stopped = True
        self.faulted = True
        self.homed = False
        self.homed_axes.clear()
        for index in self.link.homed:
            self.link.homed[index] = False

    def reset_emergency(self) -> None:
        self.stopped = False
        self.faulted = False
        self.homed = False
        self.homed_axes.clear()

    # -- diagnostics -----------------------------------------------------
    def axis_status_snapshot(self, detailed: bool = False) -> list[dict[str, float]]:
        if not self.link.connected:
            raise MotionError('controller is not connected')
        info = self.link.read_all_axis_info(self.axis_base + len(self.axes))
        snapshot = []
        for index, name in enumerate(self.axes):
            physical = self.physical_axis(index)
            status = int(info['status'][physical])
            stop_reason = int(self.link.axis_stop_reason(physical))
            dpos = float(info['dpos'][physical])
            mpos = float(info['mpos'][physical])
            entry: dict[str, float] = {
                'axis': name,
                'physical_axis': physical,
                'idle': int(info['idle'][physical]),
                'dpos': dpos,
                'mpos': mpos,
                'following_error': dpos - mpos,
                'status': status,
                'status_text': describe_axis_status(status),
                'status_flags': decode_axis_status(status),
                'status_fault': has_fault(status),
                'stop_reason': stop_reason,
                'stop_reason_text': describe_stop_reason(stop_reason),
                'home_status': float(self.link.home_status(physical)),
                'homed': self.link.home_status(physical) == 1,
            }
            if detailed:
                entry['enable'] = int(self.link.axis_enable(physical))
                entry['speed'] = float(self.link.measured_speed(physical))
            snapshot.append(entry)
        return snapshot

    def read_controller_errors(self) -> dict[str, object]:
        result = self.link.read_error_code()
        code = result.get('code')
        return {
            'code': code,
            'supported': bool(result.get('supported')),
            'command': result.get('command'),
            'raw': result.get('raw', ''),
            'text': ('虚拟控制器未注入错误码（一切正常）' if not result.get('supported')
                     else f'虚拟控制器错误码 {code}'),
            'axes': self.axis_status_snapshot(detailed=False),
        }

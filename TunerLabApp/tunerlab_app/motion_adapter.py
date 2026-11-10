"""Motion backend with the unit and homing conventions of this machine.

Two corrections over :class:`tuner.zmotion_motion.ZmotionMotionController`, both
confirmed on the controller:

**1. ``MPOS`` is in 10 µm, ``DPOS`` is in mm.**  Measured on the machine:
Z1 ``DPOS = -0.1356`` with ``MPOS = -13.432`` → ``-13.432 × 0.01 = -0.1343 mm``;
Z2 ``DPOS = 0.1000`` with ``MPOS = 9.905`` → ``0.0990 mm``.  The ratio is 100, not
a scale defect.  Because the stock adapter compares the raw ``MPOS`` delta with the
``DPOS`` delta, it rejected *every* move as "encoder feedback does not match the
commanded displacement", and the scale self-check suggested dividing ``UNITS`` by
100 — which would have broken a correctly configured machine (2500/10000 pulses
per mm are right).

``feedback_scale`` is therefore 100 for every axis, and this class converts
``MPOS`` to millimetres for display and for the self-check.

**2. Homing is an output-port pulse, not ``DATUM``.**  On this machine the drive
homes itself: ``OP0…OP3`` correspond to ``X…Z3``, and the operator presses the same
button twice (output on, output off).  ``home_via_output()`` reproduces exactly
that, waits for the encoder to settle, and zeroes the commanded position.
"""
from __future__ import annotations

import ctypes
import time

from tuner.motion import MotionError
from tuner.operation_log import operation_log
from tuner.zmotion_motion import ZmotionMotionController

from .link_lock import dll_lock

__all__ = ['TunerMotionController', 'MPOS_TO_MM', 'DEFAULT_DATUM_INPUT']

#: Origin input channel per axis.  ``IN=1`` means the axis sits on its zero
#: switch, which is how the machine itself defines "homed".
DEFAULT_DATUM_INPUT: dict[str, int] = {'X': 16, 'Z1': 17, 'Z2': 18, 'Z3': 19}

#: One ``MPOS`` count is 10 µm; ``DPOS`` is millimetres.
MPOS_TO_MM = 0.01

#: Encoder counts per millimetre, i.e. the value of ``feedback_scale``.
MPOS_COUNTS_PER_MM = 1.0 / MPOS_TO_MM


class TunerMotionController(ZmotionMotionController):
    """``ZmotionMotionController`` in millimetres, with OP-based homing."""

    #: How long the output is held on, in seconds, for one homing pulse.
    HOME_PULSE_SECONDS = 0.3

    #: Give up waiting for the drive to finish homing after this long.
    HOME_SETTLE_TIMEOUT = 90.0

    #: How long a read of the origin inputs may be reused, in seconds.
    #:
    #: The status poll runs every 300 ms and touches four axes; reading the four
    #: inputs on every poll would triple the number of round trips for no benefit,
    #: because an origin switch does not flicker.
    DATUM_CACHE_SECONDS = 0.35

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._datum_cache: tuple[float, dict[str, int | None]] = (0.0, {})
        #: Axes homed through the drive's OP input.
        #:
        #: The drive owns that cycle, so the controller's ``HOME_STATUS`` stays 0
        #: even after a successful homing.  Without remembering them here the
        #: stock ``refresh_home_state()`` would mark the axis unhomed again before
        #: the next move and refuse it.
        self.op_homed: set[str] = set()
        #: Last measured travel per commanded unit, per axis, in millimetres.
        #:
        #: Filled by :meth:`probe_axis_scale`.  This is the value that says
        #: whether one commanded unit really is one millimetre on this machine.
        self.scale_ratio: dict[str, float] = {}

    def refresh_home_state(self) -> set[str]:
        """Adopt ``HOME_STATUS`` and keep the axes homed through OP."""
        super().refresh_home_state()
        self.homed_axes |= {name for name in self.op_homed if name in self.axes}
        self.homed = len(self.homed_axes) == len(self.axes)
        return set(self.homed_axes)

    def scale_is_consistent(self, axis: str) -> bool:
        """Whether one commanded unit on ``axis`` is one millimetre.

        The core implementation reads ``feedback_scale`` as the
        commanded-to-measured ratio and compares it with 1.0.  That is wrong
        here: this adapter reads the encoder in millimetres, so
        ``feedback_scale`` is the constant *encoder counts per millimetre*
        (100 on this machine) and says nothing about consistency.  Taking the
        base implementation at face value would refuse every axis — and with
        it the whole real-hardware calibration flow.

        Two sources are used instead:

        * the scale self-check, when it has been run: it measures the real
          travel per commanded unit and reports 1.0 on a healthy machine;
        * otherwise the controller's live ``UNITS`` against the configured one:
          if they still agree, the axis has not been touched since the
          commissioning and the machine is as configured.
        """
        ratio = self.scale_ratio.get(axis)
        if ratio is not None:
            return abs(float(ratio) - 1.0) <= 0.05
        params = self.axis_params.get(axis)
        if params is None:
            return True
        try:
            live = float(self.link.get_units(self.axes.index(axis)))
        except Exception:  # noqa: BLE001 - an unreadable axis is not a verdict
            return False
        return abs(live - float(params.units)) <= 1e-6 * max(1.0, abs(live))

    def reset_emergency(self) -> None:
        super().reset_emergency()
        self.op_homed.clear()

    def reset_fault(self) -> None:
        super().reset_fault()
        self.op_homed.clear()

    def _require_ready(self, require_home: bool = True, axes=None) -> None:
        """Drop a *stale* fault latch when the controller itself can move.

        The base guard refuses motion while :attr:`faulted` is set.  That flag is
        raised only by our own software (a move timeout or an encoder mismatch),
        never by the controller's fault word -- so the vendor tool can jog the
        machine freely while our latch stays set.  When the operator has since
        cleared the condition in the vendor software, the latch is stale: re-read
        the live axis status and, if no axis reports a fault and every axis is
        idle, clear it so the operator is not left stuck.  A genuine fault (or an
        emergency stop) still blocks, and the operator must re-home afterwards.
        """
        if self.faulted and not self.stopped and self.link.connected:
            if self._hardware_reports_no_fault(axes):
                self.reset_fault()
                operation_log.warn(
                    '运动', '自动解除锁存',
                    '控制器报告各轴无故障且已 idle，已清除陈旧的软件故障锁存；'
                    '请重新回零后再继续运动')
        super()._require_ready(require_home=require_home, axes=axes)

    def _hardware_reports_no_fault(self, axes) -> bool:
        """``True`` when every relevant axis is idle and reports no fault bit."""
        try:
            snapshot = self.axis_status_snapshot()
        except Exception:  # noqa: BLE001 - an unreadable controller is not healthy
            return False
        wanted = set(self.axes if axes is None else axes)
        for entry in snapshot:
            name = entry.get('axis')
            if name not in wanted:
                continue
            if entry.get('status_fault'):
                return False
            if not entry.get('idle'):
                return False
        return True

    # -- origin inputs ---------------------------------------------------
    def datum_channel(self, axis: str) -> int:
        """Input channel wired to ``axis``' zero switch (``-1`` if unknown)."""
        params = self.axis_params.get(axis)
        if params is not None and getattr(params, 'datum_in', -1) >= 0:
            return int(params.datum_in)
        return DEFAULT_DATUM_INPUT.get(axis, -1)

    def read_datum_inputs(self, max_age: float | None = None) -> dict[str, int | None]:
        """Level of every axis' origin input: ``1`` = on the zero switch.

        ``None`` means the level could not be read.  This is the machine's own
        definition of "homed": the green light on the drive is exactly this input.
        """
        limit = self.DATUM_CACHE_SECONDS if max_age is None else float(max_age)
        stamp, cached = self._datum_cache
        if cached and (time.monotonic() - stamp) <= limit:
            return dict(cached)
        levels: dict[str, int | None] = {}
        for name in self.axes:
            channel = self.datum_channel(name)
            if channel < 0:
                levels[name] = None
                continue
            try:
                reply = self.link._basic_query(f'?IN({channel})').strip()
                levels[name] = int(float(reply)) if reply else None
            except Exception:  # noqa: BLE001 - a dead input must not stop the poll
                levels[name] = None
        self._datum_cache = (time.monotonic(), dict(levels))
        return levels

    def origin_is_on(self, axis: str) -> bool | None:
        """``True`` when the axis sits on its zero switch; ``None`` if unknown."""
        level = self.read_datum_inputs().get(axis)
        return None if level is None else bool(level)

    # -- units -----------------------------------------------------------
    def axis_status_snapshot(self, detailed: bool = False) -> list[dict[str, float]]:
        """Positions in millimetres, ``MPOS`` converted from 10 µm counts."""
        snapshot = super().axis_status_snapshot(detailed)
        for entry in snapshot:
            entry['mpos_raw'] = float(entry['mpos'])
            entry['mpos'] = float(entry['mpos']) * MPOS_TO_MM
            entry['following_error'] = entry['dpos'] - entry['mpos']
            # ``home_status`` stays the latched "has been referenced" flag; the
            # origin input is reported next to it, because the switch opens again
            # as soon as the axis leaves zero.  Homing itself is only accepted
            # with this input ON (see home_via_output).
            origin = self.read_datum_inputs().get(entry['axis'])
            entry['datum_input'] = self.datum_channel(entry['axis'])
            entry['origin_on'] = None if origin is None else bool(origin)
            if entry['axis'] in self.op_homed:
                entry['home_status'] = 1.0
                entry['homed'] = True
        return snapshot

    def read_feedback_position_mm(self):
        """Encoder position of every axis in millimetres."""
        return [value * MPOS_TO_MM for value in self.read_feedback_position()]

    def probe_axis_scale(self, axis: str, distance: float = 0.005,
                         timeout: float = 30.0) -> dict[str, float]:
        """Measure the real travel per commanded unit, in millimetres.

        The stock implementation divides the raw ``MPOS`` delta (10 µm counts) by
        the ``DPOS`` delta (mm), so a healthy machine reports a ratio of ~100 and
        an "Adopt UNITS ÷ 100" suggestion.  Converting the encoder delta first
        makes the ratio 1.0 when the machine is configured correctly.
        """
        if axis not in self.axes:
            raise ValueError(f'unknown axis: {axis}')
        if not (distance == distance) or abs(distance) <= 0:  # NaN-safe
            raise ValueError('probe distance must be a finite, non-zero number')
        self._require_ready(axes=[axis])
        index = self.axes.index(axis)
        start_dpos = self.link.get_dpos(index)
        start_feedback = self.link.get_mpos(index)
        if not (start_dpos == start_dpos and start_feedback == start_feedback):
            raise MotionError(f'{axis} start position is not finite; cannot probe scale')

        self.link.move_absolute(index, start_dpos + distance)
        if not self.link.wait_idle([index], timeout):
            self.stop_all()
            raise self._latch_fault(f'{axis} scale probe did not finish within the timeout')
        end_feedback = self.link.get_mpos(index)
        measured_mm = (end_feedback - start_feedback) * MPOS_TO_MM
        try:
            self.link.move_absolute(index, start_dpos)
            self.link.wait_idle([index], timeout)
        except Exception:  # noqa: BLE001 - the measurement below is still valid
            pass

        commanded = abs(float(distance))
        signed = measured_mm / distance if distance else float('nan')
        ratio = abs(signed)
        units = float(self.link.get_units(index))
        consistent = bool(ratio == ratio) and ratio > 0 and abs(ratio - 1.0) <= 0.05
        self.scale_state[axis] = 'measured'
        self.scale_ratio[axis] = float(ratio)
        return {
            'axis': axis,
            'commanded': commanded,
            'feedback': measured_mm,
            'signed_ratio': float(signed),
            'ratio': float(ratio),
            'units': units,
            'suggested_units': units,
            'consistent': consistent,
            'scale_state': self.scale_state[axis],
            'start_dpos': start_dpos,
            'restored_dpos': self.link.get_dpos(index),
            'mpos_unit_mm': MPOS_TO_MM,
        }

    # -- output pulse ----------------------------------------------------
    def _set_op(self, index: int, on: bool) -> None:
        """Drive one digital output through ``ZAux_Direct_SetOp``."""
        if not self.link.connected:
            raise MotionError('controller is not connected')
        if self.link.lib is None:
            raise MotionError('Zmotion library is not loaded')
        function = getattr(self, '_set_op_function', None)
        if function is None:
            try:
                function = self.link.lib.dll.ZAux_Direct_SetOp
            except AttributeError as exc:
                raise MotionError(
                    'Zmotion library does not export ZAux_Direct_SetOp') from exc
            function.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint32]
            function.restype = ctypes.c_int32
            self._set_op_function = function
        # This reaches the DLL through ``lib.dll`` directly, bypassing the link
        # proxy, so it takes the same lock explicitly.
        with dll_lock():
            code = function(self.link._handle, ctypes.c_int32(int(index)),
                            ctypes.c_uint32(1 if on else 0))
            self.link.lib._check(code, f'SetOp({index}, {int(on)})')

    def read_op(self, index: int) -> int:
        """Current level of one digital output (0/1)."""
        if not self.link.connected:
            raise MotionError('controller is not connected')
        try:
            reply = self.link._basic_query(f'?OP({int(index)})')
        except Exception as exc:  # noqa: BLE001 - reported as "unknown"
            raise MotionError(f'cannot read OP{index}: {exc}') from exc
        text = reply.strip()
        return int(float(text)) if text else 0

    def home_via_output(self, axis: str, pulse: float | None = None,
                        settle_timeout: float | None = None) -> dict[str, object]:
        """Home one axis the way the operator does it in RTSys.

        The drive owns the homing cycle; the controller only has to pulse the
        matching output twice (on, then off).  After the pulse the encoder is
        watched until it stops moving, and the commanded position is cleared so
        the axis reports zero at its origin.
        """
        if axis not in self.axes:
            raise ValueError(f'unknown axis: {axis}')
        self._require_ready(require_home=False)
        if self.stopped:
            raise MotionError('急停已锁存，请先复位急停')
        if self.faulted:
            raise MotionError('运动故障已锁存，请先点「复位运动故障」')
        if not self.config.independent_axes and axis == 'X':
            unreferenced = [name for name in self.axes
                            if name != 'X' and name not in self.homed_axes]
            if unreferenced:
                raise MotionError(
                    f'先把探针轴回零，再回零 X：未回零 {", ".join(unreferenced)}')

        index = self.axes.index(axis)
        pulse_seconds = self.HOME_PULSE_SECONDS if pulse is None else float(pulse)
        timeout = self.HOME_SETTLE_TIMEOUT if settle_timeout is None else float(settle_timeout)
        start_mpos = self.link.get_mpos(index)
        started = time.monotonic()

        # The operator's two clicks: output on, then off.
        self._set_op(index, True)
        time.sleep(pulse_seconds)
        self._set_op(index, False)

        # Wait until the encoder stops moving (the drive finished its cycle).
        last = None
        stable_since = time.monotonic()
        while time.monotonic() - started < timeout:
            position = self.link.get_mpos(index)
            now = time.monotonic()
            if last is None or abs(position - last) > 0.5:  # 0.5 count = 5 µm
                last = position
                stable_since = now
            elif now - stable_since > 0.6:
                break
            time.sleep(0.1)

        elapsed = time.monotonic() - started
        travel_mm = (self.link.get_mpos(index) - start_mpos) * MPOS_TO_MM
        settled = elapsed < timeout

        # Homing is only successful when the axis stands on its zero switch: the
        # switch is what the drive's green light shows and what every later
        # absolute position depends on.  A cycle that ran to completion without
        # ever closing the switch (dead sensor, wrong input index, inverted
        # polarity) leaves the coordinate undefined -- do not zero DPOS and do
        # not claim success.
        channel = self.datum_channel(axis)
        origin = self.read_datum_inputs(max_age=0.0).get(axis)
        if origin is None:
            raise MotionError(
                f'{axis} 回零后无法读取原点输入 IN({channel})，无法确认是否到位。\n'
                '检查该轴的 `datum_in` 参数是否指向正确的输入口。')
        if not origin:
            raise MotionError(
                f'{axis} 回零未成功：轴已停止，但原点输入 IN({channel}) 仍为 0（应为 ON）。\n'
                f'  位移 {travel_mm * 1000:.0f} µm，耗时 {elapsed:.1f}s\n'
                f'  DPOS 未清零（仍为 {float(self.link.get_dpos(index)):.4f} mm）\n'
                '常见原因：原点传感器无信号 / 接线松动 / 输入口配错 / INVERT_IN 极性反了。')

        self.link.set_dpos(index, 0.0)
        self.op_homed.add(axis)
        self.homed_axes.add(axis)
        self.homed = len(self.homed_axes) == len(self.axes)
        return {
            'axis': axis,
            'output': index,
            'travel_mm': travel_mm,
            'elapsed_s': elapsed,
            'dpos': float(self.link.get_dpos(index)),
            'mpos_mm': float(self.link.get_mpos(index)) * MPOS_TO_MM,
            'settled': settled,
            'datum_input': channel,
            'origin_on': bool(origin),
        }

"""Per-axis motion parameters for the TunerLab Zmotion backend.

One :class:`AxisParams` instance describes one controller axis. The defaults reproduce
the configuration that was read back from the real controller (firmware 2.9600) during
commissioning, so a fresh install matches the machine it was taken from.

Units
-----
``units`` is the pulse equivalent (pulses per user unit) reported by the controller's
``UNITS`` parameter. Every other length in this module is expressed in **user units**
(millimetres on this machine); the controller converts to pulses internally.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict, fields
from typing import Any

#: Fields whose change moves the machine's coordinate frame (UNITS, direction,
#: limits, origin). Editing any of them invalidates homing and any calibration
#: that was recorded against the previous geometry, because a saved position no
#: longer maps to the same physical point. See :func:`affects_coordinate_frame`.
COORDINATE_FIELDS = (
    'units', 'invert_step', 'datum_in', 'datum_mode',
    'fs_limit', 'rs_limit', 'feedback_scale', 'home_output', 'datum_active', 'home_output_active', 'home_pulse',
)

#: Plausibility ceilings. The defaults are pulses-per-mm in the thousands and
#: speeds in the tens-to-hundreds; these bounds reject obvious corruption
#: (NaNs, signs, or absurd magnitudes) without constraining a legitimate machine.
_AXIS_PARAM_BOUNDS = {
    'units': (1e-6, 1e12),
    'speed': (1e-9, 1e7),
    'accel': (1e-9, 1e7),
    'decel': (1e-9, 1e7),
    'creep': (1e-9, 1e7),
    'jog_speed': (1e-9, 1e7),
    'fs_limit': (-1e13, 1e13),
    'rs_limit': (-1e13, 1e13),
    'feedback_scale': (1e-9, 1e9),
}

__all__ = ['AxisParams', 'AXIS_NAMES', 'axis_index', 'default_axis_params',
           'TUNER_AXES', 'CONTROLLER_AXES', 'TUNER_MODELS', 'tuner_axes',
           'axis_count_of', 'DEFAULT_AXIS_COUNT', 'COORDINATE_FIELDS',
           'affects_coordinate_frame']

#: Controller axis order. The shared X axis is index 0; the probes follow.
#:
#: This order is a property of the **controller wiring**, not of the tuner model:
#: a 3-axis tuner uses the first three entries, a 4-axis tuner all four. The
#: controller always exposes four axes, so the index of an axis never changes
#: when the tuner model changes.
CONTROLLER_AXES = ('X', 'Z1', 'Z2', 'Z3')

#: Tuner axis order of the four-axis model. Kept as the historical name because
#: it is the axis order that most call sites iterate over.
TUNER_AXES = CONTROLLER_AXES

#: Alias kept for readability at call sites that only need the names.
AXIS_NAMES = TUNER_AXES

#: Supported tuner models: number of axes -> axis names.
#:
#: A tuner with ``n`` axes has one shared X axis and ``n - 1`` probe axes.
TUNER_MODELS: dict[int, tuple[str, ...]] = {
    3: ('X', 'Z1', 'Z2'),
    4: ('X', 'Z1', 'Z2', 'Z3'),
}

#: Axis count assumed when nothing else is known.
DEFAULT_AXIS_COUNT = 4


def tuner_axes(axis_count: int) -> tuple[str, ...]:
    """Axis names of a tuner model with ``axis_count`` axes.

    Raises ``ValueError`` for an unsupported model, so a typo cannot silently
    produce a machine with the wrong number of axes.
    """
    try:
        count = int(axis_count)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'invalid axis count: {axis_count!r}') from exc
    if count not in TUNER_MODELS:
        raise ValueError(
            f'unsupported axis count {count}; expected one of '
            f'{sorted(TUNER_MODELS)}')
    return TUNER_MODELS[count]


def axis_count_of(axes) -> int:
    """Number of axes of a tuner model given by its axis names."""
    names = tuple(axes)
    for count, model in TUNER_MODELS.items():
        if model == names:
            return count
    raise ValueError(f'unsupported axis set: {names}')


def axis_index(name: str, axes: tuple[str, ...] | None = None) -> int:
    """Controller axis index for a Tuner axis name.

    ``axes`` restricts the lookup to a tuner model; the default accepts every
    axis the controller exposes.
    """
    pool = CONTROLLER_AXES if axes is None else tuple(axes)
    try:
        return pool.index(name)
    except ValueError as exc:
        raise ValueError(f'unknown axis {name!r}; expected one of {pool}') from exc


@dataclass
class AxisParams:
    """Motion parameters of one controller axis.

    Attributes mirror the vendor axis parameters one-to-one so that the settings file
    can be applied to the controller without a translation table.
    """

    name: str = 'X'
    #: Axis type. 4 = encoder feedback axis on this controller.
    atype: int = 4
    #: Pulse equivalent, i.e. pulses per user unit (mm).
    units: float = 2500.0
    #: Target speed in user units per second.
    speed: float = 10.0
    #: Acceleration in user units per second squared.
    accel: float = 10.0
    #: Deceleration in user units per second squared.
    decel: float = 10.0
    #: Creep speed used during homing.
    creep: float = 100.0
    #: Speed used by continuous jog moves.
    jog_speed: float = 100.0
    #: Pulse mode / direction inversion word.
    invert_step: int = 0
    #: Input channel that carries the origin sensor. ``-1`` disables it.
    datum_in: int = 16
    #: Reverse-search dwell time after the origin signal, in milliseconds.
    home_pulse: float = 0.3
    home_output_active: int = 1
    datum_active: int = 1
    home_output: int = -1
    home_wait: int = 2
    #: Positive software limit in user units.
    fs_limit: float = 200000000.0
    #: Negative software limit in user units.
    rs_limit: float = -200000000.0
    #: Homing mode passed to ``DATUM``. 1 = search origin input in the negative direction.
    datum_mode: int = 1
    #: Encoder units per commanded unit for this axis.
    #:
    #: The controller reports the commanded position (``DPOS``) and the encoder feedback
    #: (``MPOS``) on different scales when ``UNITS`` does not match the axis' real pulse
    #: equivalent. A value of 1.0 means both scales agree. The commissioning measurements
    #: on this machine gave roughly 67 (X), 84 (Z1) and 96 (Z3), which means the axes
    #: travelled that many times further than the commanded number suggested. Leave this
    #: at 1.0 until :meth:`ZmotionMotionController.probe_axis_scale` has measured it.
    feedback_scale: float = 1.0

    def __post_init__(self) -> None:
        if self.name not in TUNER_AXES:
            raise ValueError(f'unknown axis {self.name!r}; expected one of {TUNER_AXES}')
        # A non-finite value (NaN or Inf) must never reach the controller: it would
        # either be rejected by the driver or, worse, move an axis to an undefined
        # position. Reject it before anything is written.
        for label, value in (('units', self.units), ('speed', self.speed),
                             ('accel', self.accel), ('decel', self.decel),
                             ('creep', self.creep), ('jog_speed', self.jog_speed),
                             ('fs_limit', self.fs_limit), ('rs_limit', self.rs_limit),
                             ('feedback_scale', self.feedback_scale)):
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f'{label} must be a finite number, got {value!r}')
        if not isinstance(self.atype, int) or isinstance(self.atype, bool) or self.atype < 0:
            raise ValueError(f'atype must be a non-negative integer, got {self.atype!r}')
        if self.units <= 0:
            raise ValueError('units (pulse equivalent) must be positive')
        for label in ('speed', 'accel', 'decel', 'creep', 'jog_speed'):
            value = getattr(self, label)
            if value <= 0:
                raise ValueError(f'{label} must be positive')
        for label in _AXIS_PARAM_BOUNDS:
            lo, hi = _AXIS_PARAM_BOUNDS[label]
            value = getattr(self, label)
            if not (lo <= value <= hi):
                raise ValueError(
                    f'{label}={value!r} is outside the plausible range [{lo}, {hi}]')
        if self.fs_limit <= self.rs_limit:
            raise ValueError('positive limit must be greater than the negative limit')
        if not math.isfinite(float(self.home_pulse)) or not 0.001 <= self.home_pulse <= 10:
            raise ValueError('home_pulse must be 0.001..10 seconds')
        if self.home_output_active not in (0, 1) or self.datum_active not in (0, 1):
            raise ValueError('home_output_active and datum_active must be 0 or 1')
        if not isinstance(self.home_output, int) or not -1 <= self.home_output < 12:
            raise ValueError('home_output must be -1 or an output 0..11')
        if self.datum_in < -1:
            raise ValueError('datum_in must be -1 (disabled) or a valid input channel')
        if self.datum_mode < 0:
            raise ValueError('datum_mode must be non-negative')
        if self.feedback_scale <= 0:
                raise ValueError('feedback_scale must be positive')

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'AxisParams':
        """Build from a stored mapping, ignoring unknown keys.

        Unknown keys are ignored so that a settings file written by a newer version
        still loads, but a missing or invalid field raises.
        """
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f'unknown axis parameter(s): {sorted(unknown)}')
        return cls(**{k: v for k, v in data.items() if k in known})


def affects_coordinate_frame(changed: set[str]) -> bool:
    """``True`` when any of ``changed`` moves the machine's coordinate frame.

    Editing a field in :data:`COORDINATE_FIELDS` (UNITS, direction, limits or
    origin) changes where a saved position lands physically, so homing and any
    calibration recorded against the previous geometry must be invalidated.
    """
    return bool(changed & set(COORDINATE_FIELDS))


def default_axis_params(axis_count: int = DEFAULT_AXIS_COUNT) -> dict[str, AxisParams]:
    """Default parameters for the axes of one tuner model.

    The values match the controller that was commissioned for this project:
    ``X`` uses a pulse equivalent of 2500 while the probe axes use 10000, and each
    axis carries its own origin input (16, 17, 18, 19).

    ``axis_count`` selects the model: 3 axes give X/Z1/Z2, 4 axes give X/Z1/Z2/Z3.
    """
    defaults = {
        'X': AxisParams(name='X', units=2500.0, datum_in=16, home_output=0),
        'Z1': AxisParams(name='Z1', units=10000.0, datum_in=17, home_output=1),
        'Z2': AxisParams(name='Z2', units=10000.0, datum_in=18, home_output=2),
        'Z3': AxisParams(name='Z3', units=10000.0, datum_in=19, home_output=3),
    }
    return {name: defaults[name] for name in tuner_axes(axis_count)}

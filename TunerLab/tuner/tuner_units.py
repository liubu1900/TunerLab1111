"""Two tuners on one Zmotion controller: logical axes -> physical axis numbers.

The controller in the field drives **two** tuners at once.  Each tuner has the
same logical axes (``X``, ``Z1``, ``Z2``, optionally ``Z3``) but occupies a
different block of the controller's physical axis numbers::

    left  tuner : X -> 0, Z1 -> 1, Z2 -> 2
    right tuner : X -> 3, Z1 -> 4, Z2 -> 5

Before this module existed the software used the *logical index* as the
controller axis number.  That made the right tuner unreachable and, worse, a
four-axis model on the left tuner would have commanded axis 3 -- the right
tuner's X.  Every backend call now goes through :meth:`TunerUnit.physical_axis`.

A :class:`TunerUnit` also carries the two wiring offsets that are not axis
parameters on the controller itself:

* ``datum_in_base`` -- first origin-input channel (``IN16..IN18`` on the left
  tuner).  These are stored per axis in :class:`~tuner.axis_params.AxisParams`
  so the operator can still edit them individually.
* ``op_base`` -- first digital output used by the desktop app's OP-pulse
  homing.  Unlike ``datum_in`` there is no per-axis setting for it, so it lives
  here.

Both are **defaults**.  Confirm them against the actual wiring before the first
homing run; a wrong OP channel would pulse the wrong drive.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from .axis_params import (
    AxisParams, CONTROLLER_AXES, TUNER_MODELS, default_axis_params, tuner_axes,
)

__all__ = [
    'TunerUnit', 'DEFAULT_UNIT_KEY', 'MAX_CONTROLLER_AXES',
    'DEFAULT_TUNER_UNITS', 'default_units', 'normalise_units',
    'validate_units', 'unit_axis_params', 'axis_map_text',
]

#: Unit selected when nothing else is configured.
DEFAULT_UNIT_KEY = 'left'

#: Highest physical axis number this controller is expected to expose.
#: Six axes (0..5) are wired; the value is a guard, not a hardware query.
MAX_CONTROLLER_AXES = 6

#: Number of digital outputs expected on the controller.
MAX_CONTROLLER_OPS = 12


@dataclass(frozen=True)
class TunerUnit:
    """One tuner's slice of the controller.

    ``axis_base`` is the controller axis number of this tuner's logical X.
    ``axis_count`` selects the 3-axis (X/Z1/Z2) or 4-axis (X/Z1/Z2/Z3) model.
    ``op_base`` / ``datum_in_base`` are the first digital output / origin input
    used by this tuner; the remaining axes use consecutive channels.
    """

    key: str = DEFAULT_UNIT_KEY
    label: str = '左 Tuner'
    axis_base: int = 0
    axis_count: int = 3
    op_base: int = 0
    datum_in_base: int = 16

    def __post_init__(self) -> None:
        if not str(self.key).strip():
            raise ValueError('tuner unit key must not be empty')
        if not str(self.label).strip():
            raise ValueError('tuner unit label must not be empty')
        if not isinstance(self.axis_base, int) or isinstance(self.axis_base, bool):
            raise ValueError('axis_base must be an integer')
        if not isinstance(self.axis_count, int) or isinstance(self.axis_count, bool):
            raise ValueError('axis_count must be an integer')
        if self.axis_base < 0:
            raise ValueError('axis_base must be non-negative')
        if self.axis_count not in TUNER_MODELS:
            raise ValueError(
                f'unsupported axis count {self.axis_count}; expected one of '
                f'{sorted(TUNER_MODELS)}')
        if self.axis_base + self.axis_count > MAX_CONTROLLER_AXES:
            raise ValueError(
                f'{self.label} would use axes {self.axis_base}..'
                f'{self.axis_base + self.axis_count - 1}, beyond the '
                f'{MAX_CONTROLLER_AXES} axes of this controller')
        if not isinstance(self.op_base, int) or self.op_base < 0:
            raise ValueError('op_base must be a non-negative integer')
        if self.op_base + self.axis_count > MAX_CONTROLLER_OPS:
            raise ValueError('op_base leaves too few digital outputs for this tuner')
        if not isinstance(self.datum_in_base, int) or self.datum_in_base < -1:
            raise ValueError('datum_in_base must be -1 (disabled) or a valid input')

    # -- axis mapping ----------------------------------------------------
    @property
    def axes(self) -> tuple[str, ...]:
        """Logical axis names of this tuner model."""
        return tuner_axes(self.axis_count)

    @property
    def physical_axes(self) -> tuple[int, ...]:
        """Controller axis numbers occupied by this tuner, in logical order."""
        return tuple(range(self.axis_base, self.axis_base + self.axis_count))

    def physical_axis(self, name: str) -> int:
        """Controller axis number for a logical axis name."""
        try:
            index = self.axes.index(name)
        except ValueError as exc:
            raise ValueError(
                f'unknown axis {name!r} for {self.label}; expected one of {self.axes}'
            ) from exc
        return self.axis_base + index

    def axis_map(self) -> dict[str, int]:
        """``{logical name: controller axis number}`` in logical order."""
        return {name: self.axis_base + index
                for index, name in enumerate(self.axes)}

    def output(self, name: str) -> int:
        """Digital output used for the OP homing pulse of ``name``."""
        try:
            index = self.axes.index(name)
        except ValueError as exc:
            raise ValueError(f'unknown axis {name!r}') from exc
        return self.op_base + index

    def datum_in(self, name: str) -> int:
        """Default origin-input channel of ``name``."""
        try:
            index = self.axes.index(name)
        except ValueError as exc:
            raise ValueError(f'unknown axis {name!r}') from exc
        return -1 if self.datum_in_base < 0 else self.datum_in_base + index

    def default_params(self) -> dict[str, AxisParams]:
        """Fresh per-axis parameters, with this tuner's origin inputs.

        The rest (UNITS, speeds, limits, homing mode) comes from
        :func:`~tuner.axis_params.default_axis_params`, i.e. the values read
        back from the commissioned controller.
        """
        params = default_axis_params(self.axis_count)
        return {name: replace(value, datum_in=self.datum_in(name))
                for name, value in params.items()}

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            'key': self.key,
            'label': self.label,
            'axis_base': self.axis_base,
            'axis_count': self.axis_count,
            'op_base': self.op_base,
            'datum_in_base': self.datum_in_base,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'TunerUnit':
        if not isinstance(data, Mapping):
            raise ValueError('tuner unit must be a mapping')
        known = {'key', 'label', 'axis_base', 'axis_count', 'op_base', 'datum_in_base'}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f'unknown tuner unit field(s): {sorted(unknown)}')
        return cls(**{k: v for k, v in data.items() if k in known})

    def describe(self) -> str:
        """One-line wiring summary for combo boxes and status bars."""
        return f'{self.label} · 轴 {" / ".join(str(a) for a in self.physical_axes)}'


#: Factory configuration for this machine.
DEFAULT_TUNER_UNITS: tuple[TunerUnit, ...] = (
    TunerUnit(key='left', label='左 Tuner', axis_base=0, axis_count=3,
              op_base=0, datum_in_base=16),
    TunerUnit(key='right', label='右 Tuner', axis_base=3, axis_count=3,
              op_base=3, datum_in_base=19),
)


def default_units() -> dict[str, TunerUnit]:
    """A fresh copy of the factory tuner configuration."""
    return {unit.key: replace(unit) for unit in DEFAULT_TUNER_UNITS}


def normalise_units(raw: Any) -> dict[str, TunerUnit]:
    """Build a unit mapping from stored JSON (or an iterable of units).

    A missing configuration yields the factory pair.  An explicitly supplied
    mapping is used as-is, so a single-tuner installation stays supported.
    """
    if raw is None:
        return default_units()
    if isinstance(raw, Mapping):
        values: Iterable[Any] = raw.values()
    else:
        values = raw
    units: dict[str, TunerUnit] = {}
    for item in values:
        unit = item if isinstance(item, TunerUnit) else TunerUnit.from_dict(item)
        if unit.key in units:
            raise ValueError(f'duplicate tuner unit key: {unit.key!r}')
        units[unit.key] = unit
    if not units:
        raise ValueError('at least one tuner unit is required')
    return units


def validate_units(units: Mapping[str, TunerUnit]) -> None:
    """Reject overlapping axis blocks or duplicate OP / origin channels.

    Two tuners commanding the same controller axis is the failure mode this
    whole module exists to prevent, so it is checked whenever the operator
    changes a model, an axis base or the wiring offsets.
    """
    if not units:
        raise ValueError('at least one tuner unit is required')
    axis_owner: dict[int, str] = {}
    for unit in units.values():
        for number in unit.physical_axes:
            if number in axis_owner:
                raise ValueError(
                    f'{unit.label} and {axis_owner[number]} both use controller '
                    f'axis {number}; the tuners must not overlap')
            axis_owner[number] = unit.label
    for key_a, unit_a in units.items():
        for key_b, unit_b in units.items():
            if key_a >= key_b:
                continue
            if (set(range(unit_a.op_base, unit_a.op_base + unit_a.axis_count))
                    & set(range(unit_b.op_base, unit_b.op_base + unit_b.axis_count))):
                raise ValueError(
                    f'{unit_a.label} and {unit_b.label} share a homing output')
            if unit_a.datum_in_base >= 0 and unit_b.datum_in_base >= 0:
                if (set(range(unit_a.datum_in_base,
                              unit_a.datum_in_base + unit_a.axis_count))
                        & set(range(unit_b.datum_in_base,
                                    unit_b.datum_in_base + unit_b.axis_count))):
                    raise ValueError(
                        f'{unit_a.label} and {unit_b.label} share an origin input')


def unit_axis_params(unit: TunerUnit) -> dict[str, AxisParams]:
    """Default per-axis parameters for ``unit`` (thin wrapper, kept for callers)."""
    return unit.default_params()


def axis_map_text(unit: TunerUnit) -> str:
    """``X→0, Z1→1, Z2→2`` — the mapping shown next to the tuner selector."""
    return ', '.join(f'{name}→{number}'
                     for name, number in unit.axis_map().items())

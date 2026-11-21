"""Persistent TunerLab settings: controller link plus per-axis motion parameters.

The settings file is a small JSON document written next to the application data. It is
the single place where the operator's choices survive a restart, so it is written
atomically (temp file plus replace) and validated on load.

Layout::

    {
      "schema_version": 3,
      "connection": {"ip": "192.168.0.11", "connect_on_start": false},
      "mode": "real",
      "axis_count": 4,
      "axes": {"X": {...}, "Z1": {...}, "Z2": {...}, "Z3": {...}}
    }

``axis_count`` selects the tuner model (3 = X/Z1/Z2, 4 = X/Z1/Z2/Z3). The ``axes``
map may keep parameters for axes that are not part of the active model, so
switching back and forth does not lose a tuned axis.

``mode`` selects the controller backend: ``real`` drives the Zmotion controller,
``virtual`` drives the simulated one so the console can be rehearsed without
hardware. Schema 1 and 2 had no such field and always meant ``real``.

Schema 4 adds two tuners on one controller. ``active_unit`` picks which one the
console commands, ``units`` holds their physical axis blocks and wiring offsets,
and ``axes_by_unit`` keeps an independent set of axis parameters per tuner so
tuning the left tuner never changes the right one. Files written by schema 1–3
are migrated on load: their ``axes`` become the left tuner's parameters and the
origin-input base is inferred from the stored ``X`` channel.

Schema 5 adds ``vna_dialect``: the SCPI command set pinned by the SCPI debug
tool's probe for the real analyzer (e.g. the workshop's GPIB N5244/N5224), so a
verified dialect survives a restart instead of being re-negotiated with a
handful of ``-113`` probes on every connect.

Schema 6 makes the *device combination* an explicit part of the tuner model.
Both models are supported at once, but they are mutually exclusive installations:
3 axes = two 3-axis tuners (left 0/1/2, right 3/4/5); 4 axes = one 4-axis tuner
(X=0, Z1=1, Z2=2, Z3=3).  A four-axis tuner needs physical axis 3 for Z3, which
is the second tuner's X, so the right unit must not exist in that combination.
``units_by_model`` keeps both combinations' wiring; switching a model swaps the
whole ``units`` mapping while ``axes_by_unit`` keeps every unit's tuned
parameters (the keys never collide: ``left``/``right`` vs ``tuner_1``).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict, replace
from pathlib import Path
from typing import Any, Mapping

from .axis_params import (
    AxisParams, CONTROLLER_AXES, DEFAULT_ACTIVE_AXIS_COUNT,
    DEFAULT_AXIS_COUNT, TUNER_MODELS, default_axis_params, tuner_axes,
)
from .tuner_units import (
    DEFAULT_UNIT_BY_MODEL, DEFAULT_UNIT_KEY, TunerUnit, default_units,
    normalise_units, units_for_model, validate_units,
)

__all__ = ['ConnectionSettings', 'TunerSettings', 'SettingsStore',
           'default_settings_path', 'TUNER_MODELS', 'DEFAULT_AXIS_COUNT',
           'DEFAULT_ACTIVE_AXIS_COUNT', 'SCHEMA_VERSION']

#: Bumped when the on-disk layout changes incompatibly.
SCHEMA_VERSION = 6

#: Factory IP of a Zmotion controller.
DEFAULT_IP = '192.168.0.11'


def default_settings_path() -> Path:
    """Default location of the settings file.

    Uses ``%APPDATA%`` on Windows so the file survives project moves and is per-user.
    """
    base = os.environ.get('APPDATA')
    if base:
        return Path(base) / 'TunerLab' / 'settings.json'
    return Path.home() / '.tunerlab' / 'settings.json'


@dataclass
class ConnectionSettings:
    """How to reach the motion controller."""

    ip: str = DEFAULT_IP
    #: Open the link automatically when the hardware page is shown.
    connect_on_start: bool = False

    def __post_init__(self) -> None:
        self.ip = str(self.ip).strip()
        if not self.ip:
            raise ValueError('controller IP address must not be empty')
        # Reject anything that is not a dotted-quad so a typo is caught before the
        # operator waits for a connection timeout.
        parts = self.ip.split('.')
        if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            raise ValueError(f'invalid IPv4 address: {self.ip!r}')

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'ConnectionSettings':
        return cls(ip=data.get('ip', DEFAULT_IP),
                   connect_on_start=bool(data.get('connect_on_start', False)))


@dataclass
class TunerSettings:
    """Complete persisted configuration of the motion subsystem."""

    connection: ConnectionSettings = field(default_factory=ConnectionSettings)
    #: Controller backend: 'real' (the Zmotion controller) or 'virtual'.
    mode: str = 'real'
    #: Tuner model: 3 axes (X/Z1/Z2) or 4 axes (X/Z1/Z2/Z3).  The model also
    #: selects the device combination, see :meth:`set_model`.
    axis_count: int = DEFAULT_ACTIVE_AXIS_COUNT
    #: Per-axis parameters. Always holds an entry for every axis of the active
    #: model; axes of the other model may be present as well so that switching
    #: back and forth keeps a tuned axis.
    axes: dict[str, AxisParams] = field(default_factory=default_axis_params)

    # -- schema 4: two tuners on one controller ---------------------------
    #: Key of the tuner currently selected in the UI / console.
    active_unit: str = DEFAULT_UNIT_KEY
    #: Physical axis block and wiring of each tuner.
    units: dict[str, TunerUnit] = field(default_factory=default_units)
    #: Independent per-axis parameters per tuner. ``axes`` is the active
    #: tuner's view of this mapping; :meth:`TunerSettings.unit_axes` is the
    #: authoritative accessor.
    axes_by_unit: dict[str, dict[str, AxisParams]] = field(default_factory=dict)

    # -- schema 5: pinned SCPI dialect of the real VNA ---------------------
    #: Command set discovered by the SCPI debug tool's probe and pinned so
    #: later VNA connections skip the dialect negotiation.  Keys are a
    #: subset of ``vna_drivers.DIALECT_FIELDS``; unknown keys are dropped on
    #: load, so a stale probe report cannot inject arbitrary settings.
    vna_dialect: dict[str, str] = field(default_factory=dict)

    # -- schema 6: one device combination per tuner model ------------------
    #: Wiring of *both* combinations, so switching 3 <-> 4 axes never loses a
    #: hand-adjusted ``axis_base`` / ``op_base`` / ``datum_in_base``.
    units_by_model: dict[int, dict[str, TunerUnit]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.mode = str(self.mode).strip().lower()
        if self.mode not in ('real', 'virtual'):
            raise ValueError(
                f'unsupported controller mode {self.mode!r}; '
                "expected 'real' or 'virtual'")
        self.axis_count = int(self.axis_count)
        if self.axis_count not in TUNER_MODELS:
            raise ValueError(
                f'unsupported axis count {self.axis_count}; expected one of '
                f'{sorted(TUNER_MODELS)}')
        self._check_axes(self.axes, 'axis settings')

        # The model determines the device combination.  A pre-schema-6 file may
        # carry e.g. ``axis_count=4`` over the two-tuner 3-axis wiring (or the
        # other way round); such a mapping would command overlapping physical
        # axes, so it is replaced by the combination that belongs to the model.
        # An explicitly supplied mapping whose own axis counts match the model
        # is honoured (hand-edited single-tuner installations stay supported).
        self.units_by_model = _normalise_units_by_model(self.units_by_model)
        supplied = normalise_units(self.units)
        expected = self.units_by_model[self.axis_count]
        if all(unit.axis_count == self.axis_count
               for unit in supplied.values()):
            # Keeps a hand-edited mapping with different keys too, as long as
            # every unit agrees with the active model.
            self.units = supplied
            self.units_by_model[self.axis_count] = {
                key: replace(unit) for key, unit in supplied.items()}
        else:
            self.units = {key: replace(unit) for key, unit in expected.items()}
        validate_units(self.units)

        default_key = DEFAULT_UNIT_BY_MODEL.get(self.axis_count)
        if self.active_unit not in self.units:
            self.active_unit = (default_key if default_key in self.units
                                else next(iter(self.units)))

        stored = self.axes_by_unit or {}
        # Keep parameters of units that are not part of the *current*
        # combination: switching back must not discard a tuned axis.
        resolved: dict[str, dict[str, AxisParams]] = {
            key: dict(params) for key, params in stored.items() if params}
        for key, unit in self.units.items():
            raw = resolved.get(key) or stored.get(key)
            if raw:
                self._check_axes(raw, f'axis settings of {unit.label}')
                params = dict(raw)
            else:
                params = dict(self.axes) if key == self.active_unit else {}
            for name, default in unit.default_params().items():
                params.setdefault(name, default)
            resolved[key] = params
        self.axes_by_unit = resolved
        # ``axes`` is the live view of the active unit.  It keeps the four-axis
        # superset so old libraries and parameter pages stay readable, but must
        # contain every axis the active model actually commands.
        for name, default in self.units[self.active_unit].default_params().items():
            self.axes.setdefault(name, default)
        self.axes_by_unit[self.active_unit] = dict(self.axes)

    @staticmethod
    def _check_axes(axes: dict[str, AxisParams], where: str) -> None:
        active = tuner_axes(int(DEFAULT_AXIS_COUNT))
        missing = [name for name in active if name not in axes]
        if missing:
            # Only the four-axis superset is mandatory for a stored mapping;
            # a three-axis tuner legitimately has no Z3 entry of its own, but
            # the shared settings file always kept all four.
            if missing != ['Z3']:
                raise ValueError(f'missing {where}: {missing}')
        extra = [name for name in axes if name not in CONTROLLER_AXES]
        if extra:
            raise ValueError(f'unknown {where}: {extra}')
        for name, params in axes.items():
            if params.name != name:
                raise ValueError(
                    f'axis key {name!r} does not match parameter name {params.name!r}')

    @property
    def active_axes(self) -> tuple[str, ...]:
        """Axis names of the active tuner model."""
        return tuner_axes(self.axis_count)

    @property
    def probe_count(self) -> int:
        """Number of probe axes, i.e. the axes besides the shared X axis."""
        return self.axis_count - 1

    # -- tuner units ------------------------------------------------------
    @property
    def unit(self) -> TunerUnit:
        """The :class:`~tuner.tuner_units.TunerUnit` currently selected."""
        return self.units[self.active_unit]

    def unit_axes(self, key: str | None = None) -> dict[str, AxisParams]:
        """Per-axis parameters of one tuner (the active one by default)."""
        return self.axes_by_unit[key or self.active_unit]

    def set_unit(self, key: str) -> None:
        """Select a tuner and expose its axis count / parameters on ``self``.

        The current tuner's parameters are parked in :attr:`axes_by_unit`
        first, so switching back and forth preserves per-tuner tuning.
        """
        if key not in self.units:
            raise ValueError(f'unknown tuner unit {key!r}; '
                             f'expected one of {sorted(self.units)}')
        self.axes_by_unit[self.active_unit] = dict(self.axes)
        self.active_unit = key
        unit = self.units[key]
        self.axis_count = unit.axis_count
        params = dict(self.axes_by_unit[key])
        for name, default in unit.default_params().items():
            params.setdefault(name, default)
        self.axes_by_unit[key] = params
        self.axes = dict(params)

    def set_model(self, count: int) -> None:
        """Select a tuner model and swap to its device combination.

        3 axes uses two 3-axis tuners (left 0/1/2, right 3/4/5).  4 axes uses
        one 4-axis tuner with ``X=0, Z1=1, Z2=2, Z3=3``; the right tuner must
        disappear because its X would otherwise share physical axis 3 with Z3.

        Both combinations' wiring and every unit's tuned parameters are kept in
        ``units_by_model`` / ``axes_by_unit``, so switching back and forth does
        not lose anything.  The swap is rejected if the target combination
        would command overlapping axes.
        """
        count = int(count)
        if count not in TUNER_MODELS:
            raise ValueError(
                f'unsupported axis count {count}; expected one of '
                f'{sorted(TUNER_MODELS)}')
        # Park the current combination before replacing it.
        self.units_by_model = _normalise_units_by_model(self.units_by_model)
        self.units_by_model[self.axis_count] = {
            key: replace(unit) for key, unit in self.units.items()}
        self.axes_by_unit[self.active_unit] = dict(self.axes)

        target = self.units_by_model.get(count) or units_for_model(count)
        target = {
            key: replace(unit, axis_count=count)
            for key, unit in target.items()
        }
        validate_units(target)
        self.units = target
        self.units_by_model[count] = {
            key: replace(unit) for key, unit in target.items()}
        self.axis_count = count
        default_key = DEFAULT_UNIT_BY_MODEL.get(count)
        self.active_unit = (default_key if default_key in target
                            else next(iter(target)))

        params = dict(self.axes_by_unit.get(self.active_unit) or {})
        for name, default in target[self.active_unit].default_params().items():
            params.setdefault(name, default)
        self.axes = params
        self.axes_by_unit[self.active_unit] = dict(params)

    def set_unit_axis_count(self, key: str, count: int) -> None:
        """Backwards-compatible alias for :meth:`set_model`.

        The old implementation only changed one unit's ``axis_count``, which
        silently created an overlapping axis block for the four-axis model.
        The model now owns the whole combination, so ``key`` is informational
        only; every caller reaches the same consistent state.
        """
        self.set_model(count)

    def overlapping_units(self) -> dict[str, list[str]]:
        """Pairs of tuners whose axis blocks intersect.

        Empty for a normal installation.  A non-empty result means the two
        tuners would command the same controller axis, which the operator has
        to resolve in the wiring.
        """
        clashes: dict[str, list[str]] = {}
        items = list(self.units.items())
        for index, (key_a, unit_a) in enumerate(items):
            for key_b, unit_b in items[index + 1:]:
                shared = set(unit_a.physical_axes) & set(unit_b.physical_axes)
                if shared:
                    clashes.setdefault(key_a, []).append(key_b)
                    clashes.setdefault(key_b, []).append(key_a)
        return clashes

    def update_unit(self, unit: TunerUnit) -> None:
        """Replace one tuner's wiring, rejecting overlapping axis blocks."""
        if unit.key not in self.units:
            raise ValueError(f'unknown tuner unit {unit.key!r}')
        candidate = dict(self.units)
        candidate[unit.key] = unit
        validate_units(candidate)
        self.units = candidate

    def to_dict(self) -> dict[str, Any]:
        # ``axes`` is the live view of the active tuner; always park it in the
        # per-tuner map before writing, so a direct field edit (the way the
        # axis-parameter page works) is persisted too.
        per_unit = {key: {name: params.to_dict() for name, params in value.items()}
                    for key, value in self.axes_by_unit.items()}
        per_unit[self.active_unit] = {
            name: params.to_dict() for name, params in self.axes.items()}
        return {
            'schema_version': SCHEMA_VERSION,
            'connection': self.connection.to_dict(),
            'mode': self.mode,
            'axis_count': self.axis_count,
            'axes': per_unit[self.active_unit],
            'active_unit': self.active_unit,
            'units': {key: unit.to_dict() for key, unit in self.units.items()},
            'axes_by_unit': per_unit,
            'units_by_model': {
                str(count): {key: unit.to_dict() for key, unit in mapping.items()}
                for count, mapping in self.units_by_model.items()
            },
            'vna_dialect': dict(self.vna_dialect),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'TunerSettings':
        """Rebuild settings from a stored mapping, raising on anything malformed."""
        version = int(data.get('schema_version', 1))
        if version > SCHEMA_VERSION:
            raise ValueError(
                f'settings file uses schema version {version}, but this build reads {SCHEMA_VERSION}')
        connection = ConnectionSettings.from_dict(data.get('connection', {}))
        mode = str(data.get('mode', 'real')).strip().lower()
        stored = data.get('axes', {}) or {}
        # Schema 1 had no axis_count; it always described the four-axis model.
        axis_count = int(data.get('axis_count', DEFAULT_AXIS_COUNT))

        raw_units = data.get('units')
        units = (normalise_units(raw_units) if raw_units
                 else _migrate_default_units(stored))
        active = str(data.get('active_unit') or DEFAULT_UNIT_KEY)
        if active not in units:
            active = DEFAULT_UNIT_KEY if DEFAULT_UNIT_KEY in units else next(iter(units))

        raw_by_unit = data.get('axes_by_unit') or {}
        per_unit: dict[str, dict[str, AxisParams]] = {}
        for key, unit in units.items():
            raw = raw_by_unit.get(key)
            params: dict[str, AxisParams] = {}
            if isinstance(raw, Mapping):
                for name, value in raw.items():
                    if name in CONTROLLER_AXES:
                        params[name] = AxisParams.from_dict(value)
            for name, default in unit.default_params().items():
                params.setdefault(name, default)
            per_unit[key] = params

        if version < 4 or not raw_by_unit:
            # Pre-schema-4 files describe a single tuner. Their top-level
            # ``axes`` belong to the active (left) tuner and must be preserved
            # byte-for-byte: on the commissioned machine these are measured
            # UNITS / limits / origin inputs, not the built-in defaults.
            params = {}
            defaults = units[active].default_params()
            for name in CONTROLLER_AXES:
                if name in stored:
                    params[name] = AxisParams.from_dict(stored[name])
                elif name in defaults:
                    params[name] = defaults[name]
            per_unit[active] = params

        return cls(connection=connection, mode=mode, axis_count=axis_count,
                   axes=dict(per_unit[active]), active_unit=active,
                   units=units, axes_by_unit=per_unit,
                   units_by_model=_normalise_units_by_model(data.get('units_by_model')),
                   vna_dialect=_clean_dialect(data.get('vna_dialect')))


def _normalise_units_by_model(raw: Any) -> dict[int, dict[str, TunerUnit]]:
    """Rebuild the per-model wiring map from stored JSON.

    JSON object keys are strings, so ``"3"`` / ``"4"`` are converted back to
    integers.  Every supported model gets an entry; a model with no stored
    mapping falls back to its factory combination.  A malformed stored entry
    is ignored rather than aborting the whole load -- a hand-edited file must
    still open, with that one combination reset to factory wiring.
    """
    stored = raw if isinstance(raw, Mapping) else {}
    result: dict[int, dict[str, TunerUnit]] = {}
    for count in TUNER_MODELS:
        source = stored.get(count)
        if source is None:
            source = stored.get(str(count))
        try:
            if source:
                result[count] = normalise_units(source)
            else:
                result[count] = units_for_model(count)
        except ValueError:
            result[count] = units_for_model(count)
    return result


def _clean_dialect(raw: Any) -> dict[str, str]:
    """Sanitise a stored ``vna_dialect`` mapping.

    Only the five driver-consumed keys survive, and only with non-empty
    string values -- everything a stale or hand-edited file adds is dropped
    before it can reach the VNA driver.  ``DIALECT_FIELDS`` is imported
    lazily: the settings module must stay importable without numpy.
    """
    if not isinstance(raw, Mapping):
        return {}
    from .vna_drivers import DIALECT_FIELDS  # noqa: PLC0415 - avoid import cycle
    return {str(key): str(value).strip() for key, value in raw.items()
            if key in DIALECT_FIELDS and str(value or '').strip()}


def _migrate_default_units(stored_axes: Any) -> dict[str, TunerUnit]:
    """Factory pair for a settings file that predates the two-tuner schema.

    The left tuner keeps the origin-input channels the operator already used
    (e.g. ``IN28/29/30`` on this machine); the right tuner is placed on the
    next three channels. Those are *inferred* defaults -- the operator must
    confirm them against the real wiring in the tuner selector.
    """
    units = default_units()
    try:
        datum = stored_axes['X']['datum_in'] if isinstance(stored_axes, Mapping) else None
    except (KeyError, TypeError):
        datum = None
    if isinstance(datum, (int, float)) and not isinstance(datum, bool) and int(datum) >= 0:
        base = int(datum)
        left = replace(units['left'], datum_in_base=base)
        units = {
            'left': left,
            'right': replace(units['right'], datum_in_base=base + left.axis_count),
        }
    return units


class SettingsStore:
    """Load and save :class:`TunerSettings` as JSON.

    A missing file yields the built-in defaults instead of an error, so the application
    starts cleanly on a fresh machine. A corrupt file is reported rather than silently
    replaced, because overwriting it would destroy the operator's configuration.
    """

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path is not None else default_settings_path()

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> TunerSettings:
        """Read the settings file, or return defaults when it does not exist."""
        if not self.path.is_file():
            return TunerSettings()
        try:
            raw = self.path.read_text(encoding='utf-8')
        except OSError as exc:
            raise ValueError(f'cannot read settings file {self.path}: {exc}') from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f'settings file {self.path} is not valid JSON: {exc}') from exc
        if not isinstance(data, dict):
            raise ValueError(f'settings file {self.path} must contain a JSON object')
        return TunerSettings.from_dict(data)

    def save(self, settings: TunerSettings) -> Path:
        """Write settings atomically and return the file path."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(settings.to_dict(), indent=2, ensure_ascii=False)
        temp = self.path.with_suffix(self.path.suffix + '.tmp')
        temp.write_text(payload + '\n', encoding='utf-8')
        os.replace(temp, self.path)
        return self.path

    def export(self, path: str | os.PathLike, settings: TunerSettings | None = None) -> Path:
        """Write settings to an arbitrary file for backup or transfer."""
        target = Path(path)
        data = settings if settings is not None else self.load()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(data.to_dict(), indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        return target

    @staticmethod
    def import_from(path: str | os.PathLike) -> TunerSettings:
        """Read settings from an arbitrary file."""
        source = Path(path)
        try:
            data = json.loads(source.read_text(encoding='utf-8'))
        except OSError as exc:
            raise ValueError(f'cannot read {source}: {exc}') from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f'{source} is not valid JSON: {exc}') from exc
        if not isinstance(data, dict):
            raise ValueError(f'{source} must contain a JSON object')
        return TunerSettings.from_dict(data)

"""Schema 6 installation settings with fixed layouts and independent parameters.

Legacy schemas are backed up and retained for inspection; ambiguous installations
require explicit confirmation before a real connection. Runtime positions, homing
and readback verification are never persisted as hardware truth.
"""
from __future__ import annotations

import json
import os
import copy
import uuid
from dataclasses import dataclass, field, asdict, replace
from pathlib import Path
from typing import Any, Mapping

from .axis_params import (
    AxisParams, CONTROLLER_AXES, DEFAULT_AXIS_COUNT, TUNER_MODELS,
    default_axis_params, tuner_axes,
)
from .tuner_units import (
    DEFAULT_UNIT_KEY, TunerUnit, default_units, normalise_units, validate_units,
    layout_units, validate_layout,
)

__all__ = ['ConnectionSettings', 'TunerSettings', 'SettingsStore',
           'default_settings_path', 'TUNER_MODELS', 'DEFAULT_AXIS_COUNT',
           'SCHEMA_VERSION']

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


class TunerSettings:
    """Schema 6: fixed layouts and independent per-layout/per-unit parameters.

    Legacy ``axes``/``axis_count`` are views, never separate stored authorities.
    """

    def __init__(self, connection=None, mode='real', axis_count=None, axes=None,
                 active_unit=None, units=None, axes_by_unit=None, vna_dialect=None,
                 layout_id=None, profiles=None, active_by_layout=None,
                 layout_confirmed=False, controller_id=None, legacy_config=None,
                 configuration_error='', extra=None):
        self.connection = connection or ConnectionSettings()
        self.mode = str(mode).lower()
        if self.mode not in ('real', 'virtual'):
            raise ValueError('unknown controller mode')
        if axis_count is not None and int(axis_count) not in TUNER_MODELS:
            raise ValueError('unsupported axis count')
        self.controller_id = controller_id or str(uuid.uuid4())
        self.layout_id = layout_id or (f'single_{axis_count}' if axis_count else 'dual_3')
        if not isinstance(layout_confirmed, bool):
            raise ValueError('layout_confirmed must be a boolean')
        self.layout_confirmed = layout_confirmed
        self.configuration_error = configuration_error
        self.legacy_config = copy.deepcopy(legacy_config)
        self.extra = copy.deepcopy(extra or {})
        self.vna_dialect = _clean_dialect(vna_dialect)
        self.profiles = {}
        self.active_by_layout = dict(active_by_layout or {})
        for layout in ('single_3', 'dual_3', 'single_4'):
            spec = layout_units(layout)
            stored = (profiles or {}).get(layout, {})
            resolved = {}
            for key, unit in spec.items():
                params = copy.deepcopy(stored.get(key, {}))
                for name, value in unit.default_params().items():
                    params.setdefault(name, value)
                self._check_axes(params, unit.axes)
                resolved[key] = params
            self.profiles[layout] = resolved
            if self.active_by_layout.get(layout) not in spec:
                self.active_by_layout[layout] = next(iter(spec))
        spec = layout_units(self.layout_id)
        chosen = active_unit if active_unit in spec else self.active_by_layout[self.layout_id]
        self.active_by_layout[self.layout_id] = chosen
        self._units = spec
        if units is not None:
            validate_layout(self.layout_id, normalise_units(units))
        if axes_by_unit:
            for key, params in axes_by_unit.items():
                if key not in spec:
                    raise ValueError('unknown tuner parameter owner')
                self.profiles[self.layout_id][key].update(copy.deepcopy(params))
        if axes:
            self.profiles[self.layout_id][chosen].update(copy.deepcopy(axes))
        self.validate()

    @staticmethod
    def _check_axes(params, required):
        if any(name not in CONTROLLER_AXES or value.name != name
               for name, value in params.items()):
            raise ValueError('unknown or mismatched axis parameter name')
        if set(params) != set(required):
            raise ValueError('axis parameters must match the selected tuner exactly')

    def validate(self):
        validate_layout(self.layout_id, self._units)
        for key, unit in self.units.items():
            self._check_axes(self.unit_axes(key), unit.axes)

    @property
    def units(self):
        return self._units

    @property
    def active_unit(self):
        return self.active_by_layout[self.layout_id]

    @active_unit.setter
    def active_unit(self, key):
        if key not in self.units:
            raise ValueError('unknown tuner unit')
        self.active_by_layout[self.layout_id] = key

    @property
    def unit(self):
        return self.units[self.active_unit]

    @property
    def axis_count(self):
        return self.unit.axis_count

    @property
    def active_axes(self):
        return self.unit.axes

    @property
    def probe_count(self):
        return self.axis_count - 1

    @property
    def axes_by_unit(self):
        return self.profiles[self.layout_id]

    @property
    def axes(self):
        return self.unit_axes()

    @axes.setter
    def axes(self, params):
        self._check_axes(params, self.active_axes)
        self.profiles[self.layout_id][self.active_unit] = dict(params)

    def unit_axes(self, key=None):
        return self.profiles[self.layout_id][key or self.active_unit]

    def set_unit(self, key):
        self.active_unit = key

    def set_layout(self, layout):
        spec = layout_units(layout)
        self.layout_id = layout
        self._units = spec
        self.layout_confirmed = True
        self.configuration_error = ''
        self.validate()

    def set_unit_axis_count(self, key, count):
        if key not in self.units:
            raise ValueError('unknown tuner unit')
        if self.layout_id == 'dual_3':
            if int(count) != 3:
                raise ValueError('双台组合仅支持两台三轴 Tuner；请断开后选择单台四轴组合')
        else:
            self.set_layout(f'single_{int(count)}')

    def overlapping_units(self):
        self.validate()
        return {}

    def update_unit(self, unit):
        candidate = dict(self.units)
        candidate[unit.key] = unit
        validate_layout(self.layout_id, candidate)
        self._units = candidate

    def to_dict(self):
        self.validate()
        result = copy.deepcopy(self.extra)
        result.update(schema_version=SCHEMA_VERSION, controller_id=self.controller_id,
                      connection=self.connection.to_dict(), mode=self.mode,
                      layout_id=self.layout_id, layout_confirmed=self.layout_confirmed,
                      active_by_layout=dict(self.active_by_layout),
                      profiles={layout: {unit: {name: value.to_dict() for name, value in axes.items()}
                                         for unit, axes in tuners.items()}
                                for layout, tuners in self.profiles.items()},
                      vna_dialect=dict(self.vna_dialect),
                      legacy_config=copy.deepcopy(self.legacy_config),
                      configuration_error=self.configuration_error)
        return result

    @classmethod
    def from_dict(cls, data):
        version = int(data.get('schema_version', 1))
        if version > SCHEMA_VERSION:
            raise ValueError('settings file uses a newer schema version')
        common = dict(connection=ConnectionSettings.from_dict(data.get('connection', {})),
                      mode=data.get('mode', 'real'), vna_dialect=data.get('vna_dialect'),
                      controller_id=data.get('controller_id'))
        if version == 6:
            raw_profiles = data.get('profiles', {})
            if any(k not in ('single_3', 'dual_3', 'single_4') for k in raw_profiles):
                raise ValueError('unknown layout profile')
            for layout, unit in data.get('active_by_layout', {}).items():
                if unit not in layout_units(layout):
                    raise ValueError('invalid active tuner for layout')
            profiles = {}
            for layout, tuners in raw_profiles.items():
                if set(tuners) - set(layout_units(layout)):
                    raise ValueError('unknown tuner in layout')
                profiles[layout] = {unit: {name: AxisParams.from_dict(value)
                                          for name, value in axes.items()}
                                    for unit, axes in tuners.items()}
            known = {'schema_version', 'controller_id', 'connection', 'mode', 'layout_id',
                     'layout_confirmed', 'active_by_layout', 'profiles', 'vna_dialect',
                     'legacy_config', 'configuration_error'}
            return cls(**common, layout_id=data.get('layout_id', 'dual_3'), profiles=profiles,
                       active_by_layout=data.get('active_by_layout'),
                       layout_confirmed=data.get('layout_confirmed', False),
                       legacy_config=data.get('legacy_config'),
                       configuration_error=data.get('configuration_error', ''),
                       extra={k: v for k, v in data.items() if k not in known})
        count = int(data.get('axis_count', DEFAULT_AXIS_COUNT))
        raw_units = data.get('units')
        layout = f'single_{count}' if not raw_units or len(raw_units) == 1 else 'dual_3'
        problem = ''
        if layout not in ('single_3', 'single_4', 'dual_3'):
            layout, problem = 'single_3', '旧配置轴数不受支持'
        if raw_units:
            try:
                old = normalise_units(raw_units)
                validate_units(old)
                # Old single-tuner identifiers are converted below.
                if len(old) == 2:
                    validate_layout(layout, old)
                elif next(iter(old.values())).axis_map() != next(iter(layout_units(layout).values())).axis_map():
                    raise ValueError('旧配置物理轴号与组合不一致')
            except ValueError as exc:
                problem = str(exc)
        if raw_units and not problem:
            old_active = data.get('active_unit', next(iter(old)))
            if old_active in old and old[old_active].axis_count != count:
                problem = '旧配置轴数与 Tuner 映射矛盾，请选择实际设备组合'
        obj = cls(**common, layout_id=layout, layout_confirmed=False,
                  legacy_config=data, configuration_error=problem)
        per_unit = data.get('axes_by_unit') or {data.get('active_unit', 'left'): data.get('axes', {})}
        for key, raw in per_unit.items():
            target = key if layout == 'dual_3' else 'tuner_1'
            if target not in obj.units:
                continue
            for name, value in raw.items():
                if name in obj.units[target].axes:
                    param = AxisParams.from_dict(value)
                    if 'home_output' not in value:
                        param.home_output = obj.units[target].default_params()[name].home_output
                    obj.profiles[layout][target][name] = param
        if data.get('active_unit') in obj.units:
            obj.active_unit = data['active_unit']
        return obj


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
        if int(data.get('schema_version', 1)) < SCHEMA_VERSION:
            backup = self.path.with_name(self.path.name + '.pre-schema6.bak')
            if not backup.exists():
                with backup.open('x', encoding='utf-8') as handle:
                    handle.write(raw)
        try:
            return TunerSettings.from_dict(data)
        except (TypeError, KeyError, AttributeError) as exc:
            raise ValueError(f'invalid settings structure: {exc}') from exc

    def save(self, settings: TunerSettings) -> Path:
        """Write settings atomically and return the file path."""
        if settings.configuration_error:
            raise ValueError('旧配置存在冲突，先选择并确认设备组合；原文件保留')
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

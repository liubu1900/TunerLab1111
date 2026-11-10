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
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .axis_params import (
    AxisParams, CONTROLLER_AXES, DEFAULT_AXIS_COUNT, TUNER_MODELS,
    default_axis_params, tuner_axes,
)

__all__ = ['ConnectionSettings', 'TunerSettings', 'SettingsStore',
           'default_settings_path', 'TUNER_MODELS', 'DEFAULT_AXIS_COUNT']

#: Bumped when the on-disk layout changes incompatibly.
SCHEMA_VERSION = 3

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
    #: Tuner model: 3 axes (X/Z1/Z2) or 4 axes (X/Z1/Z2/Z3).
    axis_count: int = DEFAULT_AXIS_COUNT
    #: Per-axis parameters. Always holds an entry for every axis of the active
    #: model; axes of the other model may be present as well so that switching
    #: back and forth keeps a tuned axis.
    axes: dict[str, AxisParams] = field(default_factory=default_axis_params)

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
        active = tuner_axes(self.axis_count)
        missing = [name for name in active if name not in self.axes]
        if missing:
            raise ValueError(f'missing axis settings: {missing}')
        extra = [name for name in self.axes if name not in CONTROLLER_AXES]
        if extra:
            raise ValueError(f'unknown axis settings: {extra}')
        for name, params in self.axes.items():
            if params.name != name:
                raise ValueError(f'axis key {name!r} does not match parameter name {params.name!r}')

    @property
    def active_axes(self) -> tuple[str, ...]:
        """Axis names of the active tuner model."""
        return tuner_axes(self.axis_count)

    @property
    def probe_count(self) -> int:
        """Number of probe axes, i.e. the axes besides the shared X axis."""
        return self.axis_count - 1

    def to_dict(self) -> dict[str, Any]:
        return {
            'schema_version': SCHEMA_VERSION,
            'connection': self.connection.to_dict(),
            'mode': self.mode,
            'axis_count': self.axis_count,
            'axes': {name: params.to_dict() for name, params in self.axes.items()},
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
        stored = data.get('axes', {})
        # Schema 1 had no axis_count; it always described the four-axis model.
        axis_count = int(data.get('axis_count', DEFAULT_AXIS_COUNT))
        defaults = default_axis_params(axis_count)
        axes: dict[str, AxisParams] = {}
        for name in CONTROLLER_AXES:
            if name in stored:
                axes[name] = AxisParams.from_dict(stored[name])
            elif name in defaults:
                axes[name] = defaults[name]
        return cls(connection=connection, mode=mode, axis_count=axis_count,
                   axes=axes)


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

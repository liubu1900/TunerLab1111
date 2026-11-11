"""Two tuners on one controller: configuration, mapping and switching.

The machine drives two tuners at once.  The left one owns the controller axes
0/1/2 (X/Z1/Z2), the right one axes 3/4/5.  These tests pin down that
mapping, the per-tuner parameter sets and the switching behaviour.
"""
from __future__ import annotations

import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
import pathlib
import tempfile

import numpy as np
import pytest

from tuner.axis_params import AxisParams, default_axis_params
from tuner.settings import SCHEMA_VERSION, SettingsStore, TunerSettings
from tuner.tuner_units import (
    DEFAULT_TUNER_UNITS, TunerUnit, default_units, normalise_units,
    validate_units,
)
from tuner.ui_hardware import HardwareSession


def _store(tmp_path) -> SettingsStore:
    return SettingsStore(pathlib.Path(tmp_path) / 'settings.json')


# ------------------------------------------------------------------ units
def test_the_factory_pair_matches_the_wiring():
    units = default_units()
    assert units['left'].physical_axes == (0, 1, 2)
    assert units['right'].physical_axes == (3, 4, 5)
    assert units['left'].axis_map() == {'X': 0, 'Z1': 1, 'Z2': 2}
    assert units['right'].axis_map() == {'X': 3, 'Z1': 4, 'Z2': 5}


def test_a_unit_maps_its_logical_axes_to_physical_numbers():
    unit = TunerUnit(key='right', label='右 Tuner', axis_base=3, axis_count=3)
    assert unit.physical_axis('X') == 3
    assert unit.physical_axis('Z1') == 4
    assert unit.physical_axis('Z2') == 5
    with pytest.raises(ValueError):
        unit.physical_axis('Z3')


def test_overlapping_axis_blocks_are_rejected():
    units = {
        'left': TunerUnit(key='left', axis_base=0, axis_count=3),
        'right': TunerUnit(key='right', axis_base=2, axis_count=3),
    }
    with pytest.raises(ValueError):
        validate_units(units)


def test_a_unit_may_not_run_past_the_controller():
    with pytest.raises(ValueError):
        TunerUnit(key='bad', axis_base=4, axis_count=3)


def test_units_round_trip_through_json():
    units = default_units()
    restored = normalise_units({k: v.to_dict() for k, v in units.items()})
    assert restored == units


# ---------------------------------------------------------------- settings
def test_a_pre_schema_4_file_keeps_its_measured_parameters(tmp_path):
    """The commissioned machine has real UNITS and origin inputs on the left."""
    path = pathlib.Path(tmp_path) / 'settings.json'
    path.write_text(
        '{"schema_version": 3, "connection": {"ip": "192.168.0.12"},'
        ' "mode": "real", "axis_count": 3, "axes": {'
        '"X": {"name": "X", "units": 10000.0, "datum_in": 28,'
        ' "datum_mode": 4, "fs_limit": 12.0, "rs_limit": 0.0,'
        ' "feedback_scale": 100.0},'
        '"Z1": {"name": "Z1", "units": 10000.0, "datum_in": 29,'
        ' "datum_mode": 4, "fs_limit": 5.0, "rs_limit": 0.0,'
        ' "feedback_scale": 100.0}}}',
        encoding='utf-8')

    settings = SettingsStore(path).load()
    assert settings.active_unit == 'left'
    left = settings.unit_axes('left')
    assert left['X'].units == 10000.0
    assert left['X'].datum_in == 28
    assert left['Z1'].datum_in == 29
    # The origin inputs of the left tuner are inferred, not guessed.
    assert settings.units['left'].datum_in_base == 28
    assert settings.units['right'].datum_in_base == 31


def test_switching_units_keeps_both_parameter_sets(tmp_path):
    settings = TunerSettings()
    settings.axes['X'] = AxisParams(**{**settings.axes['X'].to_dict(),
                                       'units': 12345.0})
    settings.axes_by_unit[settings.active_unit] = dict(settings.axes)

    settings.set_unit('right')
    assert settings.active_unit == 'right'
    assert settings.unit_axes('right')['X'].units != 12345.0

    settings.set_unit('left')
    assert settings.active_unit == 'left'
    assert settings.unit_axes('left')['X'].units == 12345.0


def test_the_active_unit_drives_the_visible_axis_count(tmp_path):
    settings = TunerSettings()
    settings.set_unit('right')
    assert settings.axis_count == settings.units['right'].axis_count
    assert settings.active_axes == ('X', 'Z1', 'Z2')


def test_a_stale_active_unit_falls_back_to_a_known_one(tmp_path):
    settings = TunerSettings()
    settings.active_unit = 'does-not-exist'
    settings.__post_init__()
    assert settings.active_unit in settings.units


def test_the_settings_file_records_both_tuners(tmp_path):
    store = _store(tmp_path)
    settings = TunerSettings()
    store.save(settings)
    raw = store.path.read_text(encoding='utf-8')
    data = json.loads(raw)
    assert data['schema_version'] == SCHEMA_VERSION
    assert data['active_unit'] == 'left'
    assert set(data['units']) == {'left', 'right'}
    assert set(data['axes_by_unit']) == {'left', 'right'}


# -------------------------------------------------------------- controller
def test_the_controller_addresses_the_right_physical_axes(tmp_path):
    """The right tuner must command axes 3/4/5, never 0/1/2."""
    from tuner.zmotion_motion import ZmotionMotionController, build_tuner_config

    params = default_axis_params(3)
    config = build_tuner_config(params, axis_count=3)

    class RecordingLink:
        connected = True

        def __init__(self):
            self.moved: list[tuple[int, float]] = []
            self.stopped: list = []
            self.positions: dict[int, float] = {}

        def home_status(self, index):
            return 1

        def get_dpos(self, index):
            return self.positions.get(index, 0.0)

        def get_mpos(self, index):
            return self.positions.get(index, 0.0)

        def move_absolute(self, index, value):
            self.moved.append((index, float(value)))
            self.positions[index] = float(value)

        def wait_idle(self, axes, timeout):
            return True

        def stop_axes(self, axes):
            self.stopped.append(tuple(axes))

        def read_all_axis_info(self, count):
            return {
                'idle': np.ones(count, dtype=int),
                'dpos': np.zeros(count, dtype=float),
                'mpos': np.zeros(count, dtype=float),
                'status': np.zeros(count, dtype=int),
            }

    link = RecordingLink()
    controller = ZmotionMotionController(config, link, params, axis_count=3,
                                         axis_base=3, op_base=3)
    assert controller.axis_map() == {'X': 3, 'Z1': 4, 'Z2': 5}

    controller.initialized = True
    controller.homed_axes = set(controller.axes)
    controller.homed = True
    controller.move_axis_to('Z1', 1.0)
    assert link.moved[-1] == (4, 1.0)

    controller.stop_all()
    assert link.stopped[-1] == (3, 4, 5)


def test_the_status_snapshot_reports_the_physical_axis(tmp_path):
    from tuner.zmotion_motion import ZmotionMotionController, build_tuner_config

    params = default_axis_params(3)
    config = build_tuner_config(params, axis_count=3)

    class Link:
        connected = True

        def home_status(self, index):
            return 1

        def axis_stop_reason(self, index):
            return 0

        def get_dpos(self, index):
            return float(index)

        def get_mpos(self, index):
            return float(index)

        def read_all_axis_info(self, count):
            return {
                'idle': np.ones(count, dtype=int),
                'dpos': np.arange(count, dtype=float),
                'mpos': np.arange(count, dtype=float),
                'status': np.zeros(count, dtype=int),
            }

    controller = ZmotionMotionController(config, Link(), params, axis_count=3,
                                         axis_base=3)
    snapshot = controller.axis_status_snapshot()
    assert [entry['physical_axis'] for entry in snapshot] == [3, 4, 5]
    assert [entry['dpos'] for entry in snapshot] == [3.0, 4.0, 5.0]


# ----------------------------------------------------------------- session
def test_the_session_lists_both_tuners(tmp_path):
    session = HardwareSession(_store(tmp_path))
    units = session.available_units()
    assert [unit['key'] for unit in units] == ['left', 'right']
    assert units[1]['physical_axes'] == [3, 4, 5]
    assert units[0]['active'] is True


def test_switching_the_tuner_rebuilds_the_backend(tmp_path):
    session = HardwareSession(_store(tmp_path))
    session.connect(mode='virtual')

    left_map = session.controller.axis_map()
    session.set_active_unit('right')
    right_map = session.controller.axis_map()

    assert left_map == {'X': 0, 'Z1': 1, 'Z2': 2}
    assert right_map == {'X': 3, 'Z1': 4, 'Z2': 5}
    assert session.settings.active_unit == 'right'


def test_switching_the_tuner_does_not_reopen_the_link(tmp_path):
    session = HardwareSession(_store(tmp_path))
    session.connect(mode='virtual')
    link = session.link
    session.set_active_unit('right')
    assert session.link is link
    assert session.connected


def test_an_unknown_tuner_is_refused(tmp_path):
    session = HardwareSession(_store(tmp_path))
    with pytest.raises(ValueError):
        session.set_active_unit('middle')


def test_switching_the_tuner_is_persisted(tmp_path):
    store = _store(tmp_path)
    session = HardwareSession(store)
    session.connect(mode='virtual')
    session.set_active_unit('right')
    assert store.load().active_unit == 'right'


# -------------------------------------------------------------------- web
def test_the_web_api_lists_and_switches_the_tuners(tmp_path):
    from tuner.web import handlers

    class WebState:
        def __init__(self):
            self.hw = HardwareSession(_store(tmp_path))
            self.hw_lock = __import__('threading').Lock()

    state = WebState()
    listing = handlers.hw_units(state, {})
    assert [unit['key'] for unit in listing['units']] == ['left', 'right']

    result = handlers.hw_set_unit(state, {'unit': 'right'})
    assert result['active_unit'] == 'right'
    assert state.hw.settings.active_unit == 'right'

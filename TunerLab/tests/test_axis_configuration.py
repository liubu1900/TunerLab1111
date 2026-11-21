"""3-axis / 4-axis coexistence: device combinations and physical axis numbers.

Both tuner models are supported at the same time and are mutually exclusive
installations:

* 3 axes -> two 3-axis tuners (left 0/1/2, right 3/4/5)
* 4 axes -> one 4-axis tuner (X=0, Z1=1, Z2=2, Z3=3)

Switching the model swaps the whole combination while every unit's tuned
parameters are kept.  These tests pin down that mapping and the persistence
across a save/load cycle, including migration of an old single-combination
settings file.  This module deliberately uses only the standard library so it
can run without numpy; ``tuner.vna_drivers`` is stubbed for the dialect helper.
"""
from __future__ import annotations

import json
import pathlib
import sys
import types

import pytest

# settings._clean_dialect imports vna_drivers (which imports numpy) even for an
# empty dialect.  Stub just the constant it needs so these pure-Python tests do
# not pull in the scientific stack.
if 'tuner.vna_drivers' not in sys.modules:
    try:
        import tuner.vna_drivers  # noqa: F401
    except ModuleNotFoundError:
        stub = types.ModuleType('tuner.vna_drivers')
        stub.DIALECT_FIELDS = ('define_cmd', 'fetch_mnemonic', 'power_cmd')
        sys.modules['tuner.vna_drivers'] = stub

from tuner.axis_params import (  # noqa: E402
    DEFAULT_ACTIVE_AXIS_COUNT, VISIBLE_TUNER_MODELS, tuner_axes,
)
from tuner.settings import SCHEMA_VERSION, SettingsStore, TunerSettings  # noqa: E402
from tuner.tuner_units import (  # noqa: E402
    installation_label, units_for_model,
)


# ------------------------------------------------------------- declarations
def test_both_models_are_offered():
    assert set(VISIBLE_TUNER_MODELS) == {3, 4}
    assert DEFAULT_ACTIVE_AXIS_COUNT == 3
    assert tuner_axes(3) == ('X', 'Z1', 'Z2')
    assert tuner_axes(4) == ('X', 'Z1', 'Z2', 'Z3')


def test_each_model_resolves_to_its_device_combination():
    three = units_for_model(3)
    assert {k: v.axis_map() for k, v in three.items()} == {
        'left': {'X': 0, 'Z1': 1, 'Z2': 2},
        'right': {'X': 3, 'Z1': 4, 'Z2': 5},
    }
    four = units_for_model(4)
    assert {k: v.axis_map() for k, v in four.items()} == {
        'tuner_1': {'X': 0, 'Z1': 1, 'Z2': 2, 'Z3': 3},
    }
    assert '单台四轴' in installation_label(4)
    assert '双台三轴' in installation_label(3)


# ---------------------------------------------------------------- switching
def test_switching_to_four_axes_uses_a_single_tuner_0_to_3():
    settings = TunerSettings()
    assert settings.axis_count == 3
    assert set(settings.units) == {'left', 'right'}

    settings.set_model(4)
    assert settings.axis_count == 4
    assert settings.active_unit == 'tuner_1'
    assert set(settings.units) == {'tuner_1'}
    assert settings.unit.axis_map() == {'X': 0, 'Z1': 1, 'Z2': 2, 'Z3': 3}
    assert settings.active_axes == ('X', 'Z1', 'Z2', 'Z3')


def test_switching_back_restores_the_two_tuner_combination():
    settings = TunerSettings()
    settings.set_model(4)
    settings.set_model(3)
    assert settings.axis_count == 3
    assert set(settings.units) == {'left', 'right'}
    assert settings.unit.axis_map() == {'X': 0, 'Z1': 1, 'Z2': 2}


def test_switching_keeps_each_units_tuned_parameters():
    settings = TunerSettings()
    settings.axes['X'].units = 12345.0
    settings.axes_by_unit[settings.active_unit] = dict(settings.axes)
    settings.set_model(4)
    settings.axes['Z3'].units = 999.0

    settings.set_model(3)
    assert settings.unit_axes('left')['X'].units == 12345.0
    assert settings.unit_axes('tuner_1')['Z3'].units == 999.0


def test_the_legacy_alias_switches_the_whole_combination():
    settings = TunerSettings()
    settings.set_unit_axis_count('left', 4)
    assert settings.axis_count == 4
    assert settings.unit.axis_map() == {'X': 0, 'Z1': 1, 'Z2': 2, 'Z3': 3}


# ----------------------------------------------------------------- persist
def test_both_combinations_survive_a_save_load_cycle(tmp_path):
    store = SettingsStore(pathlib.Path(tmp_path) / 'settings.json')
    settings = TunerSettings()
    settings.set_model(4)
    settings.axes['Z3'].units = 999.0
    store.save(settings)

    data = json.loads(store.path.read_text(encoding='utf-8'))
    assert data['schema_version'] == SCHEMA_VERSION
    assert set(data['units_by_model']) == {'3', '4'}

    restored = store.load()
    assert restored.axis_count == 4
    assert restored.unit.axis_map() == {'X': 0, 'Z1': 1, 'Z2': 2, 'Z3': 3}
    assert restored.axes['Z3'].units == 999.0
    restored.set_model(3)
    assert set(restored.units) == {'left', 'right'}


def test_an_old_file_without_units_by_model_still_opens(tmp_path):
    """A pre-schema-6 file (no units_by_model) migrates, then can go to 4 axes."""
    from tuner.axis_params import AxisParams

    axes = {name: AxisParams(name=name).to_dict() for name in tuner_axes(3)}
    payload = {
        'schema_version': 5,
        'connection': {'ip': '192.168.0.11'},
        'mode': 'real',
        'axis_count': 3,
        'axes': axes,
    }
    path = pathlib.Path(tmp_path) / 'settings.json'
    path.write_text(json.dumps(payload), encoding='utf-8')

    settings = SettingsStore(path).load()
    assert settings.axis_count == 3
    assert settings.unit.axis_map() == {'X': 0, 'Z1': 1, 'Z2': 2}
    settings.set_model(4)
    assert settings.unit.axis_map() == {'X': 0, 'Z1': 1, 'Z2': 2, 'Z3': 3}

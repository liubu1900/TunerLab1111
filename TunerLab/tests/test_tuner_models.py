"""Support for the two tuner models: 3-axis (X/Z1/Z2) and 4-axis (X/Z1/Z2/Z3).

The controller always exposes four axes at fixed indices (X=0, Z1=1, Z2=2,
Z3=3).  What differs between the two tuner models is *which* of them belong to
the tuner, so the model is a configuration value rather than a compile-time
constant.
"""
import json
import os
import pathlib

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
import pytest

from tuner.axis_params import (
    CONTROLLER_AXES, DEFAULT_AXIS_COUNT, TUNER_MODELS, axis_count_of,
    axis_index, default_axis_params, tuner_axes,
)
from tuner.models import TunerConfig
from tuner.settings import SCHEMA_VERSION, SettingsStore, TunerSettings
from tuner.zmotion_motion import ZmotionMotionController, build_tuner_config


# ----------------------------------------------------------------- models
def test_the_two_models_are_defined():
    assert TUNER_MODELS[3] == ('X', 'Z1', 'Z2')
    assert TUNER_MODELS[4] == ('X', 'Z1', 'Z2', 'Z3')


def test_the_controller_axis_order_does_not_depend_on_the_model():
    """The physical axis index of an axis never changes."""
    assert CONTROLLER_AXES == ('X', 'Z1', 'Z2', 'Z3')
    for count in TUNER_MODELS:
        for name in tuner_axes(count):
            assert axis_index(name) == CONTROLLER_AXES.index(name)


def test_tuner_axes_rejects_an_unknown_model():
    with pytest.raises(ValueError):
        tuner_axes(5)
    with pytest.raises(ValueError):
        tuner_axes(0)


def test_axis_count_of_round_trips():
    for count, names in TUNER_MODELS.items():
        assert axis_count_of(names) == count
    with pytest.raises(ValueError):
        axis_count_of(('X', 'Z1'))


def test_axis_index_can_be_restricted_to_a_model():
    assert axis_index('Z2', ('X', 'Z1', 'Z2')) == 2
    with pytest.raises(ValueError):
        axis_index('Z3', ('X', 'Z1', 'Z2'))


def test_default_parameters_follow_the_model():
    assert set(default_axis_params(3)) == {'X', 'Z1', 'Z2'}
    assert set(default_axis_params(4)) == {'X', 'Z1', 'Z2', 'Z3'}
    # The origin inputs keep their controller channel numbers.
    assert default_axis_params(3)['Z2'].datum_in == 18
    assert default_axis_params(4)['Z3'].datum_in == 19


# --------------------------------------------------------------- settings
def test_settings_default_to_the_four_axis_model():
    settings = TunerSettings()
    assert settings.axis_count == DEFAULT_AXIS_COUNT
    assert settings.active_axes == ('X', 'Z1', 'Z2', 'Z3')
    assert settings.probe_count == 3


def test_settings_round_trip_keeps_the_model(tmp_path):
    store = SettingsStore(tmp_path / 'settings.json')
    settings = TunerSettings(axis_count=3)
    store.save(settings)
    loaded = store.load()
    assert loaded.axis_count == 3
    assert loaded.active_axes == ('X', 'Z1', 'Z2')
    assert set(loaded.axes) >= {'X', 'Z1', 'Z2'}


def test_switching_the_model_keeps_the_tuned_axes(tmp_path):
    """The parameters of the axis that leaves the model are not thrown away."""
    store = SettingsStore(tmp_path / 'settings.json')
    settings = TunerSettings(axis_count=4)
    axes = dict(settings.axes)
    axes['Z3'] = type(axes['Z3'])(**{**axes['Z3'].to_dict(), 'units': 123.0})
    settings.axes = axes
    store.save(settings)

    loaded = store.load()
    loaded.axis_count = 3
    # Z3 is not part of the model any more, but its parameters survive.
    assert loaded.axes['Z3'].units == pytest.approx(123.0)
    assert loaded.active_axes == ('X', 'Z1', 'Z2')


def test_an_old_settings_file_still_loads(tmp_path):
    """Schema 1 had no axis_count and always described the four-axis model."""
    store = SettingsStore(tmp_path / 'settings.json')
    legacy = {
        'schema_version': 1,
        'connection': {'ip': '192.168.0.11', 'connect_on_start': False},
        'axes': {
            'X': {'name': 'X', 'units': 2500.0, 'datum_in': 16},
            'Z1': {'name': 'Z1', 'units': 10000.0, 'datum_in': 17},
            'Z2': {'name': 'Z2', 'units': 10000.0, 'datum_in': 18},
            'Z3': {'name': 'Z3', 'units': 10000.0, 'datum_in': 19},
        },
    }
    store.path.write_text(json.dumps(legacy), encoding='utf-8')
    loaded = store.load()
    assert loaded.axis_count == 4
    assert loaded.axes['X'].units == pytest.approx(2500.0)


def test_the_settings_file_records_the_model(tmp_path):
    store = SettingsStore(tmp_path / 'settings.json')
    store.save(TunerSettings(axis_count=3))
    data = json.loads(store.path.read_text(encoding='utf-8'))
    assert data['schema_version'] == SCHEMA_VERSION
    assert data['axis_count'] == 3


def test_an_unsupported_model_is_rejected():
    with pytest.raises(ValueError):
        TunerSettings(axis_count=5)


# ----------------------------------------------------------------- config
def test_the_config_geometry_follows_the_model():
    axes = default_axis_params(3)
    config = build_tuner_config(axes)
    assert config.probes == 2
    assert config.axes == ('X', 'Z1', 'Z2')
    assert len(config.fixed_offsets) == 2

    axes = default_axis_params(4)
    config = build_tuner_config(axes)
    assert config.probes == 3
    assert config.axes == ('X', 'Z1', 'Z2', 'Z3')


def test_the_three_axis_grid_is_quadratic_in_the_probe_count():
    """Three axes need x_count * z_count**2 points, four axes x_count * z_count**3."""
    config = TunerConfig(probes=2, fixed_offsets=(0.0, 2.0))
    grid3 = config.grid(np.linspace(0, 10, 9), np.linspace(0, 1, 4))
    assert len(grid3) == 9 * 4 ** 2

    config = TunerConfig(probes=3, fixed_offsets=(0.0, 2.0, 4.0))
    grid4 = config.grid(np.linspace(0, 10, 9), np.linspace(0, 1, 4))
    assert len(grid4) == 9 * 4 ** 3


def test_the_position_of_the_three_axis_model_has_three_components():
    config = TunerConfig(probes=2, fixed_offsets=(0.0, 2.0))
    position = config.validate_position([1.0, 0.2, 0.3])
    assert position.shape == (3,)
    with pytest.raises(ValueError):
        config.validate_position([1.0, 0.2, 0.3, 0.4])


# ----------------------------------------------------------------- motion
class _FakeLink:
    """Minimal stand-in for the Zmotion link."""

    def __init__(self):
        self.connected = True
        self.positions = {0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0}
        self.homed = {0: 1, 1: 1, 2: 1, 3: 1}
        self.moved = []

    def home_status(self, index):
        return self.homed.get(index, 0)

    def get_dpos(self, index):
        return self.positions.get(index, 0.0)

    def get_mpos(self, index):
        return self.positions.get(index, 0.0)

    def home_axis(self, index, mode):
        self.homed[index] = 1

    def wait_idle(self, axes, timeout):
        return True

    def stop_axis(self, index):
        pass

    def stop_axes(self, axes):
        pass

    def emergency_stop(self):
        pass

    def set_units(self, index, value):
        pass

    def axis_stop_reason(self, index):
        return 0

    def read_all_axis_info(self, count):
        return {
            'idle': [1] * count,
            'dpos': [self.get_dpos(i) for i in range(count)],
            'mpos': [self.get_mpos(i) for i in range(count)],
            'status': [0] * count,
        }


def _controller(axis_count):
    axes = default_axis_params(axis_count)
    config = build_tuner_config(axes, axis_count=axis_count)
    return ZmotionMotionController(config, _FakeLink(), axes,
                                   axis_count=axis_count)


def test_the_controller_commands_only_the_axes_of_the_model():
    controller = _controller(3)
    assert controller.axes == ('X', 'Z1', 'Z2')
    assert len(controller.read_position()) == 3


def test_the_four_axis_controller_commands_four_axes():
    controller = _controller(4)
    assert controller.axes == ('X', 'Z1', 'Z2', 'Z3')
    assert len(controller.read_position()) == 4


def test_homing_only_touches_the_axes_of_the_model():
    controller = _controller(3)
    controller.initialized = True
    # The fake link starts with every axis homed; homing must still only
    # command the axes that belong to the model.
    controller.link.homed = {0: 1, 1: 1, 2: 1, 3: 0}
    controller.home()
    assert controller.link.homed[3] == 0
    assert controller.homed_axes == {'X', 'Z1', 'Z2'}


def test_an_axis_outside_the_model_is_refused():
    controller = _controller(3)
    with pytest.raises(ValueError):
        controller.jog('Z3', 0.1)
    with pytest.raises(ValueError):
        controller.stop_axis('Z3')
    with pytest.raises(ValueError):
        controller.set_axis_units('Z3', 100.0)


def test_the_status_snapshot_has_one_entry_per_axis():
    assert len(_controller(3).axis_status_snapshot()) == 3
    assert len(_controller(4).axis_status_snapshot()) == 4


# ------------------------------------------------------------- desktop UI
def test_the_desktop_pages_follow_the_tuner_model(qapp):
    """Switching the model rebuilds the axis rows of every page."""
    from tuner.ui_hardware import (
        AxisMotionPage, AxisParameterPage, CommunicationPage, HardwareSession,
    )
    from tuner.settings import SettingsStore

    store = SettingsStore(tmp_path_for_ui() / 'settings.json')
    session = HardwareSession(store)
    communication = CommunicationPage(session)
    motion = AxisMotionPage(session)
    parameters = AxisParameterPage(session)

    assert len(motion.axis_panels) == 4
    assert parameters.table.rowCount() == 4

    index = communication.model_combo.findData(3)
    assert index >= 0
    communication.model_combo.setCurrentIndex(index)
    qapp.processEvents()

    assert session.settings.axis_count == 3
    assert sorted(motion.position_labels) == ['X', 'Z1', 'Z2']
    assert len(motion.axis_panels) == 3
    assert parameters.table.rowCount() == 3

    index = communication.model_combo.findData(4)
    communication.model_combo.setCurrentIndex(index)
    qapp.processEvents()

    assert sorted(motion.position_labels) == ['X', 'Z1', 'Z2', 'Z3']
    assert parameters.table.rowCount() == 4


def tmp_path_for_ui():
    import tempfile
    return pathlib.Path(tempfile.mkdtemp())


# ------------------------------------------------------------------- web
def test_the_web_api_switches_the_model(web_state):
    from tuner.web import handlers

    handlers.hw_set_model(web_state, {'axis_count': 3})
    settings = handlers.hw_settings(web_state, {})
    assert settings['axis_count'] == 3
    assert settings['axes'] == ['X', 'Z1', 'Z2']

    params = handlers.hw_params(web_state, {})
    assert sorted(params) == ['X', 'Z1', 'Z2']

    handlers.hw_set_model(web_state, {'axis_count': 4})
    params = handlers.hw_params(web_state, {})
    assert sorted(params) == ['X', 'Z1', 'Z2', 'Z3']


def test_the_web_api_rejects_an_unknown_model(web_state):
    from tuner.web import handlers

    with pytest.raises(ValueError):
        handlers.hw_set_model(web_state, {'axis_count': 5})


def test_the_web_status_reports_the_model(web_state):
    from tuner.web import handlers

    handlers.hw_set_model(web_state, {'axis_count': 3})
    status = web_state.hw_status()
    assert status['axis_count'] == 3
    assert status['active_axes'] == ['X', 'Z1', 'Z2']


# ------------------------------------------------------------- fixtures
@pytest.fixture(scope='module')
def qapp():
    """A Qt application instance for the widget tests."""
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def web_state(tmp_path, monkeypatch):
    """The shared web state, with its settings redirected to a temp file."""
    from tuner.web import state as state_module

    original = state_module.state
    fresh = state_module.AppState()
    fresh.hw.store = SettingsStore(tmp_path / 'settings.json')
    fresh.hw.settings = fresh.hw._load_settings()
    monkeypatch.setattr(state_module, 'state', fresh)
    try:
        yield fresh
    finally:
        monkeypatch.setattr(state_module, 'state', original)

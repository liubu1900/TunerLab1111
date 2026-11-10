"""The virtual (simulated) controller backend and the synthetic data generator.

The virtual mode lets the operator rehearse the whole console without the
machine: the simulated controller answers every command, and the generator
produces a calibration database with the same structure as a measured one.
"""
import json
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
import pytest

from tuner.axis_params import default_axis_params
from tuner.mock_hardware import (
    ALARM_INPUT_BIT, MockZmotionController, build_virtual_config,
)
from tuner.models import TunerConfig
from tuner.motion import MotionError
from tuner.settings import TunerSettings
from tuner.virtual_data import VirtualTunerModel, generate_calibration


# ------------------------------------------------------- virtual controller
def _controller(axis_count=4):
    axes = default_axis_params(axis_count)
    controller = MockZmotionController(
        build_virtual_config(axes, axis_count), axes, axis_count=axis_count)
    controller.initialize()
    controller.home()
    return controller


def test_the_virtual_controller_has_the_same_axis_set():
    controller = _controller(3)
    assert controller.axes == ('X', 'Z1', 'Z2')
    assert len(controller.axis_status_snapshot()) == 3
    controller = _controller(4)
    assert controller.axes == ('X', 'Z1', 'Z2', 'Z3')
    assert len(controller.axis_status_snapshot()) == 4


def test_the_virtual_controller_seeds_itself_with_data():
    controller = _controller(4)
    # Connecting already homes the machine, so the operator can move right away.
    assert controller.homed_axes == {'X', 'Z1', 'Z2', 'Z3'}
    positions = controller.read_position()
    assert positions.shape == (4,)
    assert np.all(np.isfinite(positions))


def test_the_virtual_controller_moves_and_reports():
    controller = _controller(4)
    controller.move_axis_to('Z1', 0.25)
    assert controller.read_position()[1] == pytest.approx(0.25)
    snapshot = controller.axis_status_snapshot()
    assert snapshot[1]['dpos'] == pytest.approx(0.25)
    assert snapshot[1]['status_text'] == '正常'


def test_the_virtual_scale_check_reports_a_consistent_ratio():
    controller = _controller(4)
    result = controller.probe_axis_scale('Z1')
    assert result['ratio'] == pytest.approx(1.0, abs=1e-6)
    assert result['consistent'] is True
    # The probe returns the axis to where it started.
    assert controller.read_position()[1] == pytest.approx(0.0)


def test_a_mis_scaled_virtual_axis_is_detected():
    controller = _controller(4)
    controller.feedback_ratio = 84.0
    result = controller.probe_axis_scale('Z1')
    assert result['ratio'] == pytest.approx(84.0, rel=1e-3)
    assert result['consistent'] is False
    # The correction is the UNITS divided by the measured ratio.
    assert result['suggested_units'] == pytest.approx(10000.0 / 84.0, rel=1e-3)


def test_the_virtual_controller_latches_the_emergency_stop():
    controller = _controller(4)
    controller.emergency_stop()
    assert controller.stopped is True
    with pytest.raises(MotionError):
        controller.jog('Z1', 0.1)
    controller.reset_emergency()
    controller.home()
    assert controller.stopped is False


def test_an_unhomed_axis_blocks_only_itself():
    controller = _controller(4)
    controller.link.homed = {0: 1, 1: 0, 2: 1, 3: 1}
    controller.refresh_home_state()
    assert controller.homed_axes == {'X', 'Z2', 'Z3'}
    with pytest.raises(MotionError):
        controller.jog('Z1', 0.1)
    # The other axes still move.
    controller.move_axis_to('Z2', 0.1)


def test_a_fault_can_be_injected_and_cleared():
    controller = _controller(4)
    status = controller.inject_fault('Z2', ALARM_INPUT_BIT)
    assert status == 1 << ALARM_INPUT_BIT
    snapshot = controller.axis_status_snapshot()
    entry = next(item for item in snapshot if item['axis'] == 'Z2')
    assert entry['status_fault'] is True
    assert 'bit22' in entry['status_text']
    controller.clear_faults()
    snapshot = controller.axis_status_snapshot()
    entry = next(item for item in snapshot if item['axis'] == 'Z2')
    assert entry['status_fault'] is False
    assert entry['status_text'] == '正常'


def test_an_axis_outside_the_model_is_refused():
    controller = _controller(3)
    with pytest.raises(ValueError):
        controller.jog('Z3', 0.1)
    with pytest.raises(ValueError):
        controller.inject_fault('Z3')


# ------------------------------------------------------------- settings
def test_the_mode_is_persisted(tmp_path):
    from tuner.settings import SettingsStore

    store = SettingsStore(tmp_path / 'settings.json')
    store.save(TunerSettings(mode='virtual'))
    assert store.load().mode == 'virtual'


def test_an_old_settings_file_means_the_real_controller(tmp_path):
    from tuner.settings import SettingsStore

    store = SettingsStore(tmp_path / 'settings.json')
    store.path.write_text(json.dumps({
        'schema_version': 1,
        'connection': {'ip': '192.168.0.11'},
        'axes': {},
    }), encoding='utf-8')
    assert store.load().mode == 'real'


def test_an_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        TunerSettings(mode='magic')


# ------------------------------------------------------- session switching
def test_the_session_switches_between_real_and_virtual(tmp_path):
    from tuner.settings import SettingsStore
    from tuner.ui_hardware import HardwareSession

    session = HardwareSession(SettingsStore(tmp_path / 'settings.json'))
    assert session.mode == 'real'

    session.set_mode('virtual')
    assert session.mode == 'virtual'
    assert session.settings.mode == 'virtual'

    session.connect(mode='virtual')
    assert session.connected is True
    assert session.controller is not None
    # The virtual controller is already homed, so it can move right away.
    assert session.controller.homed_axes

    session.set_mode('real')
    # Switching drops the link: the two backends have nothing in common.
    assert session.connected is False
    assert session.controller is None
    assert session.mode == 'real'


def test_the_session_refuses_an_unknown_mode(tmp_path):
    from tuner.settings import SettingsStore
    from tuner.ui_hardware import HardwareSession

    session = HardwareSession(SettingsStore(tmp_path / 'settings.json'))
    with pytest.raises(ValueError):
        session.set_mode('magic')


# ------------------------------------------------------- synthetic data
def test_the_generator_writes_a_complete_database(tmp_path):
    config = TunerConfig(probes=2, fixed_offsets=(0.0, 2.0))
    path = tmp_path / 'virtual.h5'
    summary = generate_calibration(path, config, x_count=5, z_count=3)
    assert summary['points'] == 5 * 3 ** 2
    assert summary['valid'] == summary['points']
    assert path.is_file()

    from tuner.database import CalibrationDatabase
    data = CalibrationDatabase(path).load()
    assert data['positions'].shape == (5 * 3 ** 2, 3)
    assert bool(np.all(data['valid']))


def test_the_generator_refuses_to_overwrite(tmp_path):
    config = TunerConfig(probes=2, fixed_offsets=(0.0, 2.0))
    path = tmp_path / 'virtual.h5'
    generate_calibration(path, config, x_count=3, z_count=2)
    with pytest.raises(FileExistsError):
        generate_calibration(path, config, x_count=3, z_count=2)


def test_the_generated_data_covers_the_smith_chart():
    """The synthetic response must reach a wide part of the Gamma plane."""
    model = VirtualTunerModel()
    gammas = []
    for x in np.linspace(0, 10, 9):
        for z1 in np.linspace(0, 1, 4):
            for z2 in np.linspace(0, 1, 4):
                gammas.append(model.gamma([x, z1, z2]))
    magnitudes = np.abs(gammas)
    phases = np.degrees(np.angle(gammas))
    assert magnitudes.min() < 0.1
    assert magnitudes.max() > 0.8
    # The phase must sweep the whole circle, otherwise the Smith chart would
    # show a single arc instead of a filled region.
    assert phases.max() - phases.min() > 300.0


def test_the_generated_data_can_be_tuned(tmp_path):
    """The whole chain works on synthetic data: load, interpolate, tune."""
    from tuner.database import CalibrationDatabase
    from tuner.rf import RFNetworkEngine as RF
    from tuner.search import GammaSearchEngine

    config = TunerConfig(probes=2, fixed_offsets=(0.0, 2.0))
    path = tmp_path / 'virtual.h5'
    generate_calibration(path, config, x_count=7, z_count=4)
    data = CalibrationDatabase(path).load()
    engine = GammaSearchEngine(data, z0=50.0)
    model = engine.response_model()
    target = RF.z_to_gamma(complex(30.0, 0.0), 50.0)
    result = model.tune(target, 10e9)
    assert result['error'] < 1e-6
    assert [stage['stage'] for stage in result['stages']][:2] == [
        'general_search', 'gradient_search']

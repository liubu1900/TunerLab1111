"""Real-hardware motion in the calibration studio, and the scale correction.

The scale defect found during commissioning is the reason this module exists: with
``UNITS`` set to the encoder resolution instead of the drive's pulse equivalent, one
commanded unit is not one millimetre and every calibration point is off by that factor.
The software therefore refuses to run a real-hardware calibration until the axis scales
agree, and it computes the corrected ``UNITS`` value from a measured ratio.
"""
from types import SimpleNamespace

import numpy as np
import pytest

from test_zmotion import make_controller

from tuner.axis_params import AxisParams, TUNER_AXES, default_axis_params, tuner_axes
from tuner.motion import MotionError
from tuner.settings import SettingsStore
from tuner.ui_hardware import HardwareSession
from tuner.web import handlers
from tuner.web.state import state
from tuner.zmotion_motion import ZmotionMotionController, build_tuner_config


# --------------------------------------------------------------- helpers
class FakeRealController:
    """Minimal stand-in for the real motion backend."""

    simulated = False

    def __init__(self, homed=('X', 'Z1', 'Z2', 'Z3'), axis_params=None):
        self.homed_axes = set(homed)
        self.homed = True
        self.stopped = False
        self.axis_params = axis_params if axis_params is not None else {}
        self._position = np.zeros(4, dtype=float)
        self.moves: list[list[float]] = []

    def scale_is_consistent(self, axis: str) -> bool:
        """Mirror the core controller's contract.

        The studio's scale gate asks the motion controller instead of reading
        ``feedback_scale`` itself, because the desktop application stores a
        different quantity in that field.  The core adapter reads the encoder
        in the controller's own counts, so 1.0 means "the axis travels exactly
        as far as it is told".
        """
        params = self.axis_params.get(axis)
        if params is None:
            return True
        return abs(float(params.feedback_scale) - 1.0) <= 0.05

    def read_position(self):
        return self._position.copy()

    def move_to(self, position, timeout=None):
        self._position = np.asarray(position, dtype=float)
        self.moves.append(list(self._position))
        return self._position.copy()

    def home(self, timeout=120.0):
        self.homed_axes = set(TUNER_AXES)
        self.homed = True
        return self.read_position()

    def jog(self, axis, delta):
        return self.move_to(self._position)

    def retract(self):
        return self.move_to(self._position)

    def emergency_stop(self):
        self.stopped = True

    def reset_emergency(self):
        self.stopped = False


class FakeSession:
    """Stand-in for :class:`tuner.ui_hardware.HardwareSession`."""

    def __init__(self, axes, controller, axis_count=None):
        self.settings = SimpleNamespace(
            axes=axes, connection=SimpleNamespace(ip='192.168.0.11'),
            axis_count=axis_count if axis_count is not None else len(axes))
        self.controller = controller
        self.connected = controller is not None

    def context(self):
        return {'test': 'real-motion'}

    def check_context(self, expected=None):
        assert expected is None or expected == self.context()

    @property
    def axes(self):
        return tuner_axes(self.settings.axis_count)

    @property
    def probe_count(self):
        return self.settings.axis_count - 1

    def require_controller(self):
        if self.controller is None:
            raise MotionError('尚未连接控制器，请先在通讯设置页连接')
        return self.controller


@pytest.fixture
def real_session():
    """Install a fake hardware session into the shared web state."""
    original = state.hw
    axes = default_axis_params()
    # The controller gets the *same* mapping the session hands out, so a test
    # that replaces an axis' parameters is seen by the scale gate too.
    controller = FakeRealController(axis_params=axes)
    state.hw = FakeSession(axes, controller)
    try:
        yield state.hw
    finally:
        state.hw = original
        with state.studio_lock:
            state.studio['motion'] = None
            state.studio['config'] = None
            state.studio['planner'] = None
            state.studio['motion_source'] = 'mock'
            state.studio['homed_axes'] = []


# --------------------------------------------------------- travel limits
def test_build_tuner_config_uses_the_declared_travel_box():
    axes = default_axis_params()
    config = build_tuner_config(axes, (0.0, 10.0), (0.0, 1.0), 0.0)
    assert config.x_limits == (0.0, 10.0)
    assert config.z_limits == (0.0, 1.0)
    assert config.safe_z == 0.0
    assert config.probes == 3
    assert config.simulated is False


def test_build_tuner_config_falls_back_to_the_controller_soft_limits():
    axes = default_axis_params()
    config = build_tuner_config(axes)
    # The controller ships with the factory soft limits.
    assert config.x_limits == (axes['X'].rs_limit, axes['X'].fs_limit)
    assert config.z_limits[1] == axes['Z3'].fs_limit


def test_declared_travel_box_is_enforced_by_the_position_validation():
    axes = default_axis_params()
    config = build_tuner_config(axes, (0.0, 10.0), (0.0, 1.0), 0.0)
    with pytest.raises(ValueError):
        config.validate_position([50.0, 0.0, 0.0, 0.0])
    with pytest.raises(ValueError):
        config.validate_position([1.0, 5.0, 0.0, 0.0])


# ------------------------------------------------------- scale correction
def make_real_controller(feedback_gain=84.0):
    library_controller = make_controller()
    library_controller.lib.feedback_gain = feedback_gain
    library_controller.open()
    axes = default_axis_params()
    controller = ZmotionMotionController(
        build_tuner_config(axes, (0.0, 10.0), (0.0, 1.0), 0.0),
        library_controller, axes)
    controller.initialized = True
    controller.homed_axes = set(TUNER_AXES)
    return controller


def test_scale_probe_reports_the_corrected_units():
    controller = make_real_controller(feedback_gain=84.0)
    result = controller.probe_axis_scale('Z1', distance=0.005)
    assert result['ratio'] == pytest.approx(84.0, rel=1e-6)
    assert result['consistent'] is False
    assert result['units'] == pytest.approx(10000.0)
    # The value that makes one commanded unit equal one millimetre.
    assert result['suggested_units'] == pytest.approx(10000.0 / 84.0, rel=1e-3)


def test_scale_probe_reports_a_consistent_axis_as_consistent():
    controller = make_real_controller(feedback_gain=1.0)
    result = controller.probe_axis_scale('X', distance=0.005)
    assert result['ratio'] == pytest.approx(1.0, rel=1e-6)
    assert result['consistent'] is True
    assert result['suggested_units'] == pytest.approx(result['units'])


def test_applying_the_scale_correction_rewrites_units_and_requires_a_rehome(tmp_path):
    controller = make_real_controller(feedback_gain=84.0)
    session = HardwareSession(store=SettingsStore(tmp_path / 'settings.json'))
    session.set_layout('single_4')
    session.link = controller.link
    session.controller = controller
    session.settings.axes = {
        name: AxisParams(**{**default_axis_params()[name].to_dict(),
                             'feedback_scale': 84.0}) for name in TUNER_AXES}
    controller.homed_axes = set(TUNER_AXES)

    result = session.apply_scale_correction('Z1', 119.0)
    assert result['units'] == 119.0
    assert result['previous_units'] == 10000.0
    assert result['needs_rehome'] is True
    # The controller was told ...
    assert controller.link.get_units(TUNER_AXES.index('Z1')) == pytest.approx(119.0)
    # ... the stored parameters follow ...
    assert session.settings.axes['Z1'].units == pytest.approx(119.0)
    assert session.settings.axes['Z1'].feedback_scale == pytest.approx(1.0)
    # ... the setting survived a reload ...
    reloaded = SettingsStore(tmp_path / 'settings.json').load()
    assert reloaded.axes['Z1'].units == pytest.approx(119.0)
    # ... and the axis is no longer considered homed.
    assert 'Z1' not in controller.homed_axes


def test_scale_correction_rejects_an_unknown_axis(tmp_path):
    controller = make_real_controller(feedback_gain=84.0)
    session = HardwareSession(store=SettingsStore(tmp_path / 'settings.json'))
    session.set_layout('single_4')
    session.link = controller.link
    session.controller = controller
    with pytest.raises(ValueError):
        session.apply_scale_correction('Q9', 100.0)


# ------------------------------------------------------- studio real mode
def test_studio_real_motion_uses_the_hardware_controller(real_session):
    result = handlers.studio_init_hardware(state, {
        'motion_source': 'real',
        'x_min': 0.0, 'x_max': 10.0,
        'z_min': 0.0, 'z_max': 1.0, 'safe_z': 0.0,
        'vna_source': 'mock',
    })
    assert result['motion_source'] == 'real'
    assert tuple(result['config']['x_limits']) == (0.0, 10.0)
    assert tuple(result['config']['z_limits']) == (0.0, 1.0)
    assert result['config']['simulated'] is False
    assert result['homed_axes'] == ['X', 'Z1', 'Z2', 'Z3']
    # The studio drives the very same controller the hardware console owns.
    assert state.studio['motion'] is real_session.controller


def test_studio_real_motion_is_refused_while_the_scale_is_wrong(real_session):
    for name in TUNER_AXES:
        params = real_session.settings.axes[name]
        real_session.settings.axes[name] = AxisParams(
            **{**params.to_dict(), 'feedback_scale': 84.0})
    with pytest.raises(RuntimeError) as excinfo:
        handlers.studio_init_hardware(state, {'motion_source': 'real'})
    assert '标度' in str(excinfo.value)
    assert 'Z1' in str(excinfo.value)


def test_studio_real_motion_needs_a_connected_controller(real_session):
    real_session.controller = None
    real_session.connected = False
    with pytest.raises(MotionError):
        handlers.studio_init_hardware(state, {'motion_source': 'real'})


def test_studio_mock_motion_still_works(real_session):
    result = handlers.studio_init_hardware(state, {
        'motion_source': 'mock', 'probes': 2, 'offsets': [0, 2]})
    assert result['motion_source'] == 'mock'
    assert result['config']['simulated'] is True
    assert state.studio['motion'].simulated is True


def test_studio_home_reports_the_homed_axes(real_session):
    handlers.studio_init_hardware(state, {'motion_source': 'real'})
    real_session.controller.homed_axes = {'X', 'Z1'}
    result = handlers.studio_home(state, {})
    assert result['homed_axes'] == ['X', 'Z1', 'Z2', 'Z3']
    assert state.studio['homed_axes'] == ['X', 'Z1', 'Z2', 'Z3']


def test_studio_overview_reports_the_motion_source(real_session):
    handlers.studio_init_hardware(state, {'motion_source': 'real'})
    overview = state.studio_overview()
    assert overview['motion_source'] == 'real'
    assert overview['homed_axes']


def test_studio_planner_inherits_the_controllers_motion_budget(real_session):
    """The planner's config must use the controller's timeout, not the 5 s default.

    Regression (2026-11-08): ``studio_init_hardware`` builds a *second*
    ``TunerConfig`` for the planner.  That config kept the 5 s default while
    the desktop session had already raised the controller's budget to 60 s
    (X is 400 mm at 10 mm/s).  A 50 mm move then needed 5.02 s and was
    reported as a motion timeout while the axis was still travelling -- the
    machine reached the target, but the software latched a fault and refused
    every following move.
    """
    from tuner.models import TunerConfig

    controller = real_session.controller
    controller.config = TunerConfig(probes=3, fixed_offsets=(0.0, 2.0, 4.0),
                                    motion_timeout=60.0)
    handlers.studio_init_hardware(state, {
        'motion_source': 'real',
        'x_min': 0.0, 'x_max': 380.0,
        'z_min': 0.0, 'z_max': 10.0, 'safe_z': 0.0,
        'vna_source': 'mock',
    })
    assert state.studio['planner'].config.motion_timeout == 60.0

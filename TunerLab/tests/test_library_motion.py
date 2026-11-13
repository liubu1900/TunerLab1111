"""A finished calibration library must drive the tuner on its own (0.5.13.4).

Before this change ``studio_move_to_gamma_only`` demanded a planner created by
``studio_init_hardware`` -- a calibration-wizard step that also initialises a
VNA.  On site the operator arrives with a completed ``.h5`` and a controller
box, so opening the library has to be enough to search its positions and move
there.  These tests pin that down with a 3-axis library whose stored positions
include the small negative Z seen in the field.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from tuner.models import TunerConfig
from tuner.motion import MotionPlanner
from tuner.rf import gamma_to_z
from tuner.web import handlers


#: X / Z1 / Z2 -- the third row is a real point from the field library.
THREE_AXIS_POSITIONS = np.array([
    [0.0, 0.0, 0.0],
    [9.1084, 0.7462, -0.0066],
    [12.0, 5.0, 5.0],
])

META = {'config': {
    'probes': 2, 'x_limits': [0.0, 12.0], 'z_limits': [0.0, 5.0],
    'safe_z': 0.0, 'fixed_offsets': [0.0, 0.0], 'z0': 50.0,
}}

BODY = {'gamma_re': 0.2, 'gamma_im': 0.6, 'method': 'nearest',
        'frequency': 29e9}


class FakeMotion:
    """Stand-in for the live Zmotion controller (one position per axis)."""

    def __init__(self, axes: int = 3):
        self.axes = axes
        self.position = np.zeros(axes)
        self.commands: list[np.ndarray] = []
        self.config = SimpleNamespace(motion_timeout=60.0)

    def read_position(self):
        return self.position.copy()

    def move_to(self, position, timeout=None):
        target = np.asarray(position, dtype=float).copy()
        self.commands.append(target)
        self.position = target.copy()
        return target


class FakeHW:
    def __init__(self, controller=None):
        self.connected = controller is not None
        self.controller = controller
        self.settings = SimpleNamespace(
            axis_count=controller.axes if controller else 3)

    def require_controller(self):
        if not self.connected:
            raise RuntimeError('尚未连接控制器')
        return self.controller


class FakeEngine:
    z0 = 50.0

    def __init__(self, position, gamma=0.2 + 0.6j):
        self.position = np.asarray(position, dtype=float)
        self.gamma = complex(gamma)

    def search(self, frequency, target_gamma, k=5, tolerance=0.05):
        return [SimpleNamespace(
            index=0, position=self.position.copy(), gamma=self.gamma,
            impedance=gamma_to_z(self.gamma, self.z0),
            error=0.001, reachable=True)]

    def tune(self, *args, **kwargs):
        raise AssertionError('method=nearest must not interpolate')


class FakeDatabase:
    def __init__(self):
        self.saved = []

    def save_validation(self, report):
        self.saved.append(report)


def make_state(*, axes=3, positions=None, connect=True, planner=None,
               fingerprint='fp-a', metadata=None):
    positions = (THREE_AXIS_POSITIONS.copy() if positions is None
                 else np.asarray(positions, dtype=float))
    motion = FakeMotion(axes)
    studio = {
        'data': {
            'positions': positions,
            'valid': np.ones(len(positions), bool),
            'frequencies': np.array([29e9]),
            'metadata': META if metadata is None else metadata,
        },
        'database': FakeDatabase(),
        'search_engine': FakeEngine(positions[-1 if len(positions) < 2 else 1]),
        # No wizard was ever run: these are exactly the fields that used to
        # make the button fail with "no motion planner available".
        'planner': planner, 'motion': None, 'config': None, 'vna': None,
        'vna_source': None, 'motion_source': None,
        'calibration_state': 'loaded', 'frequency_hz': 29e9,
        'last_candidates': [], 'last_candidates_fingerprint': None,
        'last_tuned': None, 'last_tuned_fingerprint': None,
        'last_target': None, 'last_selected': None, 'last_tune_to_mode': None,
        'validation_report': None, 'database_fingerprint': fingerprint,
        'calibration_progress': (0, 0), 'homed_axes': [],
    }
    state = SimpleNamespace(
        studio=studio, studio_lock=threading.RLock(),
        hw=FakeHW(motion if connect else None))
    return state, motion


# --------------------------------------------------------------- config
def test_library_config_comes_from_the_library_positions():
    data = {'positions': THREE_AXIS_POSITIONS, 'metadata': META}
    config = handlers._library_tuner_config(data, FakeMotion())
    assert config.probes == 2
    assert config.axes == ('X', 'Z1', 'Z2')
    assert config.motion_timeout == 60.0
    # The declared z_limits start at 0, but the library really reached
    # -0.0066 (encoder zero drift).  Every stored point must stay reachable.
    assert config.z_limits[0] < -0.0066
    for position in THREE_AXIS_POSITIONS:
        np.testing.assert_allclose(config.validate_position(position), position)
    assert config.x_limits[1] >= 12.0
    assert config.z_limits[1] >= 5.0


def test_interpolated_points_just_outside_the_sampled_grid_are_reachable():
    """The field library's search produced Z = -0.0066 from a grid starting at 0."""
    data = {'positions': np.array([[0., 0., 0.], [12., 5., 5.]]),
            'metadata': META}
    config = handlers._library_tuner_config(data, FakeMotion())
    predicted = [9.1084, 0.7462, -0.0066]
    np.testing.assert_allclose(config.validate_position(predicted), predicted)
    # The margin is small: a truly out-of-range command is still refused.
    with pytest.raises(ValueError):
        config.validate_position([9.1084, 0.7462, -1.0])


def test_a_four_axis_library_still_builds_a_four_axis_config():
    positions = np.array([[0., 0., 0., 0.], [9., 1., 2., 3.]])
    data = {'positions': positions, 'metadata': META}
    config = handlers._library_tuner_config(data, FakeMotion(4))
    assert config.probes == 3
    assert config.axes == ('X', 'Z1', 'Z2', 'Z3')


# --------------------------------------------------------------- move only
def test_move_only_works_without_a_wizard_or_a_vna():
    state, motion = make_state()
    assert state.studio['planner'] is None
    assert state.studio['vna'] is None

    report = handlers.studio_move_to_gamma_only(state, BODY)

    assert report['measured'] is False
    assert report['validation_saved'] is False
    assert len(report['position']) == 3
    assert len(motion.commands) == 1
    np.testing.assert_allclose(
        motion.commands[0], THREE_AXIS_POSITIONS[1], atol=1e-9)
    # Nothing outside the motion path was touched.
    assert state.studio['vna'] is None
    assert state.studio['database'].saved == []
    assert state.studio['validation_report'] is None
    # The library now owns a planner built from its own geometry.
    planner = state.studio['planner']
    assert isinstance(planner, MotionPlanner)
    assert planner.config.probes == 2
    assert planner.controller is motion
    assert state.studio['config'].probes == 2
    assert state.studio['motion'] is motion


def test_a_second_move_reuses_the_library_planner():
    state, motion = make_state()
    handlers.studio_move_to_gamma_only(state, BODY)
    planner = state.studio['planner']
    handlers.studio_move_to_gamma_only(state, BODY)
    assert state.studio['planner'] is planner
    assert len(motion.commands) == 2


def test_a_wizard_planner_with_matching_axes_is_kept():
    state, motion = make_state()
    wizard_config = TunerConfig(
        probes=2, x_limits=(0., 12.), z_limits=(-1., 6.), safe_z=0.,
        fixed_offsets=(0., 0.))
    wizard = MotionPlanner(wizard_config, motion)
    state.studio.update(planner=wizard, motion=motion, config=wizard_config)
    planner, bound_motion = handlers._ensure_library_motion(state)
    assert planner is wizard
    assert bound_motion is motion
    assert state.studio['config'] is wizard_config


def test_switching_libraries_rebuilds_the_motion_config():
    state, motion = make_state()
    handlers.studio_move_to_gamma_only(state, BODY)
    first = state.studio['planner']
    state.studio['database_fingerprint'] = 'fp-b'
    state.studio['data']['metadata'] = {'config': {
        'probes': 2, 'x_limits': [0., 20.], 'z_limits': [0., 9.],
        'safe_z': 0., 'z0': 50.}}
    handlers._ensure_library_motion(state)
    assert state.studio['planner'] is not first
    assert state.studio['config'].x_limits[1] >= 20.0
    assert state.studio['config'].z_limits[1] >= 9.0
    assert state.studio['planner_library_fingerprint'] == 'fp-b'


def test_verified_move_still_requires_a_vna():
    """Only the move-only action is freed from the wizard, not verification."""
    state, motion = make_state()
    with pytest.raises(RuntimeError, match='instrument'):
        handlers.studio_move_to_gamma(state, BODY)
    assert motion.commands == []


# --------------------------------------------------------------- guards
def test_a_dimension_mismatch_is_refused_before_any_move():
    state, motion = make_state(axes=4)  # 4-axis controller, 3-axis library
    with pytest.raises(RuntimeError, match='3 轴'):
        handlers.studio_move_to_gamma_only(state, BODY)
    assert motion.commands == []


def test_a_disconnected_controller_has_an_actionable_message():
    state, _ = make_state(connect=False)
    with pytest.raises(RuntimeError, match='连接 Tuner 控制箱'):
        handlers.studio_move_to_gamma_only(state, BODY)


def test_a_library_without_positions_keeps_an_existing_planner():
    """Resumed wizard runs / minimal fixtures must not regress."""
    state, motion = make_state()
    stub = SimpleNamespace(move_to=lambda p: None)
    state.studio.update(planner=stub, motion=motion)
    state.studio['data'] = {'valid': np.array([True])}
    planner, bound = handlers._ensure_library_motion(state)
    assert planner is stub
    assert bound is motion


# --------------------------------------------------------------- other moves
def test_candidate_and_tuned_moves_also_work_off_the_library():
    state, motion = make_state()
    point = THREE_AXIS_POSITIONS[1]
    result = SimpleNamespace(position=point, gamma=0.2 + 0.6j, error=0.001)
    state.studio['last_candidates'] = [result]
    state.studio['last_candidates_fingerprint'] = 'fp-a'
    candidate = handlers.studio_move_candidate(state, {'index': 0})
    assert len(candidate['position']) == 3

    state.studio['last_tuned'] = {
        'position': list(point), 'gamma': {'re': 0.2, 'im': 0.6},
        'error': 0.001}
    state.studio['last_tuned_fingerprint'] = 'fp-a'
    tuned = handlers.studio_move_tuned(state, {})
    assert len(tuned['position']) == 3
    assert len(motion.commands) == 2
    assert state.studio['vna'] is None

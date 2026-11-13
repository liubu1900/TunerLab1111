"""Database append and the new web endpoints.

The web handlers use the process-wide :data:`tuner.web.state.state` singleton, so the
tests drive that object directly instead of going through HTTP.
"""
import numpy as np
import pytest

from tuner.database import CalibrationDatabase
from tuner.models import Measurement, TunerConfig
from tuner.operation_log import operation_log
from tuner.web import handlers
from tuner.web.state import state


# --------------------------------------------------------------- database
def test_append_positions_extends_the_same_calibration(tmp_path):
    db = CalibrationDatabase(tmp_path / 'test.h5')
    config = TunerConfig()
    db.create(config, [1e9, 2e9], [[0, 0, 0], [1, .5, .5]])
    measurement = Measurement(np.array([1e9, 2e9]), np.zeros((2, 2, 2), complex))
    db.write_point(0, [0, 0, 0], measurement)

    indices = db.append_positions([[2, .5, .5], [3, .5, .5]])
    assert indices == [2, 3]

    data = db.load()
    assert data['positions'].shape == (4, 3)
    assert data['valid'].tolist() == [True, False, False, False]
    assert data['s'].shape == (4, 2, 2, 2)
    # The already measured point survived the rewrite.
    assert np.allclose(data['s'][0], 0)
    assert db.pending_indices() == [1, 2, 3]
    statuses = {point: status for point, status, *_ in db.task_status()}
    assert statuses[3] == 'pending'
    assert data['metadata']['appended'] == 2


def test_append_positions_validates_the_new_points(tmp_path):
    db = CalibrationDatabase(tmp_path / 'test.h5')
    db.create(TunerConfig(), [1e9], [[0, 0, 0]])
    with pytest.raises(ValueError):
        db.append_positions([[99, 0, 0]])


def test_append_positions_rejects_a_different_axis_count(tmp_path):
    db = CalibrationDatabase(tmp_path / 'test.h5')
    db.create(TunerConfig(), [1e9], [[0, 0, 0]])
    with pytest.raises(ValueError):
        db.append_positions([[0, 0]])


# ------------------------------------------------------------- operation log
def test_oplog_endpoints_return_and_clear_entries():
    operation_log.clear()
    operation_log.info('运动', '点动', 'X +0.1')
    result = handlers.oplog_recent(state, {'limit': 50})
    assert result['entries'][-1]['action'] == '点动'
    assert result['count'] >= 1

    handlers.oplog_clear(state, {})
    remaining = handlers.oplog_recent(state, {'limit': 50})['entries']
    # The clear itself is logged, but the previous entry is gone.
    assert all(entry['action'] != '点动' for entry in remaining)


def test_oplog_export_writes_a_file(tmp_path):
    operation_log.clear()
    operation_log.info('系统', '导出测试')
    target = tmp_path / 'ops.json'
    result = handlers.oplog_export(state, {'path': str(target)})
    assert result['path'] == str(target)
    assert target.is_file()


def test_oplog_export_requires_a_path():
    with pytest.raises(ValueError):
        handlers.oplog_export(state, {})


# --------------------------------------------------------- point planning
def test_plan_preview_returns_the_uniform_target_set():
    result = handlers.studio_plan_preview(state, {'density': 'low'})
    assert result['count'] == 95
    assert len(result['targets']) == 95
    assert result['densities']['medium'] == 181
    for target in result['targets']:
        radius = (target['re'] ** 2 + target['im'] ** 2) ** 0.5
        assert radius <= 0.95 + 1e-9


def test_plan_preview_rejects_an_unknown_density():
    with pytest.raises(ValueError):
        handlers.studio_plan_preview(state, {'density': 'ultra'})


def test_plan_refine_needs_measured_points():
    state.studio['database'] = None
    with pytest.raises(RuntimeError):
        handlers.studio_plan_refine(state, {'density': 'low'})


def test_plan_refine_appends_points_to_the_current_database(tmp_path):
    """End-to-end: measured points -> model -> refinement -> appended points."""
    operation_log.clear()
    config = TunerConfig(probes=2, fixed_offsets=(0.0, 2.0))
    db = CalibrationDatabase(tmp_path / 'refine.h5')
    positions = [[0.0, 0.0, 0.0], [1.0, 0.2, 0.2], [2.0, 0.4, 0.4],
                 [3.0, 0.6, 0.6], [4.0, 0.8, 0.8], [5.0, 0.3, 0.3]]
    db.create(config, [1e9, 2e9], positions)
    # A deterministic, non-trivial response so the model has something to learn.
    for index, position in enumerate(positions):
        s = np.zeros((2, 2, 2), complex)
        s[:, 0, 0] = complex(0.05 * (index + 1), -0.02 * index)
        s[:, 1, 0] = complex(0.9, 0.1)
        db.write_point(index, position, Measurement(np.array([1e9, 2e9]), s))

    state.studio['database'] = db
    state.studio['data'] = db.load()
    state.studio['config'] = config
    state.studio['frequency_hz'] = 1e9
    from tuner.search import GammaSearchEngine
    state.studio['search_engine'] = GammaSearchEngine(state.studio['data'], z0=50.0)
    try:
        result = handlers.studio_plan_refine(state, {'density': 'low'})
        assert result['added'] == 95
        assert len(result['positions']) == 95
        assert result['pending'] == 95
        # The new points joined the same calibration.
        assert db.load()['positions'].shape == (101, 3)
        assert len(db.pending_indices()) == 95
    finally:
        state.studio['database'] = None
        state.studio['data'] = None
        state.studio['search_engine'] = None
        state.studio['config'] = None


# ------------------------------------------------- interpolated tuning (web)
def _tensor_calibration(tmp_path, nx=9, nz1=3, nz2=3):
    """A calibration database on a complete three-axis tensor grid.

    The grid has to be *complete*: every combination of the per-axis levels must
    be present, otherwise the tensor interpolation is impossible and the scattered
    interpolator takes over.
    """
    from tuner.models import Measurement
    config = TunerConfig(probes=2, fixed_offsets=(0.0, 2.0))
    db = CalibrationDatabase(tmp_path / 'tune.h5')
    x_levels = np.linspace(0.0, 10.0, nx)
    z1_levels = np.linspace(0.0, 1.0, nz1)
    z2_levels = np.linspace(0.0, 1.0, nz2)
    positions = [[x, z1, z2] for x in x_levels for z1 in z1_levels for z2 in z2_levels]
    db.create(config, [1e9, 2e9], positions)
    for index, (x, z1, _z2) in enumerate(positions):
        s = np.zeros((2, 2, 2), complex)
        # Gamma = r(Z1) * exp(j*phase(X)); with no termination this is S11.
        s[:, 0, 0] = (0.05 + 0.9 * z1) * np.exp(1j * 2 * np.pi * x / 10.0)
        s[:, 1, 0] = 0.9
        db.write_point(index, [x, z1, _z2], Measurement(np.array([1e9, 2e9]), s))
    return db


def test_studio_tune_uses_the_interpolated_model(tmp_path):
    db = _tensor_calibration(tmp_path)
    handlers._load_database(state, db)
    try:
        result = handlers.studio_tune(state, {
            'frequency': 1e9, 'R': 30.0, 'X': 0.0, 'method': 'two-stage'})
        assert result['method'] == 'two-stage'
        tuned = result['tuned']
        assert tuned['interpolator'] == 'tensor'
        assert [stage['stage'] for stage in tuned['stages']] == [
            'general_search', 'gradient_search', 'fine_grid']
        # The refinement must not be worse than the general search.
        assert tuned['error'] <= tuned['stages'][0]['error'] + 1e-12
        assert tuned['error'] < 0.05
        assert len(tuned['position']) == 3
        # The result must be JSON-serialisable, which the server requires.
        import json
        json.dumps(result)
    finally:
        with state.studio_lock:
            state.studio['database'] = None
            state.studio['data'] = None
            state.studio['search_engine'] = None


def test_studio_tune_can_skip_the_interpolation(tmp_path):
    db = _tensor_calibration(tmp_path)
    handlers._load_database(state, db)
    try:
        result = handlers.studio_tune(state, {
            'frequency': 1e9, 'R': 30.0, 'X': 0.0, 'method': 'nearest'})
        assert result['method'] == 'nearest'
        assert result['tuned'] is None
        assert result['candidates']
    finally:
        with state.studio_lock:
            state.studio['database'] = None
            state.studio['data'] = None
            state.studio['search_engine'] = None


def test_studio_tune_rejects_an_unknown_method(tmp_path):
    db = _tensor_calibration(tmp_path)
    handlers._load_database(state, db)
    try:
        with pytest.raises(ValueError):
            handlers.studio_tune(state, {'frequency': 1e9, 'R': 30.0,
                                         'method': 'magic'})
    finally:
        with state.studio_lock:
            state.studio['database'] = None
            state.studio['data'] = None
            state.studio['search_engine'] = None


def test_loading_new_database_clears_stale_tuned_result(tmp_path):
    """R5 / P0-07: opening a different calibration must invalidate last_tuned."""
    db1 = _tensor_calibration(tmp_path / 'a')
    db2 = _tensor_calibration(tmp_path / 'b')
    handlers._load_database(state, db1)
    handlers.studio_tune(state, {'frequency': 1e9, 'R': 30.0, 'X': 0.0,
                                 'method': 'two-stage'})
    with state.studio_lock:
        assert state.studio['last_tuned'] is not None
    # Re-opening another database clears the stale tuned position.
    handlers._load_database(state, db2)
    with state.studio_lock:
        assert state.studio['last_tuned'] is None
    # And moving to a result whose fingerprint no longer matches is refused.
    with state.studio_lock:
        state.studio['last_tuned'] = {'position': [0.0, 0.0, 0.0], 'gamma': 1.0,
                                     'error': 0.0}
        state.studio['last_tuned_fingerprint'] = 'stale'
        state.studio['database_fingerprint'] = 'current'
    with pytest.raises(RuntimeError, match='stale'):
        handlers.studio_move_tuned(state, {})
    with state.studio_lock:
        state.studio['database'] = None
        state.studio['data'] = None
        state.studio['search_engine'] = None


def test_studio_motion_is_blocked_while_calibrating():
    """P0-06: geometry / motion ops are refused while a sweep owns the hardware."""
    s = state.studio
    s['motion'] = object()  # pretend virtual hardware is initialised
    s['calibration_state'] = 'running'
    try:
        with pytest.raises(RuntimeError, match='calibration is running'):
            handlers.studio_jog(state, {'axis': 'Z1', 'delta': 0.1})
    finally:
        s['motion'] = None
        s['calibration_state'] = 'idle'


# -------------------------------------------- interpolation validation (web)
def test_studio_interpolation_validation_runs_and_reports(tmp_path):
    """The full loop: plan interpolated points, measure them, judge the result."""
    import time
    from tuner.motion import MotionController, MotionPlanner
    from tuner.vna import MockVNAController

    db = _tensor_calibration(tmp_path)
    handlers._load_database(state, db)
    config = TunerConfig(probes=2, fixed_offsets=(0.0, 2.0))
    motion = MotionController(config)
    motion.initialize()
    motion.home()
    planner = MotionPlanner(config, motion)
    vna = MockVNAController(config, motion, np.array([1e9, 2e9]))
    vna.initialize()
    with state.studio_lock:
        state.studio['config'] = config
        state.studio['motion'] = motion
        state.studio['planner'] = planner
        state.studio['vna'] = vna
        state.studio['frequency_hz'] = 1e9
    try:
        started = handlers.studio_validate_interpolation(state, {
            'count': 3, 'tolerance': 0.5, 'strategy': 'coverage',
            'frequency': 1e9})
        assert started['state'] == 'validating_interpolation'
        assert started['points'] == 3
        for _ in range(200):
            report = handlers.studio_validation_report(state, {})
            if report:
                break
            time.sleep(0.1)
        assert report, 'the validation did not produce a report'
        assert report['kind'] == 'interpolation_validation'
        assert report['count'] == 3
        assert len(report['points']) == 3
        for point in report['points']:
            assert 'predicted_gamma' in point and 'measured_gamma' in point
            assert point['error'] >= 0.0
        assert report['verdict'] in ('pass', 'recalibration_recommended')
        # The report was written into the calibration database as well.
        assert db.load()['validation']
    finally:
        with state.studio_lock:
            for key in ('config', 'motion', 'planner', 'vna', 'database', 'data',
                        'search_engine', 'interpolation_report'):
                state.studio[key] = None
            state.studio['calibration_thread'] = None


def test_studio_interpolation_validation_needs_a_calibration():
    with state.studio_lock:
        state.studio['data'] = None
        state.studio['search_engine'] = None
    with pytest.raises(RuntimeError):
        handlers.studio_validate_interpolation(state, {'count': 2})


# --------------------------------------------- restart / resume an interrupted run
class _ResumeMotion:
    def __init__(self):
        self.position = np.zeros(3)

    def read_position(self):
        return self.position.copy()

    def move_to(self, p, timeout=None):
        self.position = np.asarray(p, float)

    def retract(self):
        self.position[1:] = 0


class _ResumeVNA:
    frequencies = np.array([5e9])

    def measure(self):
        return Measurement(self.frequencies, np.zeros((1, 2, 2), complex))


def test_studio_setup_attaches_to_an_existing_library(tmp_path):
    """After an app restart the same library must be resumable, not recreated."""
    config = TunerConfig()
    path = tmp_path / 'resume.h5'
    keys = ('config', 'motion', 'vna', 'manager', 'database', 'data',
            'calibration_state', 'calibration_progress')
    with state.studio_lock:
        previous = {key: state.studio.get(key) for key in keys}
        state.studio.update({'config': config, 'motion': _ResumeMotion(),
                             'vna': _ResumeVNA(), 'manager': None, 'database': None,
                             'data': None, 'calibration_state': 'idle'})
    try:
        body = {'plan': 'cartesian', 'x_count': 2, 'z_count': 2, 'path': str(path)}
        first = handlers.studio_setup(state, body)
        assert first['resumed'] is False
        db = state.studio['database']
        version = db.load()['metadata']['version_id']
        db.write_point(0, db.load()['positions'][0],
                       Measurement(np.array([5e9]), np.zeros((1, 2, 2), complex)))
        second = handlers.studio_setup(state, body)
        assert second['resumed'] is True
        assert second['valid'] == 1
        assert state.studio['manager'] is not None
        assert db.load()['metadata']['version_id'] == version
        assert state.studio['calibration_progress'] == (0, first['positions'])
        # A resumed library must also rebuild the in-memory model, otherwise the
        # Smith chart and the pick-and-move loop are dead after a restart.
        assert state.studio['data'] is not None
        assert state.studio['search_engine'] is not None
        assert state.studio['database_fingerprint']
        assert state.studio['calibration_state'] == 'ready'
        assert state.studio['manager'] is not None
        # The attached manager finishes the pending points without prepare().
        assert state.studio['manager'].run() == 'completed'
        assert db.load()['valid'].all()
    finally:
        with state.studio_lock:
            state.studio.update(previous)


def test_studio_setup_refuses_a_different_grid_on_an_existing_library(tmp_path):
    config = TunerConfig()
    path = tmp_path / 'different.h5'
    keys = ('config', 'motion', 'vna', 'manager', 'database', 'data',
            'calibration_state', 'calibration_progress')
    with state.studio_lock:
        previous = {key: state.studio.get(key) for key in keys}
        state.studio.update({'config': config, 'motion': _ResumeMotion(),
                             'vna': _ResumeVNA(), 'manager': None, 'database': None,
                             'data': None, 'calibration_state': 'idle'})
    try:
        handlers.studio_setup(state, {'plan': 'cartesian', 'x_count': 2,
                                      'z_count': 2, 'path': str(path)})
        with pytest.raises(ValueError, match='布点'):
            handlers.studio_setup(state, {'plan': 'cartesian', 'x_count': 3,
                                          'z_count': 2, 'path': str(path)})
        assert CalibrationDatabase(path).load()['positions'].shape[0] == 8
    finally:
        with state.studio_lock:
            state.studio.update(previous)


# ------------------------------------- Smith chart pick -> move -> verify (0.3.9)
from types import SimpleNamespace  # noqa: E402

from tuner.rf import gamma_to_z  # noqa: E402
from tuner.ui import smith_gamma_from_pixel  # noqa: E402


class _PickEngine:
    z0 = 50.0

    def __init__(self, gamma, position):
        self.gamma = complex(gamma)
        self.position = np.asarray(position, float)

    def search(self, frequency, target_gamma, k=5, tolerance=0.05):
        return [SimpleNamespace(
            index=0, position=self.position, gamma=self.gamma,
            impedance=gamma_to_z(self.gamma, self.z0),
            error=float(abs(self.gamma - target_gamma)), reachable=True)]

    def tune(self, *args, **kwargs):
        raise AssertionError('method=nearest must not invoke interpolation')


class _PickPlanner:
    def __init__(self, motion=None):
        self.commanded = None
        self.motion = motion

    def move_to(self, position):
        self.commanded = np.asarray(position, float).copy()
        if self.motion is not None:
            self.motion.move_to(self.commanded)


class _PickVNA:
    def __init__(self, gamma, simulated=False):
        self.frequencies = np.array([5e9])
        self.gamma = complex(gamma)
        self.simulated_flag = simulated

    def measure(self):
        s = np.zeros((1, 2, 2), complex)
        s[0, 0, 0] = self.gamma
        return Measurement(self.frequencies, s, simulated=self.simulated_flag)


class _PickDatabase:
    def __init__(self):
        self.saved = []

    def save_validation(self, report):
        self.saved.append(report)


def _install_pick_state(measured_gamma, simulated=False):
    previous = dict(state.studio)
    motion = _ResumeMotion()
    planner = _PickPlanner(motion)
    database = _PickDatabase()
    gamma = 0.6 + 0.2j
    position = [3.75, 0.0, 1.0]
    state.studio.update({
        'config': None, 'motion': motion, 'planner': planner,
        'vna': _PickVNA(measured_gamma, simulated), 'database': database,
        'data': {'valid': np.array([True])},
        'search_engine': _PickEngine(gamma, position),
        'calibration_state': 'idle', 'frequency_hz': 5e9,
        'last_candidates': [], 'last_tuned': None,
        'last_candidates_fingerprint': None, 'last_tuned_fingerprint': None,
        'last_target': None, 'last_selected': None,
        'validation_report': None, 'database_fingerprint': None,
    })
    return previous, planner, database, gamma, position


def test_smith_gamma_from_pixel_maps_the_unit_circle():
    width = height = 500
    radius = (min(width, height) - 36) / 2
    assert smith_gamma_from_pixel(250, 250, width, height) == pytest.approx(0j)
    assert smith_gamma_from_pixel(250 + radius, 250, width, height) == pytest.approx(1 + 0j)
    assert smith_gamma_from_pixel(250, 250 - radius, width, height) == pytest.approx(1j)
    assert smith_gamma_from_pixel(0, 0, width, height) is None
    assert smith_gamma_from_pixel(250, 250, 10, 10) is None


def test_studio_move_to_gamma_reports_arrived_impedance_gamma_and_vswr():
    measured = 0.61 + 0.21j
    previous, planner, database, target, position = _install_pick_state(measured)
    try:
        report = handlers.studio_move_to_gamma(state, {
            'gamma_re': target.real, 'gamma_im': target.imag,
            'frequency': 5e9, 'method': 'nearest', 'tolerance': 0.05,
            'settle_s': 0,
        })
        arrived_z = gamma_to_z(measured)
        target_z = gamma_to_z(target)
        assert report['kind'] == 'smith_pick_move_verification'
        assert np.allclose(report['position'], position)
        assert np.allclose(planner.commanded, position)
        assert report['position_error_max'] == pytest.approx(0.0)
        assert report['prediction_kind'] == 'calibrated point'
        assert report['measured_gamma_real'] == pytest.approx(measured.real)
        assert report['measured_gamma_imag'] == pytest.approx(measured.imag)
        assert report['measured_impedance_real'] == pytest.approx(arrived_z.real)
        assert report['measured_impedance_imag'] == pytest.approx(arrived_z.imag)
        assert report['measured_vswr'] == pytest.approx(
            (1 + abs(measured)) / (1 - abs(measured)))
        assert report['target_vswr'] == pytest.approx(
            (1 + abs(target)) / (1 - abs(target)))
        assert report['impedance_delta_r'] == pytest.approx(arrived_z.real - target_z.real)
        assert report['vector_error'] == pytest.approx(abs(measured - target))
        assert report['within_tolerance'] is True
        assert report['rf_hardware_verified'] is True
        assert report['simulated'] is False
        assert 'Simulated RF verification only' not in report['interpretation']
        assert database.saved and database.saved[-1] is report
        assert state.studio['validation_report'] is report
    finally:
        state.studio.clear()
        state.studio.update(previous)


def test_studio_move_to_gamma_rejects_a_point_outside_the_chart():
    previous, _, _, _, _ = _install_pick_state(0.61 + 0.21j)
    try:
        with pytest.raises(ValueError, match='outside'):
            handlers.studio_move_to_gamma(state, {
                'gamma_re': 1.2, 'gamma_im': 0.0, 'frequency': 5e9,
                'method': 'nearest', 'settle_s': 0})
    finally:
        state.studio.clear()
        state.studio.update(previous)


def test_studio_move_only_moves_without_measuring_or_validating():
    """「仅移动」commands the axes but leaves the VNA and audit trail alone."""
    previous, planner, database, target, position = _install_pick_state(0.61 + 0.21j)
    try:
        def forbidden_sweep():
            pytest.fail('move-only must not sweep the VNA')
        state.studio['vna'].measure = forbidden_sweep
        sentinel = {'kind': 'previous verification'}
        state.studio['validation_report'] = sentinel
        report = handlers.studio_move_to_gamma_only(state, {
            'gamma_re': target.real, 'gamma_im': target.imag,
            'frequency': 5e9, 'method': 'nearest', 'tolerance': 0.05,
            'settle_s': 0,
        })
        assert report['kind'] == 'smith_pick_move_only'
        assert report['measured'] is False
        assert report['validation_saved'] is False
        assert np.allclose(planner.commanded, position)
        assert np.allclose(report['position'], position)
        assert np.allclose(report['commanded_position'], position)
        assert report['position_error_max'] == pytest.approx(0.0)
        assert report['prediction_kind'] == 'calibrated point'
        # No measured Γ, no error vector, no pass/fail field: a move is not a
        # verification and must never be presented as one.
        for key in ('measured_gamma_real', 'vector_error', 'within_tolerance',
                    'measured_vswr'):
            assert key not in report
        assert database.saved == []
        assert state.studio['validation_report'] is sentinel
    finally:
        state.studio.clear()
        state.studio.update(previous)


def test_studio_move_only_works_without_a_connected_vna():
    previous, planner, _, target, position = _install_pick_state(0.61 + 0.21j)
    try:
        state.studio['vna'] = None
        state.studio['database'] = None
        report = handlers.studio_move_to_gamma_only(state, {
            'gamma_re': target.real, 'gamma_im': target.imag,
            'frequency': 5e9, 'method': 'nearest', 'settle_s': 0})
        assert np.allclose(planner.commanded, position)
        assert report['measured'] is False
    finally:
        state.studio.clear()
        state.studio.update(previous)


def test_studio_move_only_rejects_a_point_outside_the_chart():
    previous, _, _, _, _ = _install_pick_state(0.61 + 0.21j)
    try:
        with pytest.raises(ValueError, match='outside'):
            handlers.studio_move_to_gamma_only(state, {
                'gamma_re': 1.2, 'gamma_im': 0.0, 'frequency': 5e9,
                'method': 'nearest', 'settle_s': 0})
    finally:
        state.studio.clear()
        state.studio.update(previous)


def test_studio_move_only_route_is_registered():
    assert handlers.ROUTES['POST /api/studio/move_only'] is (
        handlers.studio_move_to_gamma_only)


def test_studio_verify_distinguishes_real_from_simulated_rf():
    measured = 0.61 + 0.21j
    target_z = gamma_to_z(0.6 + 0.2j)
    body = {'R': target_z.real, 'X': target_z.imag,
            'frequency': 5e9, 'tolerance': 0.05}
    for simulated, expected_flag in ((False, True), (True, False)):
        previous, _, database, _, _ = _install_pick_state(measured, simulated)
        try:
            report = handlers.studio_verify(state, body)
            assert report['measured_vswr'] == pytest.approx(
                (1 + abs(measured)) / (1 - abs(measured)))
            assert 'measured_impedance_real' in report
            assert 'target_gamma_mag' in report
            assert report['rf_hardware_verified'] is expected_flag
            assert report['simulated'] is simulated
            assert ('Simulated RF verification only' in report['interpretation']) is simulated
            assert database.saved[-1] is report
        finally:
            state.studio.clear()
            state.studio.update(previous)

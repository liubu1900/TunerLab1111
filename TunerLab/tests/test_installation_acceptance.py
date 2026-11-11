"""A01–A18 installation acceptance: no DLL, no network, no physical motion."""
import copy
import json
from dataclasses import replace
from unittest import mock

import numpy as np
import pytest

from tuner.settings import SettingsStore, TunerSettings
from tuner.tuner_units import layout_units, TunerUnit
from tuner.ui_hardware import HardwareSession
from tuner.layout_session import HARDWARE_FIELDS
from tuner.zmotion_motion import ZmotionMotionController, build_tuner_config
from tuner.motion import MotionError
from tuner.web import handlers


class RecordingController:
    """Six independent physical axis parameter banks and recorded I/O."""
    connected = True
    firmware = 'TEST'
    ip = '192.168.0.11'
    def __init__(self, settings):
        self.calls = []
        self.failed_axis = None
        self.params = {i: {} for i in range(6)}
        self.homes = {i: 1 for i in range(6)}
        self.positions = {i: 0.0 for i in range(6)}
        for unit in settings.units.values():
            for name, i in unit.axis_map().items():
                self.params[i] = settings.unit_axes(unit.key)[name].to_dict()

    def __getattr__(self, attr):
        if attr.startswith('get_') and attr[4:] in HARDWARE_FIELDS:
            field = attr[4:]
            def get(i):
                self.calls.append(('read', i, field))
                return self.params[i][field]
            return get
        if attr.startswith('set_') and attr[4:] in HARDWARE_FIELDS:
            field = attr[4:]
            def set_value(i, value):
                self.calls.append(('write', i, field))
                if i == self.failed_axis and field == 'speed':
                    raise OSError('injected write failure')
                self.params[i][field] = value
            return set_value
        raise AttributeError(attr)

    def read_all_axis_info(self, count):
        return dict(idle=np.ones(count), status=np.zeros(count),
                    dpos=np.zeros(count), mpos=np.zeros(count))

    def home_status(self, i):
        return self.homes[i]

    def get_dpos(self, i):
        return self.positions[i]

    def get_mpos(self, i):
        return self.positions[i]

    def axis_stop_reason(self, i):
        return 0

    def open(self, *args, **kwargs):
        self.calls.append(('open',))
        self.connected = True

    def close(self):
        self.calls.append(('close',))
        self.connected = False

    def stop_axes(self, axes):
        self.calls.append(('stop', tuple(axes)))

    def move_absolute(self, i, value):
        self.calls.append(('move', i, value))
        self.positions[i] = value

    def wait_idle(self, axes, timeout):
        return True


def session(tmp_path, layout='dual_3', real=False):
    hw = HardwareSession(SettingsStore(tmp_path / 'settings.json'))
    hw.set_layout(layout)
    if real:
        link = RecordingController(hw.settings)
        unit = hw.active_unit
        hw.link = link
        hw.controller = ZmotionMotionController(
            build_tuner_config(hw.settings.axes, axis_count=unit.axis_count), link,
            hw.settings.axes, axis_count=unit.axis_count, axis_base=unit.axis_base,
            op_base=unit.op_base)
        hw.controller.initialized = True
        hw.controller.refresh_home_state()
        hw._bind_controller()
    return hw


@pytest.mark.parametrize('layout,unit,mapping', [
    ('single_3', 'tuner_1', {'X': 0, 'Z1': 1, 'Z2': 2}),
    ('dual_3', 'left', {'X': 0, 'Z1': 1, 'Z2': 2}),
    ('dual_3', 'right', {'X': 3, 'Z1': 4, 'Z2': 5}),
    ('single_4', 'tuner_1', {'X': 0, 'Z1': 1, 'Z2': 2, 'Z3': 3}),
])
def test_A01_A04_writes_and_moves_use_only_the_selected_physical_axes(tmp_path, layout, unit, mapping):
    hw = session(tmp_path, layout, real=True)
    hw.set_active_unit(unit)
    assert hw.controller.axis_map() == mapping
    assert set(hw.axes) == set(mapping)
    result = hw.apply_parameters_to_controller()
    assert all(r['status'] == 'matched' for r in result.values())
    assert {i for action, i, *_ in hw.link.calls if action == 'write'} == set(mapping.values())
    hw.controller._untrusted_home = set()
    hw.controller.refresh_home_state()
    hw.controller.move_axis_to('Z1', 0.1)
    assert ('move', mapping['Z1'], 0.1) in hw.link.calls


def test_A05_A06_profiles_and_parameter_states_remain_independent(tmp_path):
    hw = session(tmp_path, real=True)
    hw.set_active_unit('right')
    hw.read_parameters_from_controller()
    right = copy.deepcopy(hw.settings.axes)
    right_states = hw.parameter_states()
    hw.set_active_unit('left')
    hw.set_axis_parameters({'X': replace(hw.settings.axes['X'], speed=17.0)})
    assert hw.settings.unit_axes('right') == right
    hw.set_active_unit('right')
    assert hw.parameter_states() == right_states
    hw.disconnect()
    hw.set_layout('single_4')
    hw.set_axis_parameters({'Z3': replace(hw.settings.axes['Z3'], units=777.0)})
    hw.set_layout('dual_3')
    assert hw.settings.axes['X'].speed == right['X'].speed
    hw.set_layout('single_4')
    assert hw.settings.axes['Z3'].units == 777.0
    assert hw.store.load().profiles == hw.settings.profiles


def test_A09_A17_queued_task_blocks_switch_and_old_context_cannot_move(tmp_path):
    hw = session(tmp_path, real=True)
    state = type('State', (), {'hw': hw})()
    old = hw.context()
    called = []
    work = hw.reserve_task(lambda: called.append(hw.controller.axis_map()))
    with pytest.raises(MotionError, match='运行任务'):
        hw.set_active_unit('right')
    assert hw.stop_configured_axes() == list(range(6))
    work()
    assert called == [{'X': 0, 'Z1': 1, 'Z2': 2}]
    hw.set_active_unit('right')
    with pytest.raises(MotionError, match='版本'):
        handlers.hw_move(state, {'axis': 'X', 'target': 1, 'context': old})
    with pytest.raises(ValueError, match='缺少'):
        handlers.hw_move(state, {'axis': 'X', 'target': 1})
    assert not any(c[0] == 'move' for c in hw.link.calls)


def test_A10_rejects_overlapping_or_unsupported_installations_without_io(tmp_path):
    hw = session(tmp_path)
    original = hw.settings.to_dict()
    with pytest.raises(ValueError):
        hw.set_layout('left_4_right_3')
    assert hw.settings.to_dict() == original
    with pytest.raises(ValueError):
        hw.settings.update_unit(TunerUnit(key='left', axis_count=4))
    assert hw.settings.to_dict() == original


def test_A11_save_read_and_selected_write_are_separate(tmp_path):
    hw = session(tmp_path, real=True)
    hw.set_active_unit('right')
    hw.link.calls.clear()
    hw.set_axis_parameters({'Z1': replace(hw.settings.axes['Z1'], speed=23.0)})
    assert not hw.link.calls or all(c[0] != 'write' for c in hw.link.calls)
    saved = hw.settings.to_dict()
    live = hw.read_parameters_from_controller()
    assert hw.settings.to_dict() == saved
    assert live['Z1']['speed'] != 23.0
    hw.link.calls.clear()
    result = hw.apply_parameters_to_controller(['Z1'])
    assert result == {'Z1': {'status': 'matched'}}
    assert {c[1] for c in hw.link.calls if c[0] == 'write'} == {4}


def test_A12_partial_failure_preserves_per_axis_outcomes_and_blocks_motion(tmp_path):
    hw = session(tmp_path, real=True)
    hw.link.failed_axis = 1
    result = hw.apply_parameters_to_controller()
    assert {name: value['status'] for name, value in result.items()} == {
        'X': 'matched', 'Z1': 'failed', 'Z2': 'not_executed'}
    assert not any(c[0] == 'write' and c[1] == 2 for c in hw.link.calls)
    with pytest.raises(MotionError, match='参数'):
        hw.controller.move_axis_to('Z1', .1)


def test_A13_coordinates_invalidate_only_affected_axis_and_speed_preserves_home(tmp_path):
    hw = session(tmp_path, real=True)
    hw.set_axis_parameters({'X': replace(hw.settings.axes['X'], speed=19.0)})
    assert hw.controller.homed_axes == {'X', 'Z1', 'Z2'}
    hw.set_axis_parameters({'Z1': replace(hw.settings.axes['Z1'], units=9999.0)})
    hw.controller.refresh_home_state()
    assert hw.controller.homed_axes == {'X', 'Z2'}
    assert hw._calibration_stale


def test_A14_switch_preserves_left_home_without_writes_reopen_or_automatic_home(tmp_path):
    hw = session(tmp_path, real=True)
    left = hw.controller
    left.homed_axes = {'X', 'Z1'}
    hw.link.calls.clear()
    hw.set_active_unit('right')
    hw.set_active_unit('left')
    assert hw.controller is left
    assert hw.controller.homed_axes == {'X', 'Z1'}
    assert all(c[0] not in ('write', 'open', 'move') for c in hw.link.calls)


def test_A15_A18_connect_does_not_adopt_old_home_or_simulated_application_state(tmp_path):
    hw = session(tmp_path)
    hw.connect(mode='virtual')
    hw.read_parameters_from_controller()
    virtual = hw.link
    hw.disconnect()
    link = RecordingController(hw.settings)
    with mock.patch('tuner.ui_hardware.ZmotionController', return_value=link):
        hw.connect(mode='real')
    assert hw.link is link and hw.link is not virtual
    assert hw.parameter_states() == {}
    assert not hw.controller.refresh_home_state()
    assert all(not row['homed'] for row in hw.controller.axis_status_snapshot())


def test_A16_migration_backups_and_conflicts_preserve_original_data(tmp_path):
    store = SettingsStore(tmp_path / 'settings.json')
    legacy = {'schema_version': 5, 'axis_count': 4,
              'units': {'left': TunerUnit(key='left', axis_count=4).to_dict(),
                        'right': TunerUnit(key='right', axis_base=3).to_dict()},
              'axes_by_unit': {'left': {'X': {'name': 'X', 'units': 1234.0}},
                               'right': {'X': {'name': 'X', 'units': 5678.0}}},
              'vna_dialect': {'fetch_mnemonic': 'SDATA'}, 'custom': {'preserve': True}}
    original = json.dumps(legacy).encode()
    store.path.write_bytes(original)
    settings = store.load()
    assert settings.configuration_error
    assert settings.legacy_config == legacy
    assert store.path.with_name('settings.json.pre-schema6.bak').read_bytes() == original
    assert settings.profiles['dual_3']['left']['X'].units == 1234
    assert settings.profiles['dual_3']['right']['X'].units == 5678
    with pytest.raises(ValueError):
        store.save(settings)
    assert store.path.read_bytes() == original
    settings.set_layout('dual_3')
    store.save(settings)
    assert store.load().vna_dialect == {'fetch_mnemonic': 'SDATA'}
    assert store.load().legacy_config['custom'] == {'preserve': True}


def test_A16_failed_commit_leaves_file_and_runtime_unchanged(tmp_path):
    hw = session(tmp_path)
    original = hw.settings.to_dict()
    file = hw.store.path.read_bytes()
    with mock.patch('tuner.settings.os.replace', side_effect=OSError('disk full')):
        with pytest.raises(OSError):
            hw.set_layout('single_4')
    assert hw.settings.to_dict() == original
    assert hw.store.path.read_bytes() == file


def test_A09_stop_api_does_not_wait_for_the_motion_lock(tmp_path):
    import threading
    import time
    hw = session(tmp_path, real=True)
    entered, release = threading.Event(), threading.Event()
    normal_lock = threading.RLock()
    state = type('State', (), {'hw': hw, 'hw_lock': normal_lock,
                              'hw_emergency_lock': threading.Lock()})()
    def running_move():
        with normal_lock:
            entered.set()
            release.wait(3)
    thread = threading.Thread(target=running_move)
    thread.start()
    assert entered.wait(1)
    try:
        started = time.monotonic()
        handlers.hw_stop(state, {})
        assert time.monotonic() - started < .5
        assert ('stop', tuple(range(6))) in hw.link.calls
    finally:
        release.set()
        thread.join(2)


def test_A17_previous_process_context_is_not_reused_after_restart(tmp_path):
    first = session(tmp_path)
    second = HardwareSession(first.store)
    assert first.context()['controller_id'] == second.context()['controller_id']
    with pytest.raises(MotionError, match='版本'):
        second.check_context(first.context())


def test_A13_only_the_affected_axis_is_blocked_during_independent_motion(tmp_path):
    hw = session(tmp_path, real=True)
    hw.read_parameters_from_controller()
    hw.set_axis_parameters({'Z1': replace(hw.settings.axes['Z1'], units=999.0)})
    work = hw.reserve_task(lambda: hw.controller.move_axis_to('X', .1), motion=True, axes=['X'])
    work()
    assert ('move', 0, .1) in hw.link.calls
    with pytest.raises(MotionError, match='Z1'):
        hw.reserve_task(lambda: None, motion=True, axes=['Z1'])


def test_A16_invalid_file_is_backed_up_without_becoming_a_valid_installation(tmp_path):
    path = tmp_path / 'invalid.json'
    original = b'{invalid config'
    path.write_bytes(original)
    hw = HardwareSession(SettingsStore(path))
    assert hw.settings.configuration_error
    assert path.with_name('invalid.json.invalid.bak').read_bytes() == original
    with pytest.raises(MotionError, match='确认'):
        hw.connect(mode='real')
    assert path.read_bytes() == original


def test_http_workflow_preserves_context_and_save_does_not_write_hardware(tmp_path):
    import threading
    from urllib.request import urlopen, Request
    from tuner.web.state import AppState
    from tuner.web.server import make_server
    state = AppState()
    state.hw = session(tmp_path)
    server = make_server('127.0.0.1', 0, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(f'http://127.0.0.1:{server.server_port}/api/hardware/{path}',
                      data=data, headers={'Content-Type': 'application/json'})
        with urlopen(req, timeout=3) as response:
            return json.load(response)
    try:
        settings = request('settings')['data']
        result = request('layout', {'layout_id': 'single_4', 'context': settings['context']})
        assert result['ok']
        context = result['data']['context']
        assert not request('params/save', {'axes': {'X': {'speed': 17}}})['ok']
        result = request('params/save', {'axes': {'X': {'speed': 17}}, 'context': context})
        assert result['ok']
        assert not state.hw.connected
        assert request('params')['data']['axes']['X']['speed'] == 17
        assert state.hw.store.load().axes['X'].speed == 17
        assert not request('connect', {'mode': 'virtual', 'context': context})['ok']  # Stale save version.
        result = request('connect', {'mode': 'virtual', 'context': result['data']['context']})
        assert result['ok']
        context = result['data']['context']
        assert request('move', {'axis': 'Z3', 'target': .1, 'context': context})['ok']
        assert not request('move', {'axis': 'Z4', 'target': .1, 'context': context})['ok']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_A11_software_homing_settings_require_explicit_apply_not_just_hardware_read(tmp_path):
    hw = session(tmp_path, real=True)
    hw.read_parameters_from_controller()
    old_output = hw.controller.output('X')
    hw.set_axis_parameters({'X': replace(hw.settings.axes['X'], home_output=8)})
    hw.read_parameters_from_controller()
    state = hw.parameter_states()['X']
    assert state['status'] == 'different'
    assert 'home_output' in state['software_differences']
    assert hw.controller.output('X') == old_output
    assert 'feedback_scale' not in state['live']  # Not a firmware readback field.
    assert hw.apply_parameters_to_controller(['X'])['X']['status'] == 'matched'
    assert hw.controller.output('X') == 8


def test_A13_verified_limits_refresh_runtime_geometry_without_resetting_timeout(tmp_path):
    hw = session(tmp_path, real=True)
    hw.controller.config.motion_timeout = 60
    hw.set_axis_parameters({'X': replace(hw.settings.axes['X'], rs_limit=0, fs_limit=4)})
    assert hw.apply_parameters_to_controller(['X'])['X']['status'] == 'matched'
    assert hw.controller.config.x_limits == (0, 4)
    assert hw.controller.config.motion_timeout == 60


def test_A16_malformed_profile_values_are_preserved_and_fail_closed(tmp_path):
    store = SettingsStore(tmp_path / 'settings.json')
    data = TunerSettings().to_dict()
    data['profiles']['dual_3']['left']['X'] = None
    raw = json.dumps(data).encode()
    store.path.write_bytes(raw)
    hw = HardwareSession(store)
    assert hw.settings.configuration_error
    assert store.path.read_bytes() == raw
    assert store.path.with_name('settings.json.invalid.bak').read_bytes() == raw
    with pytest.raises(MotionError):
        hw.connect(mode='real')


def test_A13_coordinate_staleness_does_not_leak_to_the_other_tuner(tmp_path):
    hw = session(tmp_path)
    hw._calibration_stale = False
    hw.set_active_unit('right')
    hw.set_axis_parameters({'X': replace(hw.settings.axes['X'], units=777.0)})
    assert hw._calibration_stale
    hw.set_active_unit('left')
    assert not hw._calibration_stale


def test_A09_connect_handshake_reserves_the_installation_before_link_open_returns(tmp_path):
    import threading
    hw = session(tmp_path)
    link = RecordingController(hw.settings)
    entered, release = threading.Event(), threading.Event()
    original_open = link.open
    def slow_open(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original_open(*args, **kwargs)
    link.open = slow_open
    errors = []
    def connect():
        try:
            hw.connect(mode='real')
        except Exception as exc:
            errors.append(exc)
    with mock.patch('tuner.ui_hardware.ZmotionController', return_value=link):
        thread = threading.Thread(target=connect)
        thread.start()
        assert entered.wait(1)
        try:
            with pytest.raises(MotionError, match='运行任务'):
                hw.set_layout('single_4')
            with pytest.raises(MotionError, match='运行任务'):
                hw.set_active_unit('right')
        finally:
            release.set()
            thread.join(3)
    assert not errors
    assert hw.settings.layout_id == 'dual_3'
    assert hw.settings.active_unit == 'left'

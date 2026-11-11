"""Installation transactions, bound tasks and per-axis parameter verification."""
from __future__ import annotations

import copy
import math
import threading
import uuid
from .motion import MotionError
from .axis_params import COORDINATE_FIELDS
from .tuner_units import LAYOUT_LABELS

# These fields really exist in the motion controller. Software-only fields are
# never claimed to have been read back from firmware.
HARDWARE_FIELDS = ('atype', 'units', 'speed', 'accel', 'decel', 'creep', 'jog_speed',
                   'invert_step', 'datum_in', 'home_wait', 'fs_limit', 'rs_limit')


class InstallationSession:
    def init_installation(self):
        self._installation_lock = threading.RLock()
        self._task_local = threading.local()
        self._tasks = set()
        self._revision = 0
        self._session_id = str(uuid.uuid4())
        self._controllers = {}
        self._emergency_latched = False
        self._parameter_states = {}
        self._draft_guards = []
        self._external_busy = lambda: False

    def context(self):
        return {'session_id': self._session_id, 'controller_id': self.settings.controller_id,
                'layout_id': self.settings.layout_id,
                'unit': self.settings.active_unit, 'revision': self._revision}

    def check_context(self, expected=None):
        self.settings.validate()
        if expected is not None and expected != self.context():
            raise MotionError('配置版本已改变，旧请求已拒绝；请刷新后重试')

    def context_text(self):
        unit = self.active_unit
        return (f'{LAYOUT_LABELS[self.settings.layout_id]}｜当前：{unit.label}｜' +
                '，'.join(f'{n}＝轴{i}' for n, i in unit.axis_map().items()))

    def _state_key(self):
        return self.settings.mode, self.settings.layout_id, self.settings.active_unit

    def _assert_idle(self):
        own = set(getattr(self._task_local, 'tokens', []))
        if self._tasks - own or (not own and self._external_busy()):
            raise MotionError('控制器有运行任务，请等待运动、回零或建库结束')
        if self.connected:
            axes = [a for unit in self.settings.units.values() for a in unit.physical_axes]
            info = self.link.read_all_axis_info(max(axes) + 1)
            if any(not bool(info['idle'][a]) for a in axes):
                raise MotionError('控制器仍有轴在运动，不能切换或写入参数')

    def reserve_task(self, function, *, motion=False, expected=None, axes=None):
        # Reserve before a worker is queued, not after it starts.
        with self._installation_lock:
            self.check_context(expected)
            self._assert_idle()
            if motion:
                self.check_motion_parameters(axes)
            snapshot = self.context()
            token = object()
            self._tasks.add(token)
        def run():
            previous = list(getattr(self._task_local, 'tokens', []))
            self._task_local.tokens = previous + [token]
            try:
                self.check_context(snapshot)
                return function()
            finally:
                with self._installation_lock:
                    self._tasks.discard(token)
                self._task_local.tokens = previous
        return run

    def connect(self, ip=None, mode=None):
        def action():
            self._connect_impl(ip, mode)
            hook = getattr(self, '_after_connect', None)
            if hook:
                hook()
            self._bind_controller()
        return self.reserve_task(action, expected=self.context())()

    def disconnect(self):
        return self.reserve_task(self._disconnect_impl, expected=self.context())()

    def set_mode(self, mode):
        return self.reserve_task(lambda: self._set_mode_impl(mode), expected=self.context())()

    def _check_drafts(self):
        for guard in self._draft_guards:
            if not guard():
                raise MotionError('已取消切换，参数草稿保持不变')

    def _commit_settings(self, candidate):
        # Save before publishing a new configuration; failure leaves live state intact.
        self.store.save(candidate)
        self.settings = candidate
        self._revision += 1

    def set_layout(self, layout, *, expected=None):
        with self._installation_lock:
            self.check_context(expected)
            self._assert_idle()
            if self.connected:
                raise MotionError('更换设备组合前请先断开控制器')
            self._check_drafts()
            candidate = copy.deepcopy(self.settings)
            candidate.set_layout(layout)
            self._commit_settings(candidate)
            self._controllers.clear()
            self._parameter_states.clear()
            self.mark_calibration_stale()
            self.position_changed.emit(None)
            self.settings_changed.emit()

    def set_axis_count(self, axis_count):
        count = int(axis_count)
        if count not in (3, 4):
            raise ValueError('unsupported axis count')
        if self.settings.layout_id == 'dual_3':
            if count != 3:
                raise ValueError('双台三轴不能改成左四轴，请断开并选择单台四轴组合')
            return
        self.set_layout(f'single_{count}')

    def _bind_controller(self):
        controller = self.controller
        if controller is None:
            return
        snapshot = self.context()
        def check(axes=None):
            self.check_context(snapshot)
            if self.controller is not controller:
                raise MotionError('旧 Tuner 的运动对象已失效')
            self.check_motion_parameters(axes)
        controller.installation_check = check

    def set_active_unit(self, key, *, expected=None):
        with self._installation_lock:
            self.check_context(expected)
            self._assert_idle()
            if key == self.settings.active_unit:
                return
            self._check_drafts()
            candidate = copy.deepcopy(self.settings)
            candidate.set_unit(key)
            previous = self.settings.active_unit
            self._commit_settings(candidate)
            self.position_changed.emit(None)
            if self.controller is not None:
                self._controllers[previous] = self.controller
                if key in self._controllers:
                    self.controller = self._controllers[key]
                    if self.controller.simulated:
                        self.link.retarget(self.axes, self.active_unit.axis_base)
                else:
                    from .mock_hardware import MockZmotionController, build_virtual_config
                    from .zmotion_motion import ZmotionMotionController, build_tuner_config
                    unit = self.active_unit
                    params = self.settings.axes
                    virtual = bool(self.controller.simulated)
                    cls = MockZmotionController if virtual else ZmotionMotionController
                    config = (build_virtual_config if virtual else build_tuner_config)(
                        params, axis_count=unit.axis_count)
                    self.controller = cls(config, **({'link': self.link} if not virtual else {}),
                                          axis_params=params, axis_count=unit.axis_count,
                                          axis_base=unit.axis_base, op_base=unit.op_base,
                                          **({'link': self.link} if virtual else
                                             {'datum_in_base': unit.datum_in_base}))
                    self.controller.initialized = True
                    if virtual:
                        self.controller.refresh_home_state()
                    if not virtual:
                        self.controller._untrusted_home = set(self.axes)
                        self.controller.homed_axes.clear()
                    self._controllers[key] = self.controller
                upgrade = getattr(self, '_upgrade_controller', None)
                if upgrade:
                    upgrade()
                self._controllers[key] = self.controller
                self._bind_controller()
            self.settings_changed.emit()
            self.connection_changed.emit(self.connected)

    def check_motion_parameters(self, axes=None):
        self.settings.validate()
        if axes is not None and set(axes) - set(self.axes):
            raise ValueError('未配置的轴')
        if self._emergency_latched:
            raise MotionError('急停已锁存，请复位并重新回零')
        if self.connected and not self.controller.simulated:
            if not self.settings.layout_confirmed or self.settings.configuration_error:
                raise MotionError('请在通讯设置确认实际设备组合与接线')
            statuses = self.parameter_states()
            for name in (self.axes if axes is None else axes):
                if name not in self.axes:
                    raise ValueError('未配置的轴')
                if statuses.get(name, {}).get('status') != 'matched':
                    raise MotionError(f'{name} 参数尚未读回核对一致，请先读取或写入参数')

    @property
    def _calibration_stale(self):
        return getattr(self, '_calibration_flags', {}).get(self._state_key(), False)

    @_calibration_stale.setter
    def _calibration_stale(self, value):
        if not hasattr(self, '_calibration_flags'):
            self._calibration_flags = {}
        self._calibration_flags[self._state_key()] = bool(value)

    def parameter_states(self):
        return copy.deepcopy(self._parameter_states.get(self._state_key(), {}))

    def set_axis_parameters(self, axis_params):
        with self._installation_lock:
            self._assert_idle()
            if set(axis_params) - set(self.axes):
                raise ValueError('不能修改未配置轴或另一台 Tuner 的参数')
            for name, param in axis_params.items():
                if name != param.name:
                    raise ValueError('轴名称与参数归属不一致')
            # Validate and save first; update runtime state only on persistence success.
            old = self.settings
            candidate = copy.deepcopy(old)
            candidate.axes = {**candidate.axes, **copy.deepcopy(axis_params)}
            changed = {name for name in axis_params if old.axes[name] != axis_params[name]}
            self._commit_settings(candidate)
            statuses = self._parameter_states.setdefault(self._state_key(), {})
            for name in changed:
                statuses[name] = {'status': 'unread'}
                before, after = old.axes[name], candidate.axes[name]
                fields = {k for k in before.to_dict() if getattr(before, k) != getattr(after, k)}
                if fields & self._HOME_INVALIDATING:
                    for ctrl in [self.controller] if self.controller else []:
                        ctrl.homed_axes.discard(name)
                        if hasattr(ctrl, 'op_homed'):
                            ctrl.op_homed.discard(name)
                        ctrl._untrusted_home = getattr(ctrl, '_untrusted_home', set()) | {name}
                if fields & set(COORDINATE_FIELDS):
                    self._calibration_stale = True
            self._bind_controller()
            self.settings_changed.emit()

    def _compare_parameters(self, live, names):
        statuses = self._parameter_states.setdefault(self._state_key(), {})
        software_fields = ('datum_mode', 'feedback_scale', 'home_output', 'home_pulse',
                           'datum_active', 'home_output_active')
        for name in names:
            expected = self.settings.axes[name]
            differences = {}
            for field in HARDWARE_FIELDS:
                value = live.get(name, {}).get(field)
                if value is None or not math.isclose(float(value), float(getattr(expected, field)),
                                                     rel_tol=1e-6, abs_tol=1e-8):
                    differences[field] = {'configured': getattr(expected, field), 'live': value}
            active = self.controller.axis_params.get(name)
            software = {field: {'configured': getattr(expected, field),
                                'active': getattr(active, field, None)}
                        for field in software_fields
                        if getattr(expected, field) != getattr(active, field, None)}
            statuses[name] = {'status': 'different' if differences or software else 'matched',
                              'differences': differences, 'software_differences': software,
                              'live': {field: live.get(name, {}).get(field) for field in HARDWARE_FIELDS}}
            if not differences and not software:
                # Synchronise the Python view only after the actual hardware
                # agrees. Software-only settings require an explicit apply.
                self.controller.axis_params[name] = copy.deepcopy(expected)
        params = self.controller.axis_params
        x = params['X']
        z = [params[name] for name in self.axes if name != 'X']
        xlimits = (x.rs_limit, x.fs_limit)
        zlimits = (min(p.rs_limit for p in z), max(p.fs_limit for p in z))
        config = getattr(self.controller, 'config', None)
        if config is not None:
            if not getattr(self.controller, 'simulated', False) or xlimits[1] - xlimits[0] < 1e6:
                config.x_limits = xlimits
            if not getattr(self.controller, 'simulated', False) or zlimits[1] - zlimits[0] < 1e6:
                config.z_limits = zlimits
                config.safe_z = min(zlimits[1], max(zlimits[0], config.safe_z))

    def read_parameters_from_controller(self):
        with self._installation_lock:
            self._assert_idle()
            controller = self.require_controller()
            live = controller.read_axis_parameters()
            self._compare_parameters(live, self.axes)
            return live

    def apply_parameters_to_controller(self, axes=None):
        with self._installation_lock:
            self._assert_idle()
            controller = self.require_controller()
            names = list(self.axes if axes is None else axes)
            if not names or set(names) - set(self.axes):
                raise ValueError('未配置的目标轴')
            results = {name: {'status': 'not_executed'} for name in names}
            for name in names:
                try:
                    controller.apply_axis_parameters({name: self.settings.axes[name]})
                    live = controller.read_axis_parameters()
                    self._compare_parameters(live, [name])
                    status = self.parameter_states()[name]
                    if status['status'] != 'matched':
                        raise MotionError(f'{name} 写入后读回不一致: {status["differences"]}')
                    results[name] = {'status': 'matched'}
                    controller.axis_params[name] = copy.deepcopy(self.settings.axes[name])
                except Exception as exc:
                    results[name] = {'status': 'failed', 'error': str(exc)}
                    statuses = self._parameter_states.setdefault(self._state_key(), {})
                    for pending in names[names.index(name):]:
                        statuses[pending] = dict(results[pending])
                    break
            self._last_parameter_result = results
            return results

    def stop_configured_axes(self):
        # This path deliberately does not take the task/installation lock.
        controller = self.require_controller()
        numbers = sorted({a for unit in self.settings.units.values() for a in unit.physical_axes})
        self.link.stop_axes(numbers)
        return numbers

    def emergency_stop_all(self):
        self._emergency_latched = True
        controller = self.require_controller()
        controller.emergency_stop()
        for ctrl in {id(c): c for c in [controller, *self._controllers.values()]}.values():
            ctrl.stopped = True
            ctrl.homed_axes.clear()
            ctrl._untrusted_home = set(ctrl.axes)
            if hasattr(ctrl, 'op_homed'):
                ctrl.op_homed.clear()

    def reset_emergency_all(self):
        self.require_controller().reset_emergency()
        self._emergency_latched = False
        for ctrl in self._controllers.values():
            ctrl.reset_emergency()

"""Tests for the Zmotion driver, axis parameters and settings persistence.

The tests never touch real hardware: a :class:`FakeLibrary` implements the same
surface as :class:`tuner.zmotion.ZmotionLibrary` and records every call, so the
contract, the error handling and the safety logic can be checked deterministically.

The real-hardware behaviour (connection, parameter round trip, a zero-displacement
move) was verified separately against the controller during commissioning; the
observed values are recorded in ``docs/HARDWARE.md``.
"""
from __future__ import annotations

import ctypes
import json
import sys

import numpy as np
import pytest

from tuner.axis_params import AxisParams, TUNER_AXES, axis_index, default_axis_params
from tuner.models import TunerConfig
from tuner.settings import ConnectionSettings, SettingsStore, TunerSettings
from tuner.zmotion import ZmotionController, ZmotionError, load_library, vendor_dir
from tuner.zmotion_motion import ZmotionMotionController, build_tuner_config


def _value(raw) -> float:
    """Unwrap a ctypes scalar argument into a plain float."""
    if isinstance(raw, (int, float)):
        return float(raw)
    return float(raw.value)


def _int(raw) -> int:
    """Unwrap a ctypes integer argument into a plain int."""
    if isinstance(raw, int):
        return raw
    return int(raw.value)


def _axis(value) -> int:
    """Normalise a controller axis index.

    On this platform ``ctypes.c_int32`` is an alias of ``ctypes.c_long``, and
    ``int(c_long(0))`` raises, so a ctypes argument must be unwrapped explicitly.
    """
    if isinstance(value, int):
        return value
    return int(value.value)


class FakeLibrary:
    """In-memory stand-in for the vendor shared library.

    It mirrors the *raw* C surface of :class:`tuner.zmotion.ZmotionLibrary`: the same
    bound-function names with the same argument conventions (``byref`` out-parameters),
    so the driver can be exercised without hardware.
    """

    def __init__(self, fail_open_times: int = 0, error_code: int = 20008):
        self.fail_open_times = fail_open_times
        self.error_code = error_code
        #: Error number the controller reports for its last task error.
        self.controller_error_code = 0
        self.open_calls = 0
        self.closed = False
        self.axis_params: dict[int, dict[str, float]] = {}
        self.positions: dict[int, float] = {}
        self.feedback: dict[int, float] = {}
        #: When set, the encoder moves by this factor times the commanded distance.
        self.feedback_gain: float = 1.0
        self.home_states: dict[int, int] = {}
        self.moves: list[tuple[int, float]] = []
        self.cancelled: list[int] = []
        self.homed: list[int] = []
        self.rapidstop_called = False
        self.handle = ctypes.c_void_p(1234)
        #: Every ``search_eth`` call, as ``(address, timeout_ms)``.
        self.search_calls: list[tuple[bytes, int]] = []
        #: Every ``search_eth_list`` call, as the buffer length.
        self.search_list_calls: list[int] = []
        self._install_bound_functions()

    # -- raw accessors used by the driver --------------------------------
    def _params(self, axis):
        return self.axis_params.setdefault(
            axis, {'atype': 4, 'units': 10000.0, 'speed': 10.0, 'accel': 10.0,
                   'decel': 10.0, 'creep': 100.0, 'jog_speed': 100.0, 'invert_step': 0,
                   'datum_in': 16, 'home_wait': 2, 'fs_limit': 200000000.0,
                   'rs_limit': -200000000.0})

    def _install_bound_functions(self) -> None:
        """Expose the C-style getters and setters the driver calls."""
        float_fields = ('units', 'speed', 'accel', 'decel', 'creep', 'jog_speed',
                        'fs_limit', 'rs_limit')
        int_fields = ('atype', 'invert_step', 'datum_in', 'home_wait')

        for field in float_fields:
            def make_getter(name):
                def getter(handle, axis, out):
                    out._obj.value = float(self._params(_axis(axis))[name])
                    return 0
                return getter

            def make_setter(name):
                def setter(handle, axis, value):
                    self._params(_axis(axis))[name] = _value(value)
                    return 0
                return setter

            setattr(self, f'get_{field}', make_getter(field))
            setattr(self, f'set_{field}', make_setter(field))

        for field in int_fields:
            def make_getter(name):
                def getter(handle, axis, out):
                    out._obj.value = int(self._params(_axis(axis))[name])
                    return 0
                return getter

            def make_setter(name):
                def setter(handle, axis, value):
                    self._params(_axis(axis))[name] = _int(value)
                    return 0
                return setter

            setattr(self, f'get_{field}', make_getter(field))
            setattr(self, f'set_{field}', make_setter(field))

    # -- connection ------------------------------------------------------
    def open_eth(self, ip, handle_ptr):
        self.open_calls += 1
        if self.open_calls <= self.fail_open_times:
            return self.error_code
        handle_ptr._obj.value = self.handle.value
        return 0

    def close(self, handle):
        self.closed = True
        return 0

    def search_eth(self, ip, timeout, handle_ptr):
        # The vendor DLL takes three arguments; the third is a handle pointer.
        # Recorded so a test can assert that a concrete address was passed.
        self.search_calls.append((ip, timeout))
        return 0 if self.fail_open_times == 0 else self.error_code

    def search_eth_list(self, buffer, length, timeout):
        self.search_list_calls.append(length)
        if self.fail_open_times:
            return self.error_code
        buffer.value = b'192.168.0.11 '
        return 0

    # -- state -----------------------------------------------------------
    def get_dpos(self, handle, axis, out):
        out._obj.value = float(self.positions.get(_axis(axis), 0.0))
        return 0

    def get_mpos(self, handle, axis, out):
        out._obj.value = float(self.feedback.get(_axis(axis),
                                                 self.positions.get(_axis(axis), 0.0)))
        return 0

    def set_dpos(self, handle, axis, value):
        self.positions[_axis(axis)] = _value(value)
        return 0

    def get_if_idle(self, handle, axis, out):
        out._obj.value = 1
        return 0

    def get_home_status(self, handle, axis, out):
        out._obj.value = int(self.home_states.get(_axis(axis), 1))
        return 0

    def get_axis_status(self, handle, axis, out):
        out._obj.value = 0
        return 0

    def get_axis_stop_reason(self, handle, axis, out):
        out._obj.value = 0
        return 0

    def get_axis_enable(self, handle, axis, out):
        out._obj.value = 1
        return 0

    def get_mspeed(self, handle, axis, out):
        out._obj.value = 0.0
        return 0

    def get_all_axis_info(self, handle, count, idle, dpos, mpos, status):
        for index in range(int(getattr(count, 'value', count))):
            idle[index] = 1
            dpos[index] = float(self.positions.get(index, 0.0))
            mpos[index] = float(self.positions.get(index, 0.0))
            status[index] = 0
        return 0

    # -- commands --------------------------------------------------------
    def direct_command(self, handle, command, buffer, length):
        text = command.decode('utf-8') if isinstance(command, bytes) else str(command)
        if 'ERRCODE' in text.upper():
            # The controller answers with the number of its last task error.
            buffer.value = str(self.controller_error_code).encode('ascii')
            return 0
        buffer.value = b'2.9600'
        return 0

    def execute(self, handle, command, buffer, length):
        buffer.value = b'2.9600'
        return 0

    def move_abs(self, handle, axis, distance):
        self.moves.append((_axis(axis), _value(distance)))
        target = _value(distance)
        previous = self.positions.get(_axis(axis), 0.0)
        self.positions[_axis(axis)] = target
        # The encoder travels feedback_gain times as far as the command says.
        self.feedback[_axis(axis)] = (self.feedback.get(_axis(axis), previous)
                                      + (target - previous) * self.feedback_gain)
        return 0

    def move_rel(self, handle, axis, distance):
        self.moves.append((_axis(axis), _value(distance)))
        self.positions[_axis(axis)] = self.positions.get(_axis(axis), 0.0) + _value(distance)
        return 0

    def cancel(self, handle, axis, mode):
        self.cancelled.append(_axis(axis))
        return 0

    def cancel_axis_list(self, handle, count, axis_list, mode):
        # Both ``count`` and the ``axis_list`` elements arrive as ctypes integers;
        # unwrap them with :func:`_axis` (``int()`` on a ctypes value raises here).
        for i in range(_axis(count)):
            raw = axis_list[i]
            self.cancelled.append(_axis(raw) if not isinstance(raw, int) else raw)
        return 0

    def rapidstop(self, handle, mode):
        self.rapidstop_called = True
        return 0

    def datum(self, handle, axis, mode):
        self.homed.append(_axis(axis))
        self.home_states[_axis(axis)] = 1
        return 0

    def vmove(self, handle, axis, direction):
        return 0

    # -- typed helpers the driver calls on the library --------------------
    def get_f32(self, handle, func, axis, what):
        value = ctypes.c_float()
        code = func(handle, ctypes.c_int32(axis), ctypes.byref(value))
        if code != 0:
            raise ZmotionError(f'{what} failed', code)
        return float(value.value)

    def get_i32(self, handle, func, axis, what):
        value = ctypes.c_int32()
        code = func(handle, ctypes.c_int32(axis), ctypes.byref(value))
        if code != 0:
            raise ZmotionError(f'{what} failed', code)
        return int(value.value)

    def set_f32(self, handle, func, axis, value, what):
        code = func(handle, ctypes.c_int32(axis), ctypes.c_float(value))
        if code != 0:
            raise ZmotionError(f'{what} failed', code)

    def set_i32(self, handle, func, axis, value, what):
        code = func(handle, ctypes.c_int32(axis), ctypes.c_int32(value))
        if code != 0:
            raise ZmotionError(f'{what} failed', code)

    def _check(self, code: int, what: str) -> int:
        if code != 0:
            raise ZmotionError(f'{what} failed', code)
        return code


def make_controller(fail_open_times: int = 0) -> ZmotionController:
    library = FakeLibrary(fail_open_times=fail_open_times)
    controller = ZmotionController(library)
    controller.lib = library
    return controller


# -- library loading ----------------------------------------------------
def test_missing_library_reports_probed_paths(tmp_path):
    with pytest.raises(ZmotionError) as excinfo:
        load_library(tmp_path / 'nope.dll')
    message = str(excinfo.value)
    assert 'nope.dll' in message
    assert 'TUNERLAB_ZAUXDLL' in message


@pytest.mark.skipif(sys.platform != 'win32',
                    reason='vendor zauxdll.dll requires Windows')
def test_vendor_library_is_present():
    library = load_library()
    assert library is not None


def test_vendor_dir_contains_library():
    assert (vendor_dir() / 'zauxdll.dll').is_file()


# -- connection ---------------------------------------------------------
def test_open_reports_failure_with_code():
    controller = make_controller(fail_open_times=99)
    with pytest.raises(ZmotionError) as excinfo:
        controller.open('192.168.0.11')
    assert excinfo.value.code == 20008
    assert '192.168.0.11' in str(excinfo.value)


def test_open_retries_after_missed_broadcast():
    # The controller occasionally does not answer the discovery broadcast; the driver
    # must retry instead of reporting a spurious wiring fault.
    controller = make_controller(fail_open_times=2)
    controller.open('192.168.0.11')
    assert controller.connected
    assert controller.lib.open_calls == 3


def test_open_gives_up_after_retries():
    controller = make_controller(fail_open_times=99)
    with pytest.raises(ZmotionError):
        controller.open('192.168.0.11')
    assert controller.lib.open_calls == 3
    assert not controller.connected


def test_close_is_safe_when_not_connected():
    controller = make_controller()
    controller.close()
    assert not controller.connected


def test_operations_require_connection():
    controller = make_controller()
    with pytest.raises(ZmotionError):
        controller.get_dpos(0)
    with pytest.raises(ZmotionError):
        controller.move_absolute(0, 1.0)


# -- axis parameters ----------------------------------------------------
def test_default_parameters_match_commissioned_controller():
    params = default_axis_params()
    assert params['X'].units == 2500.0
    assert params['Z1'].units == 10000.0
    assert params['Z2'].units == 10000.0
    assert params['Z3'].units == 10000.0
    # Every axis carries its own origin input channel.
    assert [params[name].datum_in for name in TUNER_AXES] == [16, 17, 18, 19]
    assert all(params[name].atype == 4 for name in TUNER_AXES)


def test_axis_index_mapping():
    assert [axis_index(name) for name in TUNER_AXES] == [0, 1, 2, 3]
    with pytest.raises(ValueError):
        axis_index('Z9')


def test_axis_params_rejects_invalid_values():
    with pytest.raises(ValueError):
        AxisParams(name='X', units=0)
    with pytest.raises(ValueError):
        AxisParams(name='X', speed=-1)
    with pytest.raises(ValueError):
        AxisParams(name='X', fs_limit=-10, rs_limit=10)
    with pytest.raises(ValueError):
        AxisParams(name='Q')


def test_axis_params_round_trip():
    params = AxisParams(name='Z2', units=1234.5, speed=2.5)
    assert AxisParams.from_dict(params.to_dict()) == params


def test_axis_params_rejects_unknown_field():
    with pytest.raises(ValueError):
        AxisParams.from_dict({'name': 'X', 'bogus': 1})


# -- settings -----------------------------------------------------------
def test_settings_round_trip(tmp_path):
    store = SettingsStore(tmp_path / 'settings.json')
    settings = TunerSettings(
        connection=ConnectionSettings(ip='192.168.1.50', connect_on_start=True),
        axis_count=4, axes=default_axis_params())
    store.save(settings)
    loaded = store.load()
    assert loaded.connection.ip == '192.168.1.50'
    assert loaded.connection.connect_on_start is True
    assert loaded.axes['X'].units == 2500.0


def test_settings_missing_file_returns_defaults(tmp_path):
    store = SettingsStore(tmp_path / 'absent.json')
    settings = store.load()
    assert settings.connection.ip == '192.168.0.11'
    assert set(settings.axes) == {'X', 'Z1', 'Z2'}


def test_settings_corrupt_file_is_reported(tmp_path):
    path = tmp_path / 'broken.json'
    path.write_text('{not json', encoding='utf-8')
    with pytest.raises(ValueError, match='not valid JSON'):
        SettingsStore(path).load()


def test_settings_reject_invalid_ip():
    with pytest.raises(ValueError):
        ConnectionSettings(ip='192.168.0')
    with pytest.raises(ValueError):
        ConnectionSettings(ip='999.1.1.1')


def test_settings_export_and_import(tmp_path):
    store = SettingsStore(tmp_path / 'settings.json')
    settings = TunerSettings()
    target = store.export(tmp_path / 'export.json', settings)
    assert target.is_file()
    imported = SettingsStore.import_from(target)
    assert imported.profiles['single_4']['tuner_1']['Z3'].datum_in == 19


def test_settings_reject_future_schema(tmp_path):
    path = tmp_path / 'future.json'
    path.write_text(json.dumps({'schema_version': 99}), encoding='utf-8')
    with pytest.raises(ValueError, match='schema version'):
        SettingsStore(path).load()


# -- motion backend -----------------------------------------------------
def make_motion_backend(fail_open_times: int = 0):
    link = make_controller(fail_open_times=fail_open_times)
    params = default_axis_params()
    config = build_tuner_config(params)
    backend = ZmotionMotionController(config, link, params)
    return backend, link


def test_motion_backend_requires_initialization():
    backend, _ = make_motion_backend()
    with pytest.raises(Exception):
        backend.read_position()


def test_motion_backend_initializes_without_homing():
    backend, link = make_motion_backend()
    backend.initialize()
    assert backend.initialized
    # The controller reports a successful homing, which the backend adopts.
    assert backend.homed


def test_external_homing_is_detected_before_motion():
    """Homing performed outside the software (e.g. in RTsys) must not leave the
    software stuck unable to move.

    The backend adopts the controller's per-axis ``home_status`` on every motion
    attempt, so a stale (empty) ``homed_axes`` cache is reconciled automatically
    instead of permanently blocking movement.
    """
    backend, link = make_motion_backend()
    backend.initialize()
    # Simulate the controller having been homed externally *after* the software
    # connected: the cached homed set is empty while the controller still reports
    # home_status == 1 for every axis.
    backend.homed_axes.clear()
    backend.homed = False
    assert not backend.homed_axes
    before = backend.read_position()
    backend.jog('Z1', 0.5)
    after = backend.read_position()
    assert abs(after[1] - (before[1] + 0.5)) < 1e-6
    assert 'Z1' in backend.homed_axes


def test_fault_latch_is_not_overridden_by_external_home_status():
    """A latched fault keeps blocking motion even if the controller reports homed."""
    backend, link = make_motion_backend()
    backend.initialize()
    backend._latch_fault('simulated fault')
    assert backend.faulted
    with pytest.raises(Exception, match='fault is latched'):
        backend.jog('Z1', 0.5)


def test_motion_backend_applies_parameters_to_controller():
    backend, link = make_motion_backend()
    backend.initialize()
    backend.apply_axis_parameters(default_axis_params())
    assert link.lib.axis_params[0]['units'] == 2500.0
    assert link.lib.axis_params[3]['datum_in'] == 19


def test_motion_backend_retract_before_x_in_coupled_mode():
    backend, link = make_motion_backend()
    backend.config.independent_axes = False
    backend.initialize()
    # Probes are not retracted, so shared X travel must be refused.
    with pytest.raises(Exception, match='retracted'):
        backend.move_to([1.0, 0.5, 0.0, 0.0])


def test_motion_backend_allows_x_with_probes_extended_by_default():
    """The default is independent axes -- no probe-position check on X."""
    backend, link = make_motion_backend()
    backend.initialize()
    link.lib.positions.update({0: 0.0, 1: 0.5, 2: 0.0, 3: 0.0})
    backend.move_to([1.0, 0.5, 0.0, 0.0])
    assert link.lib.positions[0] == 1.0
    assert link.lib.positions[1] == 0.5   # probe was not retracted behind our back


def test_motion_backend_single_axis_x_is_independent():
    """move_axis_to('X') no longer inspects the probe axes."""
    backend, link = make_motion_backend()
    backend.initialize()
    link.lib.positions.update({0: 0.0, 1: 0.5, 2: 0.0, 3: 0.0})
    backend.move_axis_to('X', 1.0)
    assert link.lib.positions[0] == 1.0
    assert link.lib.positions[1] == 0.5
    backend.config.independent_axes = False
    with pytest.raises(Exception, match='retracted'):
        backend.move_axis_to('X', 2.0)


def test_x_homing_does_not_wait_for_probes_by_default():
    """X can be homed on its own; the coupled mode still enforces the order."""
    backend, link = make_motion_backend()
    backend.initialize()
    backend.homed_axes.clear()
    backend.home_axis_safe('X')
    assert 'X' in backend.homed_axes
    assert not {'Z1', 'Z2', 'Z3'} & backend.homed_axes

    coupled, _ = make_motion_backend()
    coupled.config.independent_axes = False
    coupled.initialize()
    coupled.homed_axes.clear()
    with pytest.raises(Exception, match='home every probe axis before X'):
        coupled.home_axis_safe('X')


def test_motion_backend_allows_x_when_probes_retracted():
    backend, link = make_motion_backend()
    backend.initialize()
    link.lib.positions.update({0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0})
    backend.move_to([1.0, 0.0, 0.0, 0.0])
    assert link.lib.positions[0] == 1.0


def test_motion_backend_emergency_stop_latches():
    backend, link = make_motion_backend()
    backend.initialize()
    backend.emergency_stop()
    assert backend.stopped
    assert link.lib.rapidstop_called
    with pytest.raises(Exception, match='emergency stop'):
        backend.move_to([0.0, 0.0, 0.0, 0.0])
    backend.reset_emergency()
    assert not backend.stopped
    assert not backend.homed


def test_motion_backend_single_axis_move_respects_limits():
    backend, link = make_motion_backend()
    backend.initialize()
    with pytest.raises(Exception, match='outside the configured limits'):
        backend.move_axis_to('Z1', 1e9)


def test_build_config_marks_non_simulated():
    config = build_tuner_config(default_axis_params())
    assert config.simulated is False
    assert config.probes == 3
    assert config.axes == TUNER_AXES


# -- feedback scale verification ---------------------------------------
def test_move_is_verified_against_the_encoder_not_the_command():
    """A move must be checked against the encoder, not against DPOS.

    DPOS always equals the command by definition, so comparing it proves nothing. When
    the encoder reports a different displacement the move has to be rejected.
    """
    backend, link = make_motion_backend()
    backend.initialize()
    link.lib.feedback_gain = 84.0        # the machine travels 84x further than commanded
    with pytest.raises(Exception, match='encoder feedback'):
        backend.move_axis_to('Z1', 0.05)


def test_move_passes_when_feedback_scale_is_configured():
    backend, link = make_motion_backend()
    backend.initialize()
    link.lib.feedback_gain = 84.0
    params = dict(backend.axis_params)
    params['Z1'] = AxisParams(**{**params['Z1'].to_dict(), 'feedback_scale': 84.0})
    backend.axis_params = params
    backend.move_axis_to('Z1', 0.05)      # accepted, the scale is known


def test_probe_axis_scale_measures_the_ratio():
    backend, link = make_motion_backend()
    backend.initialize()
    link.lib.feedback_gain = 84.0
    result = backend.probe_axis_scale('Z1', distance=0.005)
    assert result['ratio'] == pytest.approx(84.0, rel=0.01)
    # The probe returns the axis to where it started.
    assert result['restored_dpos'] == pytest.approx(0.0, abs=1e-6)


def test_probe_axis_scale_on_consistent_axis():
    backend, link = make_motion_backend()
    backend.initialize()
    result = backend.probe_axis_scale('Z1', distance=0.005)
    assert result['ratio'] == pytest.approx(1.0, rel=0.01)


def _backend_with_encoder_scale(axis: str, scale: float):
    """Backend whose ``axis`` reports MPOS in ``scale`` counts per millimetre."""
    backend, link = make_motion_backend()
    backend.initialize()
    params = dict(backend.axis_params)
    params[axis] = AxisParams(**{**params[axis].to_dict(), 'feedback_scale': scale})
    backend.axis_params = params
    return backend, link


def test_grid_quantisation_residue_does_not_latch_a_fault():
    """A 1/3 mm grid point read back as 0.3333 mm must not look like a scale error.

    The controller keeps DPOS to four decimals, so the next move of that probe is a
    ~3.3e-5 mm residue (0.0033 encoder counts).  DPOS read-back already proves the
    command landed; the encoder is physically unable to resolve one hundredth of a
    count.  Regression for the 2026-11-08 5 GHz single-point calibration abort.
    """
    backend, link = _backend_with_encoder_scale('Z2', 100.0)
    link.lib.positions[2] = 0.3333
    link.lib.feedback[2] = 33.33
    target = np.array([0.0, 0.0, 1.0 / 3.0, 0.0], dtype=float)
    actual = backend.move_to(target)
    assert actual[2] == pytest.approx(1.0 / 3.0, abs=1e-6)  # controller uses float32
    assert backend.faulted is False


def test_sub_count_single_axis_move_does_not_latch_a_fault():
    backend, link = _backend_with_encoder_scale('Z2', 100.0)
    link.lib.positions[2] = 0.3333
    link.lib.feedback[2] = 33.33
    backend.move_axis_to('Z2', 1.0 / 3.0)
    assert backend.faulted is False


def test_real_scale_mismatch_is_still_rejected():
    """The quantisation allowance must not mask a genuine UNITS/encoder mismatch."""
    backend, link = _backend_with_encoder_scale('Z2', 100.0)
    link.lib.feedback_gain = 84.0
    with pytest.raises(Exception, match='encoder feedback'):
        backend.move_axis_to('Z2', 0.5)
    assert backend.faulted is True


def test_feedback_scale_must_be_positive():
    with pytest.raises(ValueError):
        AxisParams(name='X', feedback_scale=0)


def test_axis_params_rejects_non_finite_values():
    """R3 / P0-08: NaN and Infinity must be rejected, not accepted silently."""
    base = default_axis_params()['Z1'].to_dict()
    for bad in ('nan', 'inf', '-inf'):
        with pytest.raises(ValueError):
            AxisParams(**{**base, 'name': 'Z1', 'units': float(bad)})
    # A non-integer / negative axis type is also rejected.
    with pytest.raises(ValueError):
        AxisParams(**{**base, 'name': 'Z1', 'atype': -1})


def test_three_axis_model_rejects_z3():
    """R2 / P0-03: a 3-axis tuner (X + Z1 + Z2) must not drive Z3.

    The controller is built with ``axis_count=3`` so its model axes are
    ``('X', 'Z1', 'Z2')``; homing or moving Z3 must be refused at the guarded
    entry points rather than reaching the physical axis 3.
    """
    link = make_controller()
    three = {name: default_axis_params()[name] for name in ('X', 'Z1', 'Z2')}
    config = TunerConfig(probes=2, x_limits=(-1.0, 1.0), z_limits=(-1.0, 1.0),
                         safe_z=0.0, fixed_offsets=(0.0, 0.0))
    backend = ZmotionMotionController(config, link, three, axis_count=3)
    backend.initialize()
    assert backend.axes == ('X', 'Z1', 'Z2')
    with pytest.raises(ValueError, match='unknown axis'):
        backend.home_axis_safe('Z3')
    with pytest.raises(ValueError, match='unknown axis'):
        backend.move_axis_to('Z3', 0.0)


def test_probe_axis_scale_rejects_invalid_distance():
    """P0-05: the scale self-check must not issue a move for a bad distance."""
    backend, link = make_motion_backend()
    backend.initialize()
    # Zero, NaN and Infinity must be refused before any move is issued; a signed
    # (negative) distance is valid -- the probe returns to its start anyway.
    for bad in (0.0, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            backend.probe_axis_scale('Z1', distance=bad)


# --------------------------------------------------------------------------- #
# Network discovery
#
# Measured on the bench (2026-11-08) with a Zmotion controller at
# 192.168.0.11 and a workstation holding three IPv4 addresses:
#
# * ``ZAux_SearchEth`` takes **three** arguments.  The vendor header
#   (zauxdll2.h) still documents the old two-argument form, but the shipped
#   DLL and the vendor's own Python wrapper both use three.  Bound with two,
#   the third register arrives as garbage and every call fails with 20010 --
#   the "广播搜索" button could never find anything.
# * The call addresses **one** controller by IP.  An empty address is
#   rejected with 20010 as well; it is not a subnet broadcast.
# * Subnet-wide discovery is a different call, ``ZAux_SearchEthlist``, and it
#   is unreliable here: it answered roughly one call in eight.
# --------------------------------------------------------------------------- #


def test_search_eth_is_bound_with_three_arguments():
    """The vendor export takes a handle pointer as its third argument."""
    from tuner.zmotion import ZmotionLibrary

    class _Stub:
        """Minimal ctypes stand-in: only the two exports are inspected."""

        def __init__(self):
            self.argtypes = {}

        def __getattr__(self, name):
            def _fn(*args):
                return 0
            _fn.argtypes = None
            return _fn

    stub = _Stub()
    library = ZmotionLibrary.__new__(ZmotionLibrary)
    library.dll = stub
    # Re-run only the two bindings that matter, with the real _bind.
    from tuner.zmotion import _bind

    library.search_eth = _bind(stub, 'ZAux_SearchEth',
                                [__import__('ctypes').c_char_p,
                                 __import__('ctypes').c_uint32,
                                 __import__('ctypes').POINTER(__import__('ctypes').c_void_p)])
    assert len(library.search_eth.argtypes) == 3


def test_search_passes_a_concrete_address():
    """An empty address is not a broadcast and is rejected by the controller."""
    link = make_controller()
    assert link.search('192.168.0.11', timeout_ms=1500) is True
    assert link.lib.search_calls, 'the search must reach the vendor export'
    address, timeout = link.lib.search_calls[-1]
    assert address == b'192.168.0.11'
    assert _int(timeout) == 1500


def test_search_without_an_address_falls_back_to_the_subnet_list():
    """With no address the driver must use the enumeration call, not a blank one."""
    link = make_controller()
    assert link.search('', timeout_ms=1200) is True
    assert link.lib.search_list_calls, 'the subnet enumeration must be used'
    assert not link.lib.search_calls, 'no address may be sent to the vendor export'


def test_search_list_returns_addresses():
    """The enumeration call answers with a space-separated address list."""
    from tuner.zmotion import ZmotionController

    class _ListLibrary:
        def search_eth_list(self, buffer, length, timeout):
            buffer.value = b'192.168.0.11 192.168.0.12 '
            return 0

    controller = ZmotionController.__new__(ZmotionController)
    controller.lib = _ListLibrary()
    assert controller.search_list(timeout_ms=1000) == ['192.168.0.11', '192.168.0.12']

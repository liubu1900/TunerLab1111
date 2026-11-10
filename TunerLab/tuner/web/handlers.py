"""JSON API handlers for the TunerLab web console.

Every function takes the shared :class:`AppState` and a request ``body`` dict and
returns a JSON-serialisable value. Errors propagate to the server, which wraps
them into ``{"ok": false, "error": ...}`` so the front end always gets a uniform
envelope.

The hardware handlers reuse :class:`tuner.ui_hardware.HardwareSession`; the studio
handlers reuse the pure-Python Mock calibration modules. Neither handler touches
any Qt widget.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import numpy as np

from .state import state, DEFAULT_FREQUENCIES
from ..axis_params import (
    AxisParams, TUNER_AXES, TUNER_MODELS, axis_index, tuner_axes,
)
from ..models import TunerConfig
from ..motion import MotionController, MotionPlanner, MotionError
from ..operation_log import operation_log
from ..pointplan import (
    DENSITY_PRESETS, MeasuredModel, coverage_report, density_count,
    plan_positions_from_targets, refine_positions, smith_targets,
    gamma_rings_targets, gamma_rings_table, cold_start_positions,
)
from ..vna import MockVNAController
from ..vna_drivers import create_vna, available_vendors
from ..zmotion_motion import build_tuner_config
from ..calibration import CalibrationManager
from ..database import CalibrationDatabase
from ..search import GammaSearchEngine
from ..rf import RFNetworkEngine as RF
from ..tuner_validation import DEFAULT_TOLERANCE, evaluate, plan_points
# --- P0 (0.4.0): schema v2 / frequency plan / layered points / gate ----
from .. import vna_calibration
from ..frequency_plan import (
    FREQUENCY_MODES, apply_frequency_plan, build_frequency_plan,
)
from ..interp import interpolation_error_report, select_interpolation_mode
from ..metadata import METADATA_FIELDS, normalise_metadata
from ..prerequisites import (
    CHECK_IDS, CHECK_TITLES, criteria_report, format_criteria_text,
    format_report_text, run_prerequisite_checks, snapshot_for_metadata,
)
# --- P1 (0.4.1): reference planes / export / library ops / browser /
#     repeatability / deviation card / tune dual-mode --------------------
from ..reference_planes import (
    REFERENCE_PLANES, REFERENCE_PLANE_IDS, validate_reference_setup,
)
from ..export import (
    export_tef, export_csv, export_s2p, export_s2p_library, export_hdf5_copy,
)
from ..library_ops import (
    truncate_by_gamma, truncate_by_travel, reverse_probe,
    split_by_frequency, apply_fixture,
)
from ..library_browser import scan_libraries, compare_libraries
from ..progress_cloud import progress_cloud
from ..repeatability import build_repeatability_report
from ..repeatability import auto_repeatability_measurements
from ..reference_shift import line_s_parameters, reference_shift_report, s2p_at_frequencies
from ..audit import audit_report
from ..patterns import delete_pattern, list_patterns, save_pattern
from ..reporting import format_gamma
from ..reporting import deviation_report, gamma_band


# =====================================================================
# P0 helpers (pure, no Qt / no hardware side effects)
# =====================================================================
def _frequency_plan_from_body(body: dict) -> dict:
    """Build a frequency plan from an init_hardware body.

    The default is the single 5 GHz spot frequency (``fo_list``), which keeps
    the operator's existing calibration workflow byte-for-byte equivalent.
    """
    mode = str(body.get('frequency_mode') or '').strip().lower()
    if not mode:
        # No explicit P0 mode: preserve the legacy 1-20 GHz / 401-point sweep
        # (or the single-spot case when points == 1) exactly as before.
        mode = 'freq_sweep' if int(body.get('vna_points', 1)) > 1 else 'fo_list'
    supplied = body.get('frequency_plan')
    if isinstance(supplied, dict) and supplied.get('frequencies_hz'):
        plan = dict(supplied)
        plan.setdefault('mode', mode)
        return plan
    harmonics = body.get('harmonic_numbers') or body.get('harmonics')
    if isinstance(harmonics, str):
        harmonics = [int(v) for v in harmonics.replace('，', ',').split(',') if v.strip()]
    arbitrary = body.get('frequency_list_ghz') or body.get('arbitrary_ghz')
    if isinstance(arbitrary, str):
        arbitrary = [float(v) * 1e9 for v in arbitrary.replace('，', ',').split(',') if v.strip()]
    elif arbitrary is not None:
        # The key says GHz, so a list of numbers is GHz too.  (Only the string
        # form used to be scaled, which made the desktop's list arrive as Hz.)
        arbitrary = [float(v) * 1e9 for v in arbitrary]
    return build_frequency_plan(
        mode,
        fo_hz=float(body.get('fo_hz', body.get('vna_start_hz', 5e9))),
        start_hz=float(body.get('vna_start_hz', 5e9)),
        stop_hz=float(body.get('vna_stop_hz', 5e9)),
        points=int(body.get('vna_points', 1)),
        harmonics=harmonics,
        frequencies_hz=arbitrary,
    )


def _prerequisite_context(state, body: dict) -> dict:
    """Assemble the 6-check context from the UI body and live hardware state."""
    motion_source = str(body.get('motion_source') or
                        state.studio.get('motion_source') or 'mock').lower()
    vna_source = str(body.get('vna_source') or
                     state.studio.get('vna_source') or 'mock').lower()
    supplied_plan = body.get('frequency_plan')
    plan = (supplied_plan if isinstance(supplied_plan, dict)
            and supplied_plan.get('frequencies_hz')
            else (state.studio.get('frequency_plan')
                  or _frequency_plan_from_body(body)))
    frequencies = [float(v) for v in plan.get('frequencies_hz', [5e9])]
    probes = int(body.get('probes') or 3)
    axes: dict[str, dict] = {}
    try:
        settings = state.hw.settings
        if settings is not None:
            for name, axis in settings.axes.items():
                axes[str(name)] = {
                    'homed': bool(getattr(axis, 'homed', False)),
                    'feedback_scale': float(getattr(axis, 'feedback_scale', 1.0)),
                }
            probes = int(getattr(settings, 'probe_count', probes))
    except Exception:  # noqa: BLE001 - mock sessions have no controller
        pass
    if motion_source != 'real':
        axes = {name: {'homed': True, 'feedback_scale': 1.0}
                for name in ('X', 'Z1', 'Z2', 'Z3')[:1 + probes]}
    return {
        'vna_calibration': vna_calibration.as_dict(),
        'vna_source': vna_source,
        'motion_source': motion_source,
        'frequencies_hz': frequencies,
        's2p_frequencies_hz': body.get('s2p_frequencies_hz'),
        's2p_path': body.get('s2p_path') or '',
        'axes': axes,
        'axis_count': 1 + probes,
        'probes': probes,
        'x_limits': (float(body.get('x_min', 0.0)), float(body.get('x_max', 10.0))),
        'z_limits': (float(body.get('z_min', 0.0)), float(body.get('z_max', 1.0))),
        'reciprocity_confirmed': bool(body.get('reciprocity_confirmed', False)),
    }


def _json_safe(value):
    """Convert numpy scalars / exotic objects into JSON-serialisable values."""
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:  # noqa: BLE001 - header must never break a calibration
        return str(value)


def _run_gate(state, body: dict) -> dict:
    """Compute, cache and return the prerequisite report."""
    report = run_prerequisite_checks(_prerequisite_context(state, body))
    report['text'] = format_report_text(report)
    report['check_titles'] = dict(CHECK_TITLES)
    with state.studio_lock:
        state.studio['prerequisite_report'] = report
    return report


# =====================================================================
# Hardware (real Zmotion)
# =====================================================================
def hw_status(state, body):
    return state.hw_status()


def hw_connect(state, body):
    ip = str(body.get('ip') or state.hw.settings.connection.ip)
    connect_on_start = body.get('connect_on_start', None)
    mode = str(body.get('mode') or state.hw.mode).strip().lower()
    with state.hw_lock:
        try:
            state.hw.connect(ip, mode=mode)
            if mode == 'real':
                state.hw.set_connection_settings(ip, connect_on_start)
        except Exception as exc:  # noqa: BLE001
            operation_log.exception('通讯', '连接控制器', exc, ip=ip)
            raise
        firmware = state.hw.link.firmware if state.hw.link else ''
    operation_log.info('通讯', '连接控制器', f'{ip} · 固件 {firmware or "未知"}', ip=ip,
                       firmware=firmware)
    return state.hw_status()


def hw_disconnect(state, body):
    with state.hw_lock:
        state.hw.disconnect()
    operation_log.info('通讯', '断开控制器')
    return state.hw_status()


def hw_search(state, body):
    from ..zmotion import ZmotionController
    found = ZmotionController().search(timeout_ms=int(body.get('timeout_ms', 1500)))
    operation_log.info('通讯', '广播搜索', '发现控制器' if found else '未发现控制器',
                       found=bool(found))
    return {'found': bool(found)}


def hw_settings(state, body):
    conn = state.hw.settings.connection
    settings = state.hw.settings
    return {
        'ip': conn.ip,
        'connect_on_start': conn.connect_on_start,
        'path': str(state.hw.store.path),
        'mode': settings.mode,
        'axis_count': settings.axis_count,
        'axes': list(settings.active_axes),
        'models': {str(count): list(names)
                   for count, names in TUNER_MODELS.items()},
    }


def hw_settings_save(state, body):
    ip = str(body.get('ip') or state.hw.settings.connection.ip)
    connect_on_start = body.get('connect_on_start', state.hw.settings.connection.connect_on_start)
    state.hw.set_connection_settings(ip, bool(connect_on_start))
    axis_count = body.get('axis_count')
    if axis_count is not None:
        with state.hw_lock:
            state.hw.set_axis_count(int(axis_count))
    return hw_settings(state, body)


def hw_set_mode(state, body):
    """Switch between the real and the virtual controller.

    The virtual controller needs no hardware and seeds itself with plausible
    positions, so the whole console can be rehearsed offline.
    """
    mode = str(body['mode']).strip().lower()
    with state.hw_lock:
        state.hw.set_mode(mode)
        operation_log.info('系统', '切换控制器模式',
                           '真实控制器' if mode == 'real' else '虚拟控制器',
                           mode=mode)
    return hw_settings(state, body)


def hw_set_model(state, body):
    """Switch the tuner model (3-axis or 4-axis) and persist the choice.

    The controller's physical axis indices do not change; only the set of axes
    that the software commands is affected.
    """
    count = int(body['axis_count'])
    with state.hw_lock:
        state.hw.set_axis_count(count)
        operation_log.info('系统', '切换 tuner 型号',
                           f'{count} 轴: {", ".join(state.hw.axes)}',
                           axis_count=count)
    return hw_settings(state, body)


def hw_inject_fault(state, body):
    """Inject or clear a fault on the virtual controller.

    This is a rehearsal aid: it reproduces the axis-22 alarm signal that the
    real machine reported, so the operator can practise reading the diagnostic
    pages without waiting for a real failure.
    """
    axis = str(body.get('axis') or '')
    bit = int(body.get('bit', 22))
    clear = bool(body.get('clear', False))
    with state.hw_lock:
        controller = state.hw.require_controller()
        inject = getattr(controller, 'inject_fault', None)
        if inject is None:
            raise RuntimeError('故障注入仅在虚拟控制器模式下可用')
        if clear:
            controller.clear_faults()
            operation_log.info('系统', '清除虚拟故障', axis=axis)
            return {'cleared': True, 'axes': controller.axis_status_snapshot()}
        status = inject(axis, bit)
        operation_log.warn('系统', '注入虚拟故障',
                           f'{axis} bit{bit} → 状态字 {status}', axis=axis, bit=bit)
        return {'cleared': False, 'status': status,
                'axes': controller.axis_status_snapshot()}


def hw_generate_virtual_data(state, body):
    """Generate a synthetic calibration database and load it into the studio.

    This is the virtual-mode counterpart of a real calibration run: the file
    has the same structure and provenance flags, so the Smith chart, the
    impedance tuning and the validation pages work exactly as they do with a
    measured database.
    """
    from ..virtual_data import generate_calibration

    x_count = int(body.get('x_count', 7))
    z_count = int(body.get('z_count', 4))
    if not 2 <= x_count <= 51 or not 2 <= z_count <= 15:
        raise ValueError('x_count must be 2..51 and z_count must be 2..15')
    raw_path = body.get('path') or str(
        Path.cwd() / 'calibrations'
        / f'virtual_{__import__("datetime").datetime.now():%Y%m%d_%H%M%S}.h5')
    path = Path(raw_path)
    if path.exists():
        raise FileExistsError(f'{path} 已存在，请换一个文件名')
    config = TunerConfig(probes=state.hw.probe_count,
                          fixed_offsets=tuple(0.0 for _ in range(state.hw.probe_count)))
    summary = generate_calibration(path, config, x_count=x_count, z_count=z_count)
    database = CalibrationDatabase(path)
    data = database.load()
    search_engine = (
        GammaSearchEngine(data, z0=float(data['metadata'].get('config', {}).get('z0', 50)))
        if np.any(data['valid']) else None)
    with state.studio_lock:
        s = state.studio
        s['database'] = database
        s['data'] = data
        s['search_engine'] = search_engine
        s['manager'] = None
        s['calibration_state'] = 'loaded'
        s['frequency_hz'] = float(data['frequencies'][len(data['frequencies']) // 2])
    operation_log.info('校准', '生成虚拟校准数据',
                       f'{summary["points"]} 点 · {summary["valid"]} 有效 · '
                       f'{path.name}', path=str(path))
    return summary


def hw_params(state, body):
    """Stored per-axis parameters from the settings file."""
    return {name: state.hw.settings.axes[name].to_dict()
            for name in state.hw.axes}


def hw_params_read(state, body):
    """Live parameters read from the controller."""
    with state.hw_lock:
        live = state.hw.read_parameters_from_controller()
    return {name: live[name] for name in state.hw.axes}


def _build_axis_params(axes_body: dict) -> dict:
    result = {}
    for name in state.hw.axes:
        raw = axes_body.get(name)
        if raw is None:
            continue
        cleaned = {k: float(v) for k, v in raw.items() if k != 'name'}
        cleaned['name'] = name
        result[name] = AxisParams(**cleaned)
    return result


def hw_params_write(state, body):
    """Validate, write to the controller and persist to the settings file."""
    axes = _build_axis_params(body.get('axes', {}))
    if not axes:
        raise ValueError('no axis parameters supplied')
    with state.hw_lock:
        state.hw.set_axis_parameters(axes)
        state.hw.apply_parameters_to_controller()
    return {name: axes[name].to_dict() for name in axes}


def hw_params_load(state, body):
    """Reload the saved parameters from disk."""
    settings = state.hw.store.load()
    return {name: settings.axes[name].to_dict() for name in state.hw.axes}


def _require_controller(state):
    with state.hw_lock:
        return state.hw.require_controller()


def hw_move(state, body):
    axis = str(body['axis'])
    target = float(body['target'])
    with state.hw_lock:
        controller = _require_controller(state)
        try:
            controller.move_axis_to(axis, target)
            snapshot = controller.axis_status_snapshot()
        except Exception as exc:  # noqa: BLE001
            operation_log.exception('运动', '绝对定位', exc, axis=axis, target=target)
            raise
    operation_log.info('运动', '绝对定位', f'{axis} → {target}', axis=axis, target=target)
    return {'axis': axis, 'snapshot': snapshot}


def hw_jog(state, body):
    axis = str(body['axis'])
    delta = float(body['delta'])
    with state.hw_lock:
        controller = _require_controller(state)
        try:
            controller.jog(axis, delta)
            snapshot = controller.axis_status_snapshot()
        except Exception as exc:  # noqa: BLE001
            operation_log.exception('运动', '点动', exc, axis=axis, delta=delta)
            raise
    operation_log.info('运动', '点动', f'{axis} {delta:+g}', axis=axis, delta=delta)
    return {'axis': axis, 'snapshot': snapshot}


def hw_home(state, body):
    axis = str(body['axis'])
    with state.hw_lock:
        controller = _require_controller(state)
        # Single-axis homing goes through the guarded entry point, which enforces
        # the emergency-stop / fault latch, the active-axis set (a three-axis
        # model may not home Z3) and the "probes before X" ordering, and which
        # refreshes the internal ``homed_axes`` so the reported state stays
        # truthful (P0-03).
        try:
            controller.home_axis_safe(axis, controller.config.motion_timeout)
            homed = axis in controller.homed_axes
            snapshot = controller.axis_status_snapshot()
        except Exception as exc:  # noqa: BLE001
            operation_log.exception('回零', '单轴回零', exc, axis=axis)
            raise
    level = 'info' if homed else 'warn'
    operation_log.log('回零', '单轴回零', f'{axis} 回零{"完成" if homed else "未完成"}',
                      level, axis=axis, home_status=int(homed))
    return {'axis': axis, 'home_status': int(homed), 'snapshot': snapshot}


def hw_stop(state, body):
    # Stop must never wait for a blocking move to finish: it uses the dedicated
    # emergency lock, not ``hw_lock`` (P0-01).
    with state.hw_emergency_lock:
        controller = _require_controller(state)
        controller.stop_all()
        snapshot = controller.axis_status_snapshot()
    operation_log.warn('运动', '停止', '已向全部轴发送停止指令')
    return {'snapshot': snapshot}


def hw_probe(state, body):
    axis = str(body['axis'])
    distance = float(body.get('distance', 0.005))
    with state.hw_lock:
        controller = _require_controller(state)
        try:
            result = controller.probe_axis_scale(axis, distance)
        except Exception as exc:  # noqa: BLE001
            operation_log.exception('标度', '标度自检', exc, axis=axis)
            raise
    ratio = float(result.get('ratio', float('nan')))
    consistent = bool(result.get('consistent', abs(ratio - 1.0) <= 0.05))
    operation_log.log('标度', '标度自检',
                      f'{axis} 指令 {result["commanded"]:g} → 编码器 '
                      f'{result["feedback"]:+.6f}，比值 {ratio:.3f}',
                      'info' if consistent else 'warn', axis=axis, ratio=ratio)
    return result


def hw_apply_scale(state, body):
    """Write a corrected ``UNITS`` value to the controller and store it.

    This is the fix for the commissioning finding that ``UNITS`` held the encoder
    resolution instead of the drive's real pulse equivalent.  The corrected value is
    ``UNITS / measured_ratio``; writing it makes one commanded unit equal one
    millimetre, which is what the calibration grid and the soft limits assume.
    """
    axis = str(body['axis'])
    units = float(body['units'])
    with state.hw_lock:
        result = state.hw.apply_scale_correction(axis, units)
    return result


def hw_estop(state, body):
    # Emergency stop uses the dedicated lock so it always preempts a blocking
    # move (P0-01).
    with state.hw_emergency_lock:
        controller = _require_controller(state)
        controller.emergency_stop()
    operation_log.error('急停', '触发急停', '软件急停已锁存，需复位后重新回零')
    return state.hw_status()


def hw_reset_estop(state, body):
    with state.hw_emergency_lock:
        controller = _require_controller(state)
        controller.reset_emergency()
    operation_log.warn('急停', '复位急停', '急停已复位，请重新回零后再运动')
    return state.hw_status()


# ---------------------------------------------------------------------
# Diagnostics: per-axis status and the controller's own error report
# ---------------------------------------------------------------------
def hw_axis_status(state, body):
    """Detailed per-axis state, including the decoded error flags."""
    with state.hw_lock:
        controller = _require_controller(state)
        snapshot = controller.axis_status_snapshot(detailed=True)
    return {'axes': snapshot}


def hw_errors(state, body):
    """Read the controller's error number together with the per-axis faults.

    The controller-side query is issued on demand only: an unknown Basic
    command can itself raise a controller error, which would then be reported
    as a fault that never happened.
    """
    with state.hw_lock:
        controller = _require_controller(state)
        try:
            result = controller.read_controller_errors()
        except Exception as exc:  # noqa: BLE001
            operation_log.exception('系统', '读取控制器错误码', exc)
            raise
    faults = [entry for entry in result['axes'] if entry['status_fault']]
    if faults:
        operation_log.error('系统', '读取控制器错误码',
                            '；'.join(f"{e['axis']}: {e['status_text']}" for e in faults))
    else:
        operation_log.info('系统', '读取控制器错误码', result['text'])
    return result


# ---------------------------------------------------------------------
# Operation log
# ---------------------------------------------------------------------
def oplog_recent(state, body):
    """Recent operation-log entries, newest last."""
    limit = int(body.get('limit', 200))
    since_seq = int(body.get('since_seq', 0))
    category = body.get('category') or None
    level = body.get('level') or None
    entries = operation_log.recent(limit, category, level, since_seq)
    return {'entries': entries, 'count': operation_log.count(),
            'last_seq': entries[-1]['seq'] if entries else since_seq}


def oplog_clear(state, body):
    removed = operation_log.clear()
    operation_log.info('系统', '清空操作日志', f'清除 {removed} 条记录')
    return {'removed': removed}


def oplog_export(state, body):
    """Write the log buffer to a JSON file."""
    raw = str(body.get('path') or '').strip()
    if not raw:
        raise ValueError('path is required')
    target = Path(raw)
    operation_log.export(target)
    operation_log.info('系统', '导出操作日志', str(target))
    return {'path': str(target), 'count': operation_log.count()}


# =====================================================================
# Smith-chart point planning
# =====================================================================
def studio_plan_preview(state, body):
    """Uniform target set plus the coverage of what has been measured so far.

    Pure preview: nothing on the machine or in the database is changed.
    """
    density = str(body.get('density', 'medium'))
    count = density_count(density, body.get('count'))
    method = str(body.get('method', 'sunflower'))
    r_min = float(body.get('r_min', 0.0))
    r_max = float(body.get('r_max', 0.95))
    targets = smith_targets(count, r_min, r_max, method)
    s = state.studio
    frequency = float(body.get('frequency', s['frequency_hz']))
    measured: list[dict] = []
    coverage: dict = {}
    try:
        _model, gammas, _positions = _measured_model(state, s, frequency)
    except Exception:  # noqa: BLE001 - the preview must never break the page
        gammas = None
    if gammas is not None:
        measured = [{'re': float(g.real), 'im': float(g.imag)} for g in gammas]
        coverage = coverage_report(gammas, targets)
    return {
        'targets': [{'re': float(t.real), 'im': float(t.imag)} for t in targets],
        'measured': measured,
        'count': int(count),
        'density': density,
        'method': method,
        'densities': dict(DENSITY_PRESETS),
        'coverage': coverage,
    }


def studio_plan_refine(state, body):
    """Append the points that even out the Smith-chart coverage.

    The measured points provide the model; the proposal fills the gaps in the
    Gamma plane and is appended to the *same* database, so the calibration run
    simply continues with the new points.
    """
    s = state.studio
    if s['calibration_state'] in _BUSY_STATES:
        raise RuntimeError(
            f"calibration is {s['calibration_state']}; wait for it to finish "
            f"or cancel before refining the plan")
    if s['database'] is None:
        raise RuntimeError('尚未建立校准库，无法细化布点')
    frequency = float(body.get('frequency', s['frequency_hz']))
    model, gammas, _positions = _measured_model(state, s, frequency)
    if model is None:
        raise RuntimeError(
            'Smith 图细化布点需要至少 4 个已测点：请先运行一次校准，再执行细化')
    count = density_count(body.get('density', 'medium'), body.get('count'))
    method = str(body.get('method', 'sunflower'))
    r_min = float(body.get('r_min', 0.0))
    r_max = float(body.get('r_max', 0.95))
    before = coverage_report(gammas, smith_targets(count, r_min, r_max, method))
    new_positions, targets = refine_positions(
        gammas, model, count, _config_limits(s['config']), r_min, r_max, method)
    with state.studio_lock:
        indices = s['database'].append_positions(
            new_positions, {'plan': 'smith-refine', 'refine_frequency_hz': frequency})
        data = s['database'].load()
        s['data'] = data
        z0 = float(s['config'].z0)
        s['search_engine'] = (
            GammaSearchEngine(data, z0=z0) if np.any(data['valid']) else None)
        s['manager'] = CalibrationManager(
            s['config'], s['motion'], s['vna'], s['database'])
        s['calibration_state'] = 'ready'
    projected = coverage_report(
        list(gammas) + list(targets), smith_targets(count, r_min, r_max, method))
    operation_log.info('校准', 'Smith 图细化布点',
                       f'新增 {len(indices)} 点，覆盖度 mean_spacing '
                       f'{before["mean_spacing"]:.4f} → {projected["mean_spacing"]:.4f}',
                       added=len(indices))
    return {
        'added': len(indices),
        'indices': indices,
        'positions': [[float(v) for v in row] for row in new_positions],
        'targets': [{'re': float(t.real), 'im': float(t.imag)} for t in targets],
        'coverage_before': before,
        'coverage_projected': projected,
        'pending': len(s['database'].pending_indices()),
    }


# =====================================================================
# Studio (Mock calibration)
# =====================================================================
def studio_overview(state, body):
    return state.studio_overview()


def studio_init_hardware(state, body):
    motion_source = str(body.get('motion_source', 'mock')).strip().lower()
    if motion_source not in ('mock', 'real'):
        raise ValueError("motion_source must be 'mock' or 'real'")
    if motion_source == 'real':
        # On real hardware the tuner model decides the geometry: the number of
        # probes must match the machine, otherwise the calibration grid would
        # address axes that do not exist.
        probes = state.hw.probe_count
    else:
        probes = int(body.get('probes', 3))
    offsets = body.get('offsets')
    if offsets is None:
        offsets = ([0.0, 2.0, 4.0] if probes == 3 else [0.0, 2.0])
    offsets = [float(v) for v in offsets]
    if len(offsets) != probes:
        raise ValueError(
            f'{probes} 个探针需要 {probes} 个固定偏移，收到 {len(offsets)} 个')
    homed_axes: list[str] = []
    if motion_source == 'real':
        # Real hardware: reuse the link the hardware console already owns, so the
        # controller is never opened twice, and derive the geometry from the
        # operator's declared travel box rather than the factory soft limits.
        controller = state.hw.require_controller()
        x_limits = (float(body.get('x_min', 0.0)), float(body.get('x_max', 10.0)))
        z_limits = (float(body.get('z_min', 0.0)), float(body.get('z_max', 1.0)))
        safe_z = float(body.get('safe_z', z_limits[0]))
        if x_limits[0] >= x_limits[1] or z_limits[0] >= z_limits[1]:
            raise ValueError('travel limits must be ordered and non-empty')
        # Safety gate: with UNITS still wrong, one commanded unit is not one
        # millimetre, so the whole calibration grid would sweep the machine by
        # that factor.  Refuse until the axis scales agree.
        #
        # The question is asked of the motion controller, not of the raw
        # ``feedback_scale`` field: the desktop application reads the encoder
        # in millimetres and stores "counts per millimetre" in that field, so a
        # direct comparison with 1.0 would refuse every axis there and make
        # the real-hardware calibration unreachable.
        mismatched = [name for name in state.hw.axes
                      if not controller.scale_is_consistent(name)]
        if mismatched:
            raise RuntimeError(
                '实机运动前必须先修正标度：'
                f'{", ".join(mismatched)} 的 UNITS 与反馈标度不一致，'
                '请到「轴移动控制」做标度自检并采纳修正后的 UNITS，再重新初始化。')
        config = build_tuner_config(state.hw.settings.axes, x_limits, z_limits,
                                    safe_z, axis_count=state.hw.settings.axis_count)
        # The planner gets its own config, so it has to inherit the motion
        # budget the controller actually runs with.  The desktop session raises
        # it to 60 s (X is 400 mm at 10 mm/s); with the 5 s default a 50 mm
        # move is reported as a timeout while the axis is still travelling --
        # the same defect the session was written to correct, reintroduced
        # through a second config object.
        controller_config = getattr(controller, 'config', None)
        if controller_config is not None:
            config.motion_timeout = float(controller_config.motion_timeout)
        motion = controller
        planner = MotionPlanner(config, motion)
        homed_axes = sorted(controller.homed_axes)
        operation_log.info('校准', '初始化实机运动',
                           f'行程 X {x_limits} · Z {z_limits} · 安全 Z {safe_z} · '
                           f'已回零 {homed_axes or "无"}',
                           x_limits=list(x_limits), z_limits=list(z_limits))
    else:
        config = TunerConfig(probes=probes, fixed_offsets=tuple(offsets))
        motion = MotionController(config)
        motion.initialize()
        motion.home()
        planner = MotionPlanner(config, motion)
        homed_axes = list(config.axes)
    vna_source = body.get('vna_source', 'mock')
    frequency_mode = str(body.get('frequency_mode') or '').strip().lower()
    frequency_plan = None
    if vna_source == 'real':
        # Use the live VNA (TCP/IP) instead of the synthetic one. The frequency
        # grid is taken from the instrument sweep configured here.
        vendor = str(body.get('vna_vendor') or 'auto')
        host = str(body.get('vna_ip') or '192.168.2.111')
        port = int(body.get('vna_port', 5025))
        timeout = float(body.get('vna_timeout_ms', 10000)) / 1000.0
        start = float(body.get('vna_start_hz', 1e9))
        stop = float(body.get('vna_stop_hz', 20e9))
        points = int(body.get('vna_points', 401))
        vna = create_vna(vendor, host, port, timeout)
        vna.connect()
        # P0 F-05: an explicit P0 frequency mode uses the validated plan.
        # Without one, the legacy single set_sweep call stays untouched.
        if frequency_mode and frequency_mode != 'freq_sweep':
            frequency_plan = _frequency_plan_from_body(body)
            apply_frequency_plan(vna, frequency_plan)
        else:
            vna.set_sweep(start, stop, points)
        vna_source_used = 'real'
    else:
        vna = MockVNAController(config, motion, DEFAULT_FREQUENCIES)
        vna.initialize()
        vna_source_used = 'mock'
        # List modes only describe the local grid on a mock VNA; never touch
        # the synthetic instrument's own frequency array.
        if frequency_mode and frequency_mode != 'freq_sweep':
            frequency_plan = _frequency_plan_from_body(body)
    if frequency_plan is None:
        grid = getattr(vna, 'frequencies', None)
        values = ([float(v) for v in np.asarray(grid, float).ravel()]
                  if grid is not None else [5e9])
        frequency_plan = {
            'mode': frequency_mode or ('fo_list' if len(values) == 1 else 'freq_sweep'),
            'frequencies_hz': values,
            'points': len(values),
            'uniform': True,
        }
        if not values:
            frequency_plan['frequencies_hz'] = [5e9]
            frequency_plan['points'] = 1
    with state.studio_lock:
        state.studio['config'] = config
        state.studio['motion'] = motion
        state.studio['planner'] = planner
        state.studio['vna'] = vna
        state.studio['vna_source'] = vna_source_used
        state.studio['motion_source'] = motion_source
        state.studio['homed_axes'] = homed_axes
        state.studio['manager'] = None
        state.studio['database'] = None
        state.studio['data'] = None
        state.studio['search_engine'] = None
        state.studio['last_candidates'] = []
        state.studio['validation_report'] = None
        state.studio['calibration_state'] = 'idle'
        state.studio['calibration_progress'] = (0, 0)
        # --- P0 state -------------------------------------------------
        state.studio['frequency_mode'] = frequency_plan['mode']
        state.studio['frequency_plan'] = frequency_plan
        state.studio['frequency_hz'] = float(frequency_plan['frequencies_hz'][0])
        state.studio['prerequisite_report'] = None
        state.studio['interpolation_mode'] = None
        state.studio['plan_table'] = None
    _run_gate(state, body)
    return state.studio_overview()


def _require_studio_hardware(state):
    with state.studio_lock:
        if state.studio['motion'] is None:
            raise RuntimeError('initialize virtual hardware first')
        return state.studio


#: Calibration / validation states during which geometry- or motion-changing
#: studio operations must be refused, because they would race the running task
#: and could inject a second, conflicting motion sequence (P0-06).
_BUSY_STATES = ('running', 'validating', 'validating_interpolation')


def _require_studio_idle(state):
    """Like :func:`_require_studio_hardware` but refuses a running task.

    Jog, retract, re-home, model/plan changes, re-opening a database and moving
    to a tuned/candidate position are all blocked while a calibration or
    validation sweep owns the hardware.  Stop / emergency-stop remain allowed so
    the operator can always halt the machine.
    """
    with state.studio_lock:
        s = state.studio
        if s['motion'] is None:
            raise RuntimeError('initialize virtual hardware first')
        if s['calibration_state'] in _BUSY_STATES:
            raise RuntimeError(
                f"calibration is {s['calibration_state']}; wait for it to finish "
                f"or cancel before changing geometry / moving the machine")
        return s


def studio_position(state, body):
    s = _require_studio_hardware(state)
    return {'position': s['motion'].read_position().tolist()}


def studio_jog(state, body):
    s = _require_studio_idle(state)
    axis = str(body['axis'])
    delta = float(body['delta'])
    s['motion'].jog(axis, delta)
    return {'position': s['motion'].read_position().tolist()}


def studio_retract(state, body):
    s = _require_studio_idle(state)
    s['motion'].retract()
    return {'position': s['motion'].read_position().tolist()}


def studio_estop(state, body):
    s = _require_studio_hardware(state)
    s['motion'].emergency_stop()
    if s['manager'] is not None:
        s['manager'].pause()
    return {'state': 'emergency', 'position': s['motion'].read_position().tolist()}


def studio_reset_home(state, body):
    s = _require_studio_hardware(state)
    s['motion'].reset_emergency()
    s['motion'].home()
    return {'position': s['motion'].read_position().tolist()}


def studio_home(state, body):
    """Home the axes without clearing the emergency latch first.

    Homing moves the machine, so it is an explicit operator action.  On real hardware
    the per-axis homing state is read back from the controller afterwards.
    """
    s = _require_studio_idle(state)
    s['motion'].home()
    homed = sorted(getattr(s['motion'], 'homed_axes', []))
    with state.studio_lock:
        state.studio['homed_axes'] = homed
    operation_log.info('回零', '校准工作台回零',
                       f'已回零轴：{", ".join(homed) if homed else "无"}',
                       homed=homed)
    return {'position': s['motion'].read_position().tolist(), 'homed_axes': homed}


def _config_limits(config):
    """Soft limits per axis, in the order used by the position vectors."""
    return ([(float(config.x_limits[0]), float(config.x_limits[1]))]
            + [(float(config.z_limits[0]), float(config.z_limits[1]))] * config.probes)


def _measured_model(state, s, frequency):
    """Build a :class:`MeasuredModel` from the loaded calibration database.

    Returns ``(model, gammas, positions)`` or ``(None, None, None)`` when the
    database does not hold enough measured points yet.
    """
    data = s['data']
    if data is None and s['database'] is not None:
        data = s['database'].load()
    if data is None:
        return None, None, None
    valid = np.flatnonzero(np.asarray(data['valid'], bool))
    if valid.size < 4:
        return None, None, None
    engine = s['search_engine']
    if engine is not None:
        try:
            gammas = engine.gammas(frequency)
            positions = engine.positions
        except (ValueError, KeyError):
            gammas = None
            positions = None
    else:
        gammas = None
        positions = None
    if gammas is None:
        s_valid = np.asarray(data['s'], complex)[valid]
        gammas = np.asarray(RF.dut_gamma(s_valid, 0j, None), dtype=complex)
        positions = np.asarray(data['actual'], float)[valid]
    if gammas.size < 4:
        return None, None, None
    return MeasuredModel(positions, gammas), gammas, positions


def studio_setup(state, body):
    s = _require_studio_idle(state)
    if s['vna'] is None:
        raise RuntimeError('virtual VNA is not initialized')
    # --- F-03 prerequisite checks --------------------------------------
    # The checks still run and their verdict is still recorded (library
    # header + operation log), but they no longer *block* the build: the
    # operator asked to drop this gate from the calibration flow while the
    # software is being commissioned.
    #
    # Set ``require_prerequisites`` to True to restore the hard gate — the
    # check engine and the report format are unchanged, so the gate can come
    # back without touching anything else.
    report = s.get('prerequisite_report')
    if report is None:
        report = _run_gate(state, body)
    if bool(body.get('require_prerequisites')) and not report.get('passed'):
        raise RuntimeError(
            '前提条件检查未通过，已禁止建立校准库：\n'
            + format_report_text(report))
    if not report.get('passed'):
        operation_log.warn(
            '校准', '前提检查未通过（未阻断建库）',
            '硬阻止项: ' + ('、'.join(report.get('failed_hard', [])) or '无'),
            failed_hard=list(report.get('failed_hard', [])),
            warnings=list(report.get('warnings', [])))
    plan = str(body.get('plan', 'cartesian')).strip().lower()
    x_count = int(body.get('x_count', 9))
    z_count = int(body.get('z_count', 4))
    raw_path = body.get('path') or str(
        Path.cwd() / 'calibrations' / f'mock_{__import__("datetime").datetime.now():%Y%m%d_%H%M%S}.h5')
    targets = None
    plan_table = None
    if plan == 'smith':
        # Smith-chart plan: the uniform target set is mapped onto the machine
        # with the model learned from an earlier measurement.
        model, _gammas, _positions = _measured_model(state, s, s['frequency_hz'])
        if model is None:
            raise RuntimeError(
                'Smith 图布点需要先有一轮已测点：请先用笛卡尔网格建库并运行，'
                '再用「Smith 图细化布点」补点')
        count = density_count(body.get('density', 'medium'), body.get('count'))
        method = str(body.get('method', 'sunflower'))
        r_min = float(body.get('r_min', 0.0))
        r_max = float(body.get('r_max', 0.95))
        targets = smith_targets(count, r_min, r_max, method)
        positions = plan_positions_from_targets(
            model, targets, _config_limits(s['config']))
    elif plan == 'gamma_rings':
        # P0 F-06/F-07: equal-|Gamma| layered plan with a cold start.  No
        # measured point is required: the positions come from a deterministic
        # physical prior and are replaced by the measured model after the run.
        count = density_count(body.get('density', 'medium'), body.get('count'))
        r_min = float(body.get('r_min', 0.0))
        r_max = float(body.get('r_max', 0.95))
        enhanced_center = bool(body.get('enhanced_center', False))
        enhanced_50ohm = bool(body.get('enhanced_50ohm', False))
        targets = gamma_rings_targets(
            count, r_min, r_max, enhanced_center=enhanced_center,
            enhanced_50ohm=enhanced_50ohm)
        positions = cold_start_positions(targets, s['config'])
        plan_table = gamma_rings_table(
            count, r_min, r_max, enhanced_center=enhanced_center,
            enhanced_50ohm=enhanced_50ohm)
    else:
        x_values = np.linspace(*s['config'].x_limits, x_count)
        z_values = np.linspace(*s['config'].z_limits, z_count)
        positions = s['config'].grid(x_values, z_values)
    # --- F-08 explicit interpolation strategy (never a silent fallback) ---
    interp_mode = select_interpolation_mode(plan, np.asarray(positions, float))
    fplan = s.get('frequency_plan') or {}
    calibration_state = vna_calibration.get_state()
    header = {
        'point_plan': plan,
        'frequency_mode': str(s.get('frequency_mode', 'fo_list')),
        'frequency_list': [float(v) for v in fplan.get('frequencies_hz', [])],
        'harmonics': list(fplan.get('harmonic_numbers', [])),
        'density': str(body.get('density', 'medium')),
        'cal_type': str(body.get('cal_type') or 'Standard'),
        'reference_plane': str(body.get('reference_plane')
                               or calibration_state.reference_plane),
        'cal_method': calibration_state.method,
        'vna_cal': vna_calibration.as_dict(),
        'interpolation_mode': interp_mode['mode'],
        'interpolation_detail': _json_safe(interp_mode),
        'prerequisite_snapshot': (snapshot_for_metadata(report) if report else None),
        'plan_table': _json_safe(plan_table),
    }
    database = CalibrationDatabase(raw_path)
    manager = CalibrationManager(s['config'], s['motion'], s['vna'], database)
    resumed = False
    valid_count = 0
    if database.path.exists():
        # Recovery after a restart or an interrupted run: attach to the existing
        # library instead of failing with "file exists".  ``CalibrationManager.run``
        # already checks the stored configuration and provenance; also make sure the
        # recomputed plan still describes the same frequency grid and point set, so
        # a different sweep can never be written into someone else's points.
        stored = database.load()
        frequencies = getattr(s['vna'], 'frequencies', None)
        frequencies = np.asarray(frequencies, float) if frequencies is not None else None
        if frequencies is None or not np.array_equal(
                np.asarray(stored['frequencies'], float), frequencies):
            raise ValueError('校准库的频率网格与当前矢网设置不一致；请改用新文件或恢复原设置')
        stored_positions = np.asarray(stored['positions'], float)
        if (stored_positions.shape != np.asarray(positions).shape
                or not np.allclose(stored_positions, positions, atol=1e-9, rtol=0)):
            raise ValueError('校准库的布点与当前网格不一致；请改用新文件或恢复原设置')
        valid_count = int(np.count_nonzero(stored['valid']))
        manager.state = 'ready'
        resumed = True
    else:
        manager.prepare(positions, header)
    with state.studio_lock:
        s['database'] = database
        s['manager'] = manager
        s['calibration_state'] = 'ready'
        # ``calibration_progress`` holds the zero-based index of the last
        # completed point, so an existing library resumes from its last valid one.
        s['calibration_progress'] = (max(0, valid_count - 1), len(positions))
        s['point_plan'] = plan
        s['plan_table'] = plan_table
        s['interpolation_mode'] = interp_mode
        s['prerequisite_report'] = report
    if resumed:
        # A restarted process has no in-memory model yet.  Rebuild the search
        # engine / provenance fingerprint from the library so the Smith chart,
        # tuning and the one-click move-and-verify loop work immediately,
        # without re-running the calibration.  The ready manager survives.
        _load_database(state, database, keep_task=True)
    operation_log.info('校准', '续扫校准库' if resumed else '建立校准库',
                       f'{plan} 布点 · {len(positions)} 点 · 已有 {valid_count} 点 · {raw_path}',
                       plan=plan, positions=len(positions), path=str(raw_path),
                       resumed=resumed, valid=valid_count)
    try:
        cal_id = str(database.read_metadata().get('cal_id', '')) if not resumed else ''
    except Exception:  # noqa: BLE001 - legacy library without a v2 header
        cal_id = ''
    return {'path': str(database.path), 'positions': len(positions),
            'state': 'ready', 'plan': plan, 'resumed': resumed, 'valid': valid_count,
            'targets': ([{'re': float(t.real), 'im': float(t.imag)} for t in targets]
                        if targets is not None else None),
            'interpolation_mode': interp_mode['mode'],
            'interpolation_detail': _json_safe(interp_mode),
            'plan_table': _json_safe(plan_table),
            'prerequisite_passed': bool(report.get('passed', False)),
            'frequency_mode': header['frequency_mode'],
            'cal_id': cal_id}


def studio_run(state, body):
    s = _require_studio_hardware(state)
    manager = s['manager']
    if manager is None:
        raise RuntimeError('create or open a calibration task first')

    def run_calibration():
        with state.studio_lock:
            state.studio['calibration_state'] = 'running'
        try:
            result = manager.run(
                progress=lambda i, n: state.studio.__setitem__(
                    'calibration_progress', (i, n)))
            with state.studio_lock:
                state.studio['calibration_state'] = str(result)
            # Auto-load the freshly written database so Smith / tuning are usable
            # immediately, mirroring the desktop console's post-run behaviour.
            if str(result) in ('completed', 'completed_with_errors'):
                try:
                    _load_database(state, s['database'])
                except Exception:  # noqa: BLE001 - loading must not mask the run result
                    pass
        except Exception as exc:  # noqa: BLE001 - keep the worker from dying silently
            with state.studio_lock:
                state.studio['calibration_state'] = 'failed'
                state.studio['calibration_error'] = f'{type(exc).__name__}: {exc}'

    thread = threading.Thread(target=run_calibration, daemon=True)
    with state.studio_lock:
        if s['calibration_thread'] is not None and s['calibration_thread'].is_alive():
            raise RuntimeError('calibration already running; pause or cancel first')
        state.studio['calibration_thread'] = thread
    thread.start()
    return {'state': 'running'}


def studio_pause(state, body):
    s = _require_studio_hardware(state)
    if s['manager'] is None:
        raise RuntimeError('no calibration task')
    s['manager'].pause()
    with state.studio_lock:
        state.studio['calibration_state'] = 'paused'
    return {'state': 'paused'}


def studio_cancel(state, body):
    s = _require_studio_hardware(state)
    if s['manager'] is None:
        raise RuntimeError('no calibration task')
    s['manager'].cancel()
    with state.studio_lock:
        state.studio['calibration_state'] = 'cancelled'
    return {'state': 'cancelled'}


def studio_task(state, body):
    s = state.studio
    pending = s['database'].pending_indices() if s['database'] is not None else []
    # Authoritative counts come from the committed ``valid`` flags.  The
    # ``progress`` callback reports the *index* of the last point written, so
    # treating it as a count used to leave a finished 1152-point sweep showing
    # "1151 / 1152" at 99% while the state pill already said 已完成.
    try:
        completed, total_points = (s['database'].progress_counts()
                                   if s['database'] is not None else (0, 0))
    except Exception:  # noqa: BLE001 - a half-written library must not kill the poll
        completed, total_points = 0, 0
    return {
        'state': s['calibration_state'],
        'progress': list(s['calibration_progress']),
        'completed': completed,
        'total_points': total_points,
        'pending': len(pending),
        'total': len(pending) if s['database'] else 0,
        'running': bool(s['calibration_thread'] is not None and s['calibration_thread'].is_alive()),
        'error': s.get('calibration_error'),
    }


def studio_open_db(state, body):
    with state.studio_lock:
        if state.studio['calibration_state'] in _BUSY_STATES:
            raise RuntimeError(
                f"calibration is {state.studio['calibration_state']}; wait for it "
                f"to finish or cancel before opening another database")
    raw_path = body.get('path')
    if not raw_path:
        raise ValueError('path is required')
    database = CalibrationDatabase(raw_path)
    _load_database(state, database)
    return state.studio_overview()


def _load_database(state, database, *, keep_task=False):
    """Populate studio state from a calibration database.

    ``keep_task=True`` is used when resuming an existing, already-completed
    library: the search engine and provenance fingerprint must be rebuilt, but
    the freshly attached ``CalibrationManager`` / ``ready`` state must survive.
    """
    data = database.load()
    search_engine = (
        GammaSearchEngine(data, z0=float(data['metadata'].get('config', {}).get('z0', 50)))
        if np.any(data['valid']) else None)
    # A fingerprint of the loaded calibration so a tuning result can be refused if
    # it was produced against a different database than the one currently loaded
    # (P0-07 / R5: loading a new library must invalidate old "last tuned" results).
    fingerprint = hashlib.sha256(json.dumps({
        'path': str(database.path),
        'positions': list(data['positions'].shape),
        'frequencies': [float(data['frequencies'][0]), float(data['frequencies'][-1])],
        'metadata': data['metadata'],
    }, default=str, sort_keys=True).encode('utf-8')).hexdigest()
    with state.studio_lock:
        s = state.studio
        s['database'] = database
        s['data'] = data
        s['search_engine'] = search_engine
        s['last_candidates'] = []
        s['last_candidates_fingerprint'] = None
        # Clear any previously tuned position: it belongs to a different database.
        s['last_tuned'] = None
        s['last_tuned_fingerprint'] = None
        s['database_fingerprint'] = fingerprint
        if not keep_task:
            s['manager'] = None
            s['calibration_state'] = 'loaded'
        s['frequency_hz'] = float(data['frequencies'][len(data['frequencies']) // 2])


def studio_database(state, body):
    s = state.studio
    if s['data'] is None:
        raise RuntimeError('no calibration database loaded')
    data = s['data']
    valid = int(np.count_nonzero(data['valid']))
    return {
        'path': str(s['database'].path),
        'valid': valid,
        'total': len(data['valid']),
        'frequencies': [float(data['frequencies'][0]), float(data['frequencies'][-1])],
        'metadata': data['metadata'],
    }


def _rf_metrics(gamma, z0=50.0):
    """Everything the operator wants to read off one Γ, in plain numbers.

    ``None`` stands for a mathematical infinity (VSWR at |Γ|=1, return loss at
    |Γ|=0) so the report stays valid JSON.  All values are JSON primitives.
    """
    gamma = complex(gamma)
    magnitude = float(abs(gamma))
    phase_deg = float(np.degrees(np.angle(gamma)))
    if magnitude >= 1.0 - 1e-12:
        vswr = None                       # infinite
        return_loss_db = 0.0
    else:
        vswr = (1.0 + magnitude) / (1.0 - magnitude)
        return_loss_db = (None if magnitude == 0.0
                          else float(-20.0 * np.log10(magnitude)))
    impedance = complex(RF.gamma_to_z(gamma, z0))
    return {
        'gamma_real': float(gamma.real), 'gamma_imag': float(gamma.imag),
        'gamma_mag': magnitude, 'gamma_phase_deg': phase_deg,
        'vswr': None if vswr is None else float(vswr),
        'return_loss_db': return_loss_db,
        'impedance_real': float(impedance.real),
        'impedance_imag': float(impedance.imag),
        'r_normalized': float(impedance.real / z0),
        'x_normalized': float(impedance.imag / z0),
    }


def _verification_report(*, frequency, target_gamma, measured_gamma, z0,
                         tolerance, simulated, kind='impedance_verification'):
    """Build the full Z / Γ / VSWR comparison for one arrived position."""
    target = _rf_metrics(target_gamma, z0)
    measured = _rf_metrics(measured_gamma, z0)
    delta_r = measured['impedance_real'] - target['impedance_real']
    delta_x = measured['impedance_imag'] - target['impedance_imag']
    vector_error = float(abs(complex(measured_gamma) - complex(target_gamma)))
    report = {
        'kind': kind,
        'simulated': bool(simulated),
        'rf_hardware_verified': bool(not simulated),
        'frequency_hz': float(frequency),
        'tolerance': float(tolerance),
        'vector_error': vector_error,
        'within_tolerance': vector_error <= float(tolerance),
        'impedance_delta_r': float(delta_r),
        'impedance_delta_x': float(delta_x),
        'impedance_delta_r_percent': (100.0 * delta_r / target['impedance_real']
                                      if abs(target['impedance_real']) > 1e-9 else None),
        'impedance_delta_x_percent': (100.0 * delta_x / target['impedance_imag']
                                      if abs(target['impedance_imag']) > 1e-9 else None),
        'vswr_delta': (measured['vswr'] - target['vswr']
                       if measured['vswr'] is not None and target['vswr'] is not None
                       else None),
        'interpretation': (
            'Simulated RF verification only; no physical RF accuracy claim.'
            if simulated else
            'Measured with the connected VNA after the move; agreement is judged '
            'by |ΔΓ| against the tolerance. Mechanical positioning alone does '
            'not establish RF accuracy.'),
    }
    report.update({f'measured_{key}': value for key, value in measured.items()})
    report.update({f'target_{key}': value for key, value in target.items()})
    return report


def studio_smith(state, body):
    s = state.studio
    if s['search_engine'] is None:
        return {'gammas': [], 'target': None, 'selected': None}
    frequency = float(body.get('frequency', s['frequency_hz']))
    with state.studio_lock:
        s['frequency_hz'] = frequency
        gammas = s['search_engine'].gammas(frequency)
        target = s.get('last_target')
        selected = s.get('last_selected')
    return {
        'gammas': [{'re': g.real, 'im': g.imag} for g in gammas],
        'target': ({'re': target.real, 'im': target.imag} if target is not None else None),
        'selected': ({'re': selected.real, 'im': selected.imag} if selected is not None else None),
        'frequency_hz': frequency,
    }


def _serialise_tune(result):
    """Turn a tuning result into JSON-serialisable data."""
    return {
        'position': [float(v) for v in result['position']],
        'gamma': {'re': float(result['gamma'].real), 'im': float(result['gamma'].imag)},
        'error': float(result['error']),
        'method': result['method'],
        'interpolator': result['interpolator'],
        'predicted': bool(result['predicted']),
        'stages': [
            {
                'stage': stage['stage'],
                'position': [float(v) for v in stage['position']],
                'gamma': {'re': float(stage['gamma'].real),
                          'im': float(stage['gamma'].imag)},
                'error': float(stage['error']),
            }
            for stage in result['stages']
        ],
    }


def studio_tune(state, body):
    s = state.studio
    if s['search_engine'] is None:
        raise RuntimeError('load a completed or partial calibration first')
    frequency = float(body.get('frequency', s['frequency_hz']))
    target_z = complex(float(body.get('R', 50)), float(body.get('X', 0)))
    tolerance = float(body.get('tolerance', 0.08))
    method = str(body.get('method', 'two-stage')).strip().lower()
    # -- F-21 (P1): Tune-To dual mode ---------------------------------------
    # ``closest`` snaps to the nearest *calibrated* point (no interpolation);
    # ``cursor`` interpolates at the picked Smith-chart cursor ("Tune to
    # cursor").  The explicit ``method`` still wins if supplied.
    tune_to_mode = body.get('tune_to_mode')
    if tune_to_mode:
        mode = str(tune_to_mode).strip().lower()
        if mode not in ('closest', 'cursor'):
            raise ValueError("tune_to_mode must be 'closest' or 'cursor'")
        method = 'nearest' if mode == 'closest' else 'two-stage'
    if method not in ('nearest', 'general', 'gradient', 'two-stage'):
        raise ValueError("method must be 'nearest', 'general', 'gradient' or 'two-stage'")
    z0 = s['search_engine'].z0
    target_gamma = RF.z_to_gamma(target_z, z0)
    results = s['search_engine'].search(frequency, target_gamma, k=5, tolerance=tolerance)
    candidates = [{
        'index': int(r.index),
        'position': [float(v) for v in r.position],
        'gamma': {'re': r.gamma.real, 'im': r.gamma.imag},
        'impedance': {'re': r.impedance.real, 'im': r.impedance.imag},
        'error': float(r.error),
        'reachable': bool(r.reachable),
    } for r in results]
    tuned = None
    if method != 'nearest':
        limits = _config_limits(s['config']) if s['config'] is not None else None
        level_policy = str(body.get('level_policy', 'closest')).strip().lower()
        try:
            raw = s['search_engine'].tune(frequency, target_gamma, method=method,
                                          limits=limits, level_policy=level_policy)
            tuned = _serialise_tune(raw)
        except Exception as exc:  # noqa: BLE001 - a failed refinement must not
            # hide the nearest-neighbour candidates that were found already.
            operation_log.exception('校准', '插值调谐', exc, method=method)
            tuned = {'error': f'{type(exc).__name__}: {exc}', 'method': method}
    with state.studio_lock:
        s['last_candidates'] = results
        s['last_candidates_fingerprint'] = s.get('database_fingerprint')
        s['last_target'] = target_gamma
        s['last_selected'] = results[0].gamma if results else None
        s['frequency_hz'] = frequency
        s['last_tuned'] = tuned
        s['last_tuned_fingerprint'] = s.get('database_fingerprint') if tuned else None
        if tune_to_mode:
            s['last_tune_to_mode'] = str(tune_to_mode).strip().lower()
    operation_log.info('校准', '阻抗调谐',
                       f'{method} · 目标 Γ {target_gamma.real:+.4f}{target_gamma.imag:+.4f}j'
                       + (f' · 误差 {tuned["error"]:.6f}'
                          if tuned and isinstance(tuned.get('error'), float) else ''),
                       method=method)
    return {'candidates': candidates,
            'target_gamma': {'re': target_gamma.real, 'im': target_gamma.imag},
            'method': method,
            'tune_to_mode': (str(tune_to_mode).strip().lower()
                             if tune_to_mode else s.get('last_tune_to_mode')),
            'tuned': tuned}


def studio_move_tuned(state, body):
    """Move the machine to the position found by the interpolated tuning."""
    s = state.studio
    if s['calibration_state'] in _BUSY_STATES:
        raise RuntimeError(
            f"calibration is {s['calibration_state']}; wait for it to finish "
            f"or cancel before moving the machine")
    tuned = s.get('last_tuned')
    if not tuned or 'position' not in tuned:
        raise RuntimeError('run an interpolated tune first')
    if s.get('last_tuned_fingerprint') != s.get('database_fingerprint'):
        raise RuntimeError(
            'stale tuning result: the loaded database changed; re-run the tune')
    planner = s.get('planner')
    if planner is None:
        raise RuntimeError('no motion planner available; set up a calibration task first')
    position = np.asarray(tuned['position'], dtype=float)
    planner.move_to(position)
    operation_log.info('运动', '移动到调谐结果',
                       ' | '.join(f'{v:.4f}' for v in position),
                       position=[float(v) for v in position])
    return {'position': s['motion'].read_position().tolist(),
            'predicted_gamma': tuned['gamma'],
            'error': float(tuned['error'])}


def studio_move_candidate(state, body):
    s = state.studio
    if s['calibration_state'] in _BUSY_STATES:
        raise RuntimeError(
            f"calibration is {s['calibration_state']}; wait for it to finish "
            f"or cancel before moving the machine")
    if not s['last_candidates']:
        raise RuntimeError('search and select a candidate first')
    if s.get('last_candidates_fingerprint') != s.get('database_fingerprint'):
        raise RuntimeError(
            'stale candidates: the loaded database changed; re-run the search')
    row = int(body.get('index', 0))
    if row < 0 or row >= len(s['last_candidates']):
        raise RuntimeError('candidate index out of range')
    result = s['last_candidates'][row]
    planner = s.get('planner')
    if planner is None:
        raise RuntimeError('no motion planner available; set up a calibration task first')
    planner.move_to(result.position)
    return {'position': s['motion'].read_position().tolist(),
            'predicted_gamma': {'re': result.gamma.real, 'im': result.gamma.imag},
            'error': float(result.error)}


def studio_verify(state, body):
    s = state.studio
    if s['search_engine'] is None or s['data'] is None:
        raise RuntimeError('no valid calibration points')
    if s['vna'] is None:
        raise RuntimeError('the instrument is not initialised')
    frequency = float(body.get('frequency', s['frequency_hz']))
    target_z = complex(float(body.get('R', 50)), float(body.get('X', 0)))
    tolerance = float(body.get('tolerance', 0.08))
    z0 = s['search_engine'].z0
    m = s['vna'].measure()
    f = m.frequencies
    if not (f[0] <= frequency <= f[-1]):
        raise ValueError('target frequency outside the VNA grid')
    measured = RF.dut_gamma(m.s)
    gamma = complex(np.interp(frequency, f, measured.real),
                    np.interp(frequency, f, measured.imag))
    target_gamma = RF.z_to_gamma(target_z, z0)
    report = _verification_report(
        frequency=frequency, target_gamma=target_gamma, measured_gamma=gamma,
        z0=z0, tolerance=tolerance, simulated=m.simulated)
    s['database'].save_validation(report)
    with state.studio_lock:
        s['validation_report'] = report
        s['frequency_hz'] = frequency
    return report


def studio_move_to_gamma(state, body):
    """One-click Smith-chart pick → search → move → measure → judge.

    The returned report carries the target and the *measured* Z, Γ, |Γ|, phase
    and VSWR together with their deviations, so the operator never has to infer
    them from a single |ΔΓ| number.
    """
    s = state.studio
    if s['search_engine'] is None or s['data'] is None:
        raise RuntimeError('load a completed or partial calibration first')
    if s['vna'] is None:
        raise RuntimeError('the instrument is not initialised')
    if s['calibration_state'] in _BUSY_STATES:
        raise RuntimeError(
            f"calibration is {s['calibration_state']}; wait for it to finish "
            f"or cancel before moving the machine")
    planner = s.get('planner')
    if planner is None:
        raise RuntimeError('no motion planner available; set up a calibration task first')

    frequency = float(body.get('frequency', s['frequency_hz']))
    z0 = s['search_engine'].z0
    if 'gamma_re' in body and 'gamma_im' in body:
        target_gamma = complex(float(body['gamma_re']), float(body['gamma_im']))
        if abs(target_gamma) > 1.0:
            raise ValueError('the picked point lies outside the Smith chart')
        target_z = complex(RF.gamma_to_z(target_gamma, z0))
    else:
        target_z = complex(float(body.get('R', 50)), float(body.get('X', 0)))
        target_gamma = complex(RF.z_to_gamma(target_z, z0))
    tolerance = float(body.get('tolerance', 0.08))
    method = str(body.get('method', 'two-stage')).strip().lower()
    level_policy = str(body.get('level_policy', 'closest')).strip().lower()
    tune_to_mode = body.get('tune_to_mode')

    tune = studio_tune(state, {
        'frequency': frequency, 'R': target_z.real, 'X': target_z.imag,
        'tolerance': tolerance, 'method': method, 'level_policy': level_policy,
        **({'tune_to_mode': tune_to_mode} if tune_to_mode else {}),
    })
    candidates, tuned = tune['candidates'], tune['tuned']
    choice = None
    if candidates:
        choice = {
            'position': candidates[0]['position'],
            'gamma': complex(candidates[0]['gamma']['re'],
                             candidates[0]['gamma']['im']),
            'error': float(candidates[0]['error']), 'kind': 'calibrated point',
        }
    if tuned and 'position' in tuned and (choice is None or
                                          float(tuned['error']) <= choice['error']):
        choice = {
            'position': tuned['position'],
            'gamma': complex(tuned['gamma']['re'], tuned['gamma']['im']),
            'error': float(tuned['error']), 'kind': 'interpolated',
        }
    if choice is None:
        raise RuntimeError('no reachable calibration position for this Γ')

    commanded = np.asarray(choice['position'], dtype=float)
    planner.move_to(commanded)
    settle = float(body.get('settle_s', 0.25))
    if settle > 0:
        time.sleep(min(settle, 5.0))
    actual = np.asarray(s['motion'].read_position(), dtype=float)

    m = s['vna'].measure()
    f = m.frequencies
    if not (f[0] <= frequency <= f[-1]):
        raise ValueError('target frequency outside the VNA grid')
    measured = RF.dut_gamma(m.s)
    gamma = complex(np.interp(frequency, f, measured.real),
                    np.interp(frequency, f, measured.imag))
    report = _verification_report(
        frequency=frequency, target_gamma=target_gamma, measured_gamma=gamma,
        z0=z0, tolerance=tolerance, simulated=m.simulated,
        kind='smith_pick_move_verification')
    report.update({
        'position': [float(v) for v in actual],
        'commanded_position': [float(v) for v in commanded],
        'position_error_max': float(np.max(np.abs(actual - commanded))),
        'prediction_kind': choice['kind'],
        'predicted_gamma_real': float(choice['gamma'].real),
        'predicted_gamma_imag': float(choice['gamma'].imag),
        'predicted_error': choice['error'],
    })
    s['database'].save_validation(report)
    with state.studio_lock:
        s['validation_report'] = report
        s['frequency_hz'] = frequency
    operation_log.info(
        '校准', 'Smith 点选移动验证',
        f"目标 Z {target_z.real:.3f}{target_z.imag:+.3f}j Ω · "
        f"位置 {' | '.join(f'{v:.4f}' for v in actual)} · "
        f"实测 |ΔΓ| {report['vector_error']:.6f} · "
        f"{'通过' if report['within_tolerance'] else '超差'}",
        frequency_hz=frequency, position=[float(v) for v in actual])
    return report


def studio_validate(state, body):
    s = state.studio
    if s['calibration_state'] in _BUSY_STATES:
        raise RuntimeError(
            f"calibration is {s['calibration_state']}; wait for it to finish "
            f"or cancel before starting a validation")
    if s['data'] is None:
        raise RuntimeError('load a calibration first')
    if s['vna'] is None:
        raise RuntimeError('the instrument is not initialised')
    indices = np.flatnonzero(s['data']['valid'])
    if len(indices) < 2:
        raise ValueError('two valid positions required')
    if not np.array_equal(s['vna'].frequencies, s['data']['frequencies']):
        raise ValueError('VNA frequency grid differs; reopen database to align')
    a, b = s['data']['positions'][indices[[0, -1]]]

    def run_validation():
        with state.studio_lock:
            state.studio['calibration_state'] = 'validating'
        try:
            report = repeatability_report(s['planner'], s['vna'], a, b, repeats=3)
            s['database'].save_validation(report)
            with state.studio_lock:
                state.studio['validation_report'] = report
                state.studio['calibration_state'] = 'validated'
        except Exception as exc:  # noqa: BLE001
            with state.studio_lock:
                state.studio['calibration_state'] = 'failed'
                state.studio['calibration_error'] = f'{type(exc).__name__}: {exc}'

    thread = threading.Thread(target=run_validation, daemon=True)
    with state.studio_lock:
        if s['calibration_thread'] is not None and s['calibration_thread'].is_alive():
            raise RuntimeError('an operation is already running')
        state.studio['calibration_thread'] = thread
    thread.start()
    return {'state': 'validating'}


def studio_validation_report(state, body):
    return state.studio.get('validation_report')


def studio_validate_interpolation(state, body):
    """Validate the interpolated tuning by measuring interpolated impedances.

    This is the standard validation loop: the tuner is commanded to a set of
    *interpolated* impedances, the S-parameters are measured with the calibrated
    instrument, the error vector is calculated and reported, and a result beyond
    the accepted tolerance recommends a recalibration.

    The machine moves, so this is an explicit operator action; it runs on a
    worker thread and reports progress like the calibration run.
    """
    s = state.studio
    if s['calibration_state'] in _BUSY_STATES:
        raise RuntimeError(
            f"calibration is {s['calibration_state']}; wait for it to finish "
            f"or cancel before starting an interpolation validation")
    if s['search_engine'] is None or s['data'] is None:
        raise RuntimeError('load a calibration first')
    if s['vna'] is None:
        raise RuntimeError('the instrument is not initialised')
    if not np.array_equal(s['vna'].frequencies, s['data']['frequencies']):
        raise ValueError('VNA frequency grid differs; reopen database to align')
    count = int(body.get('count', 5))
    tolerance = float(body.get('tolerance', DEFAULT_TOLERANCE))
    strategy = str(body.get('strategy', 'coverage')).strip().lower()
    frequency = float(body.get('frequency', s['frequency_hz']))
    model = s['search_engine'].response_model()
    points = plan_points(model, count, strategy, frequency=frequency)

    def run_validation():
        with state.studio_lock:
            state.studio['calibration_state'] = 'validating_interpolation'
            state.studio['calibration_progress'] = (0, len(points))
        try:
            lower, upper, weight = model.frequency_slice(frequency)

            def measure(position):
                s['planner'].move_to(position)
                measurement = s['vna'].measure()
                low = np.asarray(measurement.s[lower], dtype=complex)
                if weight == 0.0:
                    return low
                high = np.asarray(measurement.s[upper], dtype=complex)
                return low * (1.0 - weight) + high * weight

            report = evaluate(
                model, points, measure, tolerance=tolerance, frequency=frequency,
                progress=lambda done, total: state.studio.__setitem__(
                    'calibration_progress', (done, total)))
            report['strategy'] = strategy
            s['database'].save_validation(report)
            with state.studio_lock:
                state.studio['interpolation_report'] = report
                state.studio['validation_report'] = report
                state.studio['calibration_state'] = 'validated'
            operation_log.log('校准', '插值验证', report['message'],
                              'info' if report['passed'] else 'warn',
                              passed=report['passed'],
                              max_error=report['max_error'])
        except Exception as exc:  # noqa: BLE001
            with state.studio_lock:
                state.studio['calibration_state'] = 'failed'
                state.studio['calibration_error'] = f'{type(exc).__name__}: {exc}'
            operation_log.exception('校准', '插值验证', exc)

    thread = threading.Thread(target=run_validation, daemon=True)
    with state.studio_lock:
        if s['calibration_thread'] is not None and s['calibration_thread'].is_alive():
            raise RuntimeError('an operation is already running')
        s['calibration_thread'] = thread
    thread.start()
    return {'state': 'validating_interpolation', 'points': len(points),
            'tolerance': tolerance, 'strategy': strategy}


# =====================================================================
# VNA (real vector network analyzer, TCP/IP SCPI)
# =====================================================================
def vna_status(state, body):
    return state.vna_status()


def vna_vendors(state, body):
    return {'vendors': available_vendors()}


def vna_connect(state, body):
    with state.vna_lock:
        # tear down any previous session
        old = state.vna.get('controller')
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        cfg = state.vna['config']
        cfg['vendor_key'] = str(body.get('vendor_key') or cfg['vendor_key'] or 'auto')
        cfg['host'] = str(body.get('host') or cfg['host'])
        cfg['port'] = int(body.get('port') or cfg['port'])
        cfg['timeout_ms'] = int(body.get('timeout_ms') or cfg['timeout_ms'])
        cfg['start_hz'] = float(body.get('start_hz') or cfg['start_hz'])
        cfg['stop_hz'] = float(body.get('stop_hz') or cfg['stop_hz'])
        cfg['points'] = int(body.get('points') or cfg['points'])
        if cfg['points'] == 1:
            cfg['stop_hz'] = cfg['start_hz']
        cfg['power_dbm'] = float(body.get('power_dbm') or cfg['power_dbm'])
        cfg['ifbw_hz'] = float(body.get('ifbw_hz') or cfg['ifbw_hz'])
        controller = create_vna(cfg['vendor_key'], cfg['host'], cfg['port'],
                                cfg['timeout_ms'] / 1000.0)
        idn = controller.connect()
        # apply the sweep / power / ifbw requested by the panel
        controller.set_sweep(cfg['start_hz'], cfg['stop_hz'], cfg['points'])
        controller.set_power(cfg['power_dbm'])
        controller.set_ifbw(cfg['ifbw_hz'])
        state.vna['controller'] = controller
        state.vna['connected'] = True
        state.vna['idn'] = idn
        state.vna['vendor'] = controller.vendor_key
        state.vna['last_error'] = controller.last_error
    return state.vna_status()


def vna_disconnect(state, body):
    with state.vna_lock:
        controller = state.vna.get('controller')
        if controller is not None:
            try:
                controller.close()
            except Exception:  # noqa: BLE001
                pass
        state.vna['controller'] = None
        state.vna['connected'] = False
    return state.vna_status()


def vna_idn(state, body):
    with state.vna_lock:
        controller = state.vna.get('controller')
        if controller is None or not controller.connected:
            raise RuntimeError('VNA not connected')
        idn = controller.transport.query('*IDN?')
        state.vna['idn'] = idn
    return state.vna_status()


def _require_vna(state):
    with state.vna_lock:
        controller = state.vna.get('controller')
        if controller is None or not controller.connected:
            raise RuntimeError('VNA not connected')
        return controller


def vna_set_sweep(state, body):
    controller = _require_vna(state)
    start = float(body['start_hz'])
    stop = float(body['stop_hz'])
    points = int(body['points'])
    if points == 1:
        # Single-frequency calibration: STOP is pinned to START so the stored
        # panel config matches what set_sweep actually pushed to the analyzer.
        stop = start
    controller.set_sweep(start, stop, points)
    with state.vna_lock:
        cfg = state.vna['config']
        cfg['start_hz'], cfg['stop_hz'], cfg['points'] = start, stop, points
        state.vna['last_error'] = controller.last_error
    return state.vna_status()


def vna_set_power(state, body):
    controller = _require_vna(state)
    dbm = float(body['power_dbm'])
    controller.set_power(dbm)
    with state.vna_lock:
        state.vna['config']['power_dbm'] = dbm
    return state.vna_status()


def vna_set_ifbw(state, body):
    controller = _require_vna(state)
    hz = float(body['ifbw_hz'])
    controller.set_ifbw(hz)
    with state.vna_lock:
        state.vna['config']['ifbw_hz'] = hz
    return state.vna_status()


def vna_trigger(state, body):
    controller = _require_vna(state)
    controller.trigger_single()
    errs = controller.read_errors()
    with state.vna_lock:
        state.vna['last_error'] = '; '.join(errs) if errs else None
    return {'triggered': True, 'errors': errs}


def vna_fetch(state, body):
    controller = _require_vna(state)
    m = controller.measure()
    f = m.frequencies
    s = m.s
    sp = {}
    for name, (op, ip) in {'S11': (0, 0), 'S21': (1, 0),
                           'S12': (0, 1), 'S22': (1, 1)}.items():
        mag = 20.0 * np.log10(np.abs(s[:, op, ip]) + 1e-30)
        sp[name] = {
            'mag_db': [float(v) for v in mag],
            're': [float(v.real) for v in s[:, op, ip]],
            'im': [float(v.imag) for v in s[:, op, ip]],
        }
    return {
        'frequencies_hz': [float(v) for v in f],
        's': sp,
        'idn': m.metadata.get('idn'),
        'vendor': m.metadata.get('vendor'),
    }


# =====================================================================
# P0 (0.4.0) endpoints: prerequisite gate, VNA calibration, plan, header
# =====================================================================
def studio_prerequisites(state, body):
    """Run (or re-run) the 6 prerequisite checks and cache the report."""
    context = _prerequisite_context(state, body)
    for key in ('vna_calibration', 'vna_source', 'motion_source',
                'frequencies_hz', 's2p_frequencies_hz', 's2p_path',
                'axes', 'axis_count', 'probes', 'x_limits', 'z_limits',
                'reciprocity_confirmed', 's21', 's12'):
        if key in body:
            context[key] = body[key]
    report = run_prerequisite_checks(context)
    report['text'] = format_report_text(report)
    report['check_titles'] = dict(CHECK_TITLES)
    report['check_ids'] = list(CHECK_IDS)
    with state.studio_lock:
        state.studio['prerequisite_report'] = report
    return report


def vna_calibration_status(state, body):
    """F-04: real, queryable VNA calibration state (no hard-coded False)."""
    return {'state': vna_calibration.as_dict(),
            'report': vna_calibration.status_report(),
            'max_age_days': 7.0}


def vna_calibration_set(state, body):
    """Record or clear the operator-verified VNA calibration.

    ``verified=True`` stamps ``timestamp`` with the current time unless one is
    supplied explicitly; ``clear=True`` removes the record entirely.
    """
    if bool(body.get('clear')):
        vna_calibration.clear()
    else:
        payload = {key: body[key] for key in (
            'method', 'cal_kit', 'timestamp', 'reference_plane', 'note')
                   if key in body}
        if 'verified' in body:
            payload['verified'] = bool(body['verified'])
        elif not payload.get('timestamp'):
            payload['verified'] = True
            payload['timestamp'] = time.time()
        vna_calibration.set_state(**payload)
    report = vna_calibration.status_report()
    operation_log.info('矢网', '录入 VNA 校准状态',
                       f"{report.get('method') or '未指定'} · "
                       f"{'有效' if report.get('valid') else '未验证'}",
                       method=report.get('method'))
    return {'state': vna_calibration.as_dict(), 'report': report}


def studio_frequency_plan(state, body):
    """F-05: validate a frequency plan without touching any instrument."""
    plan = _frequency_plan_from_body(body)
    return {'modes': list(FREQUENCY_MODES), 'plan': plan,
            'frequencies_ghz': [v / 1e9 for v in plan['frequencies_hz']]}


def studio_metadata(state, body):
    """F-01/F-02: expose the 14 header fields and per-point provenance."""
    database = state.studio.get('database')
    if database is None or not database.path.is_file():
        return {'available': False, 'fields': list(METADATA_FIELDS)}
    meta = database.read_metadata()
    records = database.read_point_records()
    summary = {
        key: {'count': int(len(value)),
              'finite': int(np.count_nonzero(np.isfinite(value)))}
        for key, value in records.items()
    }
    return {
        'available': True,
        'schema_version': int(meta.get('schema_version', 1)),
        'fields': {key: _json_safe(meta.get(key)) for key in METADATA_FIELDS},
        'metadata': _json_safe(meta),
        'point_records': summary,
    }


def studio_interpolation(state, body):
    """F-08: explicit interpolation mode plus per-layer error estimate."""
    s = state.studio
    mode = (s.get('interpolation_mode') or {}).get('mode', 'tensor')
    data = s.get('data')
    if data is None and s.get('database') is not None:
        try:
            data = s['database'].load()
        except Exception:  # noqa: BLE001
            data = None
    if data is None:
        return {'available': False, 'mode': mode,
                'selection': _json_safe(s.get('interpolation_mode'))}
    valid = np.flatnonzero(np.asarray(data['valid'], bool))
    positions = np.asarray(data.get('actual', data['positions']), float)[valid]
    s_matrix = np.asarray(data['s'], complex)[valid]
    result = interpolation_error_report(positions, s_matrix, mode)
    result.update({'available': True, 'mode': mode,
                   'selection': _json_safe(s.get('interpolation_mode'))})
    with state.studio_lock:
        state.studio['interpolation_error'] = result
    return result


# =====================================================================
# P1 (0.4.1) endpoints: reference planes, library browser, export,
# library ops, repeatability verification, deviation card
# =====================================================================
def studio_reference_planes(state, body):
    """F-10: list the six standard reference planes and validate a chosen setup.

    A setup is validated only when ``reference_plane`` (and optionally
    ``deembed_s2p``) is supplied.  Library frequencies come from the explicit
    ``library_frequencies_hz`` list, else from the currently loaded database —
    so the wizard can gate a de-embedding S2P before calibration runs.
    """
    setup = None
    if body.get('reference_plane') or body.get('deembed_s2p'):
        reference_plane = str(body.get('reference_plane') or 'standard')
        deembed_s2p = str(body.get('deembed_s2p') or '')
        library_freqs = body.get('library_frequencies_hz')
        if library_freqs is None and state.studio.get('data') is not None:
            library_freqs = [float(f) for f in state.studio['data']['frequencies']]
        tol_hz = float(body.get('tol_hz', 1.0))
        setup = validate_reference_setup(reference_plane, deembed_s2p,
                                         library_freqs, tol_hz=tol_hz)
    return {'planes': [dict(p) for p in REFERENCE_PLANES],
            'reference_plane_ids': list(REFERENCE_PLANE_IDS),
            'setup': setup}


def studio_export(state, body):
    """F-13: export the loaded calibration library to a standard format.

    ``format`` ∈ tef | csv | s2p | s2p_library | hdf5.  The source database is
    never mutated; the summary returned describes what was written.
    """
    db = state.studio.get('database')
    if db is None or not db.path.is_file():
        raise RuntimeError('no calibration database loaded')
    data = db.load()
    meta = db.read_metadata()
    fmt = str(body.get('format', 'tef')).lower()
    out = body.get('path')
    if not out:
        raise ValueError('output path is required')
    valid_only = bool(body.get('valid_only', True))
    freq_index = int(body.get('freq_index', 0))
    if fmt == 'tef':
        return export_tef(out, data, meta, valid_only=valid_only)
    if fmt == 'csv':
        return export_csv(out, data, meta, valid_only=valid_only)
    if fmt == 's2p':
        return export_s2p(out, data, int(body.get('point_index', 0)),
                          freq_index=freq_index)
    if fmt == 's2p_library':
        return export_s2p_library(out, data, freq_index=freq_index,
                                  valid_only=valid_only)
    if fmt == 'hdf5':
        return export_hdf5_copy(str(db.path), out)
    raise ValueError(f'unknown export format: {fmt}')


def studio_library_derive(state, body):
    """F-14: post-calibration library transformation (immutable source).

    Every action writes a *brand new* library carrying lineage back to the
    parent (immutable-source discipline): ``truncate_gamma``, ``truncate_travel``,
    ``reverse_probe``, ``split_frequency``, ``apply_fixture`` (embed/deembed).
    """
    db = state.studio.get('database')
    if db is None or not db.path.is_file():
        raise RuntimeError('no calibration database loaded')
    action = str(body.get('action', '')).lower()
    out = body.get('path')
    if not out:
        raise ValueError('output path is required')
    if action == 'truncate_gamma':
        return truncate_by_gamma(db, out, float(body['gmax']))
    if action == 'truncate_travel':
        bounds = {k: (float(v[0]), float(v[1]))
                  for k, v in dict(body['axis_bounds']).items()}
        return truncate_by_travel(db, out, bounds)
    if action == 'reverse_probe':
        return reverse_probe(db, out, str(body.get('axis', 'X')))
    if action == 'split_frequency':
        return split_by_frequency(db, out)
    if action == 'apply_fixture':
        return apply_fixture(db, out, str(body['fixture_s2p']),
                             direction=str(body.get('direction', 'embed')))
    raise ValueError(f'unknown library action: {action}')


def studio_library_scan(state, body):
    """F-12: list every ``*.h5`` calibration library under a directory."""
    directory = body.get('directory')
    if not directory:
        raise ValueError('directory is required')
    return scan_libraries(directory, recursive=bool(body.get('recursive', False)))


def studio_library_compare(state, body):
    """F-12: diff two calibration libraries (point count, |Γ|max, mean |ΔΓ|)."""
    a = body.get('path_a')
    b = body.get('path_b')
    if not a or not b:
        raise ValueError('path_a and path_b are required')
    return compare_libraries(a, b, frequency_hz=body.get('frequency_hz'))


def studio_repeatability(state, body):
    """F-20: analyse N repeat measurements of (nominally) the same target."""
    measurements = body.get('measurements')
    if not measurements:
        raise ValueError('measurements list is required')
    parsed = []
    for m in measurements:
        rec = {'gamma': complex(float(m['gamma_re']), float(m['gamma_im']))}
        if 'target_gamma_re' in m and 'target_gamma_im' in m:
            rec['target_gamma'] = complex(float(m['target_gamma_re']),
                                          float(m['target_gamma_im']))
        if m.get('position') is not None:
            rec['position'] = [float(v) for v in m['position']]
        if m.get('label') is not None:
            rec['label'] = str(m['label'])
        if m.get('point_index') is not None:
            rec['point_index'] = int(m['point_index'])
        parsed.append(rec)
    target = None
    if 'target_gamma_re' in body and 'target_gamma_im' in body:
        target = complex(float(body['target_gamma_re']),
                         float(body['target_gamma_im']))
    return build_repeatability_report(
        parsed, z0=float(body.get('z0', 50.0)),
        tolerance=float(body.get('tolerance', 0.05)), target_gamma=target,
        frequency_hz=body.get('frequency_hz'))


def studio_live_smith(state, body):
    """Real-time Γ cloud for the acquisition page's Smith chart (F-11).

    Called on the UI's 700 ms poll while a sweep runs.  Every *committed* point
    is plotted at the requested frequency; the most recently written one is
    returned separately so the UI can highlight the moving tip of the cloud.
    A library that is still being created (or has no valid points yet) yields
    empty arrays instead of raising, so the poll never logs spurious errors.
    """
    s = state.studio
    db = s['database']
    empty = {'points': [], 'latest': None, 'frequency_hz': 0.0,
             'frequencies': [], 'completed': 0, 'total_points': 0}
    if db is None or not db.path.exists():
        return empty
    frequency = body.get('frequency')
    try:
        frequency = float(frequency) if frequency not in (None, '') else None
    except (TypeError, ValueError):
        frequency = None
    try:
        indices, gammas, used, freqs = db.live_smith_snapshot(frequency)
        completed, total_points = db.progress_counts()
    except Exception:  # noqa: BLE001 - a half-written library must not kill the poll
        return empty
    points = [{'re': float(g.real), 'im': float(g.imag), 'index': int(idx)}
              for g, idx in zip(gammas, indices) if np.isfinite(g)]
    return {
        'points': points,
        'latest': points[-1] if points else None,
        'frequency_hz': float(used),
        'frequencies': [float(f) for f in freqs],
        'completed': int(completed),
        'total_points': int(total_points),
    }


def studio_progress_cloud(state, body):
    """F-11: the acquisition grid as a layered point cloud.

    Reads the live HDF5 library, so the cloud reflects what has actually been
    *written* — a point is only drawn as done after it passed the measurement
    and was committed to disk.
    """
    database = state.studio.get('database')
    if database is None or not database.path.is_file():
        raise RuntimeError('no calibration database loaded')
    data = database.load()
    return progress_cloud(data, data.get('errors'))


def studio_repeatability_auto(state, body):
    """F-20: drive the A→B→A cycle and report the repeatability.

    This one *moves the machine*.  The caller is responsible for asking the
    operator first — the desktop page puts a second confirmation in front of
    the button, and the web API documents the same requirement.

    ``a_index`` / ``b_index`` select the two positions out of the loaded
    library; by default the first and the last valid point are used, which
    maximises the travel and therefore the hysteresis the test can show.
    """
    s = state.studio
    if s['calibration_state'] in _BUSY_STATES:
        raise RuntimeError(
            f"calibration is {s['calibration_state']}; wait for it to finish "
            f"or cancel before starting a repeatability run")
    if s['data'] is None or s['planner'] is None or s['vna'] is None:
        raise RuntimeError('load a calibration and initialise the hardware first')
    valid = np.flatnonzero(np.asarray(s['data']['valid'], bool))
    if len(valid) < 2:
        raise ValueError('two valid positions required')
    positions = np.asarray(s['data']['positions'], float)
    a_index = int(body.get('a_index', valid[0]))
    b_index = int(body.get('b_index', valid[-1]))
    if not (0 <= a_index < len(positions) and 0 <= b_index < len(positions)):
        raise ValueError('position index out of range')
    repeats = int(body.get('repeats', 3))
    if not 1 <= repeats <= 20:
        raise ValueError('repeats must be 1..20')
    tolerance = float(body.get('tolerance', 0.08))
    z0 = float(body.get('z0', 50.0))
    frequency_hz = body.get('frequency_hz')
    frequency_hz = (float(frequency_hz) if frequency_hz is not None else None)
    position_a = positions[a_index]
    position_b = positions[b_index]
    target = body.get('target_gamma')
    target_gamma = (complex(float(target['re']), float(target['im']))
                    if target else None)

    def run_repeatability():
        with state.studio_lock:
            state.studio['calibration_state'] = 'validating'
        try:
            collected = auto_repeatability_measurements(
                s['planner'], s['vna'], position_a, position_b,
                repeats=repeats, frequency_hz=frequency_hz,
                target_gamma=target_gamma)
            records = []
            for item in collected['measurements']:
                records.append({
                    'gamma_re': item['gamma'].real,
                    'gamma_im': item['gamma'].imag,
                    'position': item['position'],
                    'label': item['label'],
                })
            report = build_repeatability_report(
                records, z0=z0, tolerance=tolerance,
                target_gamma=target_gamma, frequency_hz=frequency_hz)
            report['kind'] = 'repeatability_auto'
            report['hysteresis'] = collected['hysteresis']
            report['hysteresis_max'] = max(
                (item['delta_gamma'] for item in collected['hysteresis']),
                default=0.0)
            report['a_index'] = a_index
            report['b_index'] = b_index
            if s['database'] is not None:
                s['database'].save_validation(report)
            with state.studio_lock:
                state.studio['validation_report'] = report
                state.studio['calibration_state'] = 'validated'
        except Exception as exc:  # noqa: BLE001 - the worker must not die silently
            with state.studio_lock:
                state.studio['calibration_state'] = 'failed'
                state.studio['calibration_error'] = f'{type(exc).__name__}: {exc}'

    thread = threading.Thread(target=run_repeatability, daemon=True)
    with state.studio_lock:
        if s['calibration_thread'] is not None and s['calibration_thread'].is_alive():
            raise RuntimeError('an operation is already running')
        s['calibration_thread'] = thread
    thread.start()
    return {'state': 'validating', 'repeats': repeats,
            'a_index': a_index, 'b_index': b_index}


def studio_reference_shift(state, body):
    """P2: refer the loaded library to another reference plane.

    The network between the DUT and the measurement plane is described either
    as a **line** (length, velocity factor, loss) or as a **fixture** (an
    existing ``.s2p``).  ``direction`` says whether the network is taken out
    (``deembed`` — it is already part of the measurement) or put in
    (``embed``).

    The source library is never modified: the result is a new library that
    records the plane it now refers to and its lineage.
    """
    database = state.studio.get('database')
    source_path = str(body.get('source_path') or '')
    if source_path:
        database = CalibrationDatabase(source_path)
    if database is None or not database.path.is_file():
        raise RuntimeError('no calibration database loaded')
    out = body.get('path')
    if not out:
        raise ValueError('output path is required')
    direction = str(body.get('direction', 'deembed')).strip().lower()
    if direction not in ('embed', 'deembed'):
        raise ValueError("direction must be 'embed' or 'deembed'")
    z0 = float(body.get('z0', 50.0))
    data = database.load()
    frequencies = np.asarray(data['frequencies'], float)

    kind = str(body.get('kind', 'line')).strip().lower()
    if kind == 'line':
        length_mm = float(body.get('length_mm', 0.0))
        velocity = float(body.get('velocity_factor', 0.66))
        alpha = float(body.get('alpha_np_per_m', 0.0))
        network = line_s_parameters(length_mm / 1000.0, frequencies, z0=z0,
                                    velocity_factor=velocity,
                                    alpha_np_per_m=alpha)
        description = (f'线路 {length_mm:g} mm · 速度系数 {velocity:g} · '
                       f'损耗 {alpha:g} Np/m')
    elif kind == 'fixture':
        s2p = str(body.get('fixture_s2p') or '')
        if not s2p:
            raise ValueError('fixture_s2p is required for a fixture shift')
        network = s2p_at_frequencies(s2p, frequencies,
                                     tol_hz=float(body.get('tol_hz', 1.0)))
        description = f'夹具 {Path(s2p).name}'
    else:
        raise ValueError("kind must be 'line' or 'fixture'")

    report = reference_shift_report(
        database, out, network,
        reason=str(body.get('reason') or f'reference shift ({direction})'),
        reference_plane=str(body.get('reference_plane') or ''),
        z0=z0, direction=direction)
    report['network'] = description
    report['direction'] = direction
    return report


def studio_patterns_list(state, body):
    """P2: every stored target pattern."""
    return {'patterns': list_patterns()}


def studio_patterns_save(state, body):
    """P2: store a target pattern."""
    name = str(body.get('name') or '').strip()
    kind = str(body.get('kind') or 'points')
    parameters = body.get('parameters') or {}
    points = body.get('points')
    record = save_pattern(name, kind, parameters, points)
    return record


def studio_patterns_delete(state, body):
    """P2: remove a target pattern."""
    path = str(body.get('path') or '')
    if not path:
        raise ValueError('path is required')
    return {'deleted': delete_pattern(path)}


def studio_pattern_resolve(state, body):
    """P2: resolve a pattern's points against the loaded calibration.

    For every target Γ the nearest calibrated point is reported, so the
    operator sees *before* running anything whether the whole pattern is
    reachable and how far the worst target is from a measured point.
    """
    s = state.studio
    if s['search_engine'] is None:
        raise RuntimeError('load a calibration first')
    points = body.get('points') or []
    tolerance = float(body.get('tolerance', 0.08))
    frequency = float(body.get('frequency', s.get('frequency_hz', 5e9)))
    z0 = s['search_engine'].z0
    rows = []
    for index, item in enumerate(points):
        if isinstance(item, dict):
            gamma = complex(float(item.get('re', 0.0)), float(item.get('im', 0.0)))
        else:
            gamma = complex(float(item[0]), float(item[1]))
        candidates = s['search_engine'].search(frequency, gamma, k=1,
                                               tolerance=tolerance)
        if candidates:
            best = candidates[0]
            rows.append({
                'index': index,
                'gamma_re': gamma.real,
                'gamma_im': gamma.imag,
                'gamma_label': format_gamma(gamma),
                'impedance_re': float(RF.gamma_to_z(gamma, z0).real),
                'impedance_im': float(RF.gamma_to_z(gamma, z0).imag),
                'point_index': int(best.index),
                'position': [float(v) for v in best.position],
                'error': float(best.error),
                'reachable': bool(best.reachable),
                'measured_gamma_re': float(best.gamma.real),
                'measured_gamma_im': float(best.gamma.imag),
            })
        else:
            rows.append({
                'index': index,
                'gamma_re': gamma.real,
                'gamma_im': gamma.imag,
                'gamma_label': format_gamma(gamma),
                'impedance_re': float(RF.gamma_to_z(gamma, z0).real),
                'impedance_im': float(RF.gamma_to_z(gamma, z0).imag),
                'point_index': None,
                'position': None,
                'error': None,
                'reachable': False,
                'measured_gamma_re': None,
                'measured_gamma_im': None,
            })
    worst = max((row['error'] for row in rows if row['error'] is not None),
                default=None)
    return {
        'rows': rows,
        'count': len(rows),
        'reachable': sum(1 for row in rows if row['reachable']),
        'worst_error': worst,
        'all_reachable': bool(rows) and all(row['reachable'] for row in rows),
        'frequency_hz': frequency,
    }


def studio_audit(state, body):
    """P2: the calibration audit trail.

    Read-only: it gathers the library headers, the validation records they
    carry and the operation log, so an engineer can prove months later what
    the machine actually measured.
    """
    directory = body.get('directory')
    if not directory:
        from ..appdirs import calibration_dir
        directory = calibration_dir()
    log_path = body.get('operation_log')
    if not log_path:
        from ..appdirs import log_dir
        log_path = log_dir() / 'operations.jsonl'
    return audit_report(directory, log_path)


def studio_frequency_preview(state, body):
    """Preview the frequency grid the current wizard settings would produce.

    The operator picks a *mode* (one spot frequency, a sweep, harmonics, an
    explicit list); each mode uses a different subset of the frequency fields.
    This endpoint answers "what would this actually measure?" without
    touching the instrument, so the UI can show the plan as it is edited.
    """
    try:
        plan = _frequency_plan_from_body(body)
    except Exception as exc:  # noqa: BLE001 - an invalid plan is a normal state
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}',
                'mode': str(body.get('frequency_mode') or 'fo_list')}
    freqs = [float(v) for v in plan.get('frequencies_hz', [])]
    step = None
    if len(freqs) >= 2:
        step = freqs[1] - freqs[0]
    return {
        'ok': True,
        'mode': plan.get('mode'),
        'count': len(freqs),
        'frequencies_hz': freqs,
        'start_hz': freqs[0] if freqs else None,
        'stop_hz': freqs[-1] if freqs else None,
        'step_hz': step,
        'uniform': bool(plan.get('uniform')),
        'harmonic_numbers': list(plan.get('harmonic_numbers', [])),
        'warnings': list(plan.get('warnings', [])),
    }


def studio_prerequisite_criteria(state, body):
    """The judging criteria of the 6 prerequisite checks.

    Read-only: it returns how each check decides and what counts as a hard
    block, so the operator can see *why* a check could fail without having to
    read the source.
    """
    return {'criteria': criteria_report(), 'text': format_criteria_text()}


def studio_deviation(state, body):
    """F-19/F-24: target-vs-measured comparison card with band grading.

    Returns a side-by-side Γ / Z / VSWR / return-loss, the VSWR deviation, the
    impedance delta, the |Γ| reachability band (low/mid/high) and the
    suggested (relaxed) tolerance for the high-|Γ| region.
    """
    predicted = complex(float(body['predicted_gamma_re']),
                        float(body['predicted_gamma_im']))
    measured = complex(float(body['measured_gamma_re']),
                      float(body['measured_gamma_im']))
    report = deviation_report(predicted, measured,
                             z0=float(body.get('z0', 50.0)),
                             tolerance=float(body.get('tolerance', 0.05)))
    report['gamma_band'] = gamma_band(report['measured']['gamma_mag'])
    return report


# =====================================================================
# Route table
# =====================================================================
ROUTES = {
    # hardware
    'GET /api/hardware/status': hw_status,
    'POST /api/hardware/connect': hw_connect,
    'POST /api/hardware/disconnect': hw_disconnect,
    'POST /api/hardware/search': hw_search,
    'GET /api/hardware/settings': hw_settings,
    'POST /api/hardware/settings/save': hw_settings_save,
    'POST /api/hardware/model': hw_set_model,
    'POST /api/hardware/mode': hw_set_mode,
    'GET /api/hardware/params': hw_params,
    'POST /api/hardware/params/read': hw_params_read,
    'POST /api/hardware/params/write': hw_params_write,
    'POST /api/hardware/params/load': hw_params_load,
    'POST /api/hardware/move': hw_move,
    'POST /api/hardware/jog': hw_jog,
    'POST /api/hardware/home': hw_home,
    'POST /api/hardware/stop': hw_stop,
    'POST /api/hardware/probe': hw_probe,
    'POST /api/hardware/apply_scale': hw_apply_scale,
    'POST /api/hardware/estop': hw_estop,
    'POST /api/hardware/reset_estop': hw_reset_estop,
    'GET /api/hardware/axis_status': hw_axis_status,
    'POST /api/hardware/errors': hw_errors,
    'POST /api/hardware/inject_fault': hw_inject_fault,
    'POST /api/hardware/generate_data': hw_generate_virtual_data,
    # operation log
    'GET /api/log/recent': oplog_recent,
    'POST /api/log/recent': oplog_recent,
    'POST /api/log/clear': oplog_clear,
    'POST /api/log/export': oplog_export,
    # VNA (real, TCP/IP SCPI)
    'GET /api/vna/status': vna_status,
    'GET /api/vna/vendors': vna_vendors,
    'POST /api/vna/connect': vna_connect,
    'POST /api/vna/disconnect': vna_disconnect,
    'POST /api/vna/idn': vna_idn,
    'POST /api/vna/set_sweep': vna_set_sweep,
    'POST /api/vna/set_power': vna_set_power,
    'POST /api/vna/set_ifbw': vna_set_ifbw,
    'POST /api/vna/trigger': vna_trigger,
    'POST /api/vna/fetch': vna_fetch,
    # studio
    'GET /api/studio/overview': studio_overview,
    'POST /api/studio/init_hardware': studio_init_hardware,
    'GET /api/studio/position': studio_position,
    'POST /api/studio/jog': studio_jog,
    'POST /api/studio/retract': studio_retract,
    'POST /api/studio/estop': studio_estop,
    'POST /api/studio/reset_home': studio_reset_home,
    'POST /api/studio/home': studio_home,
    'POST /api/studio/setup': studio_setup,
    # P0 (0.4.0)
    'GET /api/studio/prerequisites': studio_prerequisites,
    'POST /api/studio/prerequisites': studio_prerequisites,
    'GET /api/vna/calibration': vna_calibration_status,
    'POST /api/vna/calibration': vna_calibration_set,
    'POST /api/studio/frequency_plan': studio_frequency_plan,
    'GET /api/studio/metadata': studio_metadata,
    'GET /api/studio/interpolation': studio_interpolation,
    'POST /api/studio/plan/preview': studio_plan_preview,
    'POST /api/studio/plan/refine': studio_plan_refine,
    'POST /api/studio/run': studio_run,
    'POST /api/studio/pause': studio_pause,
    'POST /api/studio/cancel': studio_cancel,
    'GET /api/studio/task': studio_task,
    'POST /api/studio/open_db': studio_open_db,
    'GET /api/studio/database': studio_database,
    'POST /api/studio/smith': studio_smith,
    'POST /api/studio/tune': studio_tune,
    'POST /api/studio/move_candidate': studio_move_candidate,
    'POST /api/studio/move_tuned': studio_move_tuned,
    'POST /api/studio/verify': studio_verify,
    'POST /api/studio/move_to_gamma': studio_move_to_gamma,
    'POST /api/studio/validate': studio_validate,
    'POST /api/studio/validate_interpolation': studio_validate_interpolation,
    'GET /api/studio/interpolation_report': lambda state, body: state.studio.get(
        'interpolation_report'),
    'GET /api/studio/validation_report': studio_validation_report,
    # P1 (0.4.1)
    'POST /api/studio/reference_planes': studio_reference_planes,
    'POST /api/studio/export': studio_export,
    'POST /api/studio/library/derive': studio_library_derive,
    'POST /api/studio/library/scan': studio_library_scan,
    'POST /api/studio/library/compare': studio_library_compare,
    'POST /api/studio/repeatability': studio_repeatability,
    'POST /api/studio/repeatability_auto': studio_repeatability_auto,
    'POST /api/studio/reference_shift': studio_reference_shift,
    'GET /api/studio/patterns': studio_patterns_list,
    'POST /api/studio/patterns': studio_patterns_save,
    'POST /api/studio/patterns/delete': studio_patterns_delete,
    'POST /api/studio/patterns/resolve': studio_pattern_resolve,
    'POST /api/studio/audit': studio_audit,
    'POST /api/studio/deviation': studio_deviation,
    'GET /api/studio/prerequisite_criteria': studio_prerequisite_criteria,
    'POST /api/studio/frequency_preview': studio_frequency_preview,
    'GET /api/studio/progress_cloud': studio_progress_cloud,
    'GET /api/studio/live_smith': studio_live_smith,
}

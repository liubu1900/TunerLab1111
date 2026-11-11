"""Studio initialisation for real hardware, with the scale gate in the right unit.

``handlers.studio_init_hardware`` refuses a real calibration until every axis'
``feedback_scale`` is 1.0 ±0.05::

    mismatched = [name for name in state.hw.axes
                  if abs(state.hw.settings.axes[name].feedback_scale - 1.0) > 0.05]

That assumption predates this machine.  The controller reports ``MPOS`` in 10 µm
counts while ``DPOS`` is millimetres, so a correctly calibrated axis carries
``feedback_scale = 100`` (100 counts per millimetre).  The gate therefore rejected
a machine whose scale self-check had just passed with a ratio of 1.00 — the check
was not wrong about the calibration, it was asking the wrong question.

Only the ``real`` branch is re-implemented here.  It keeps the original safety
checks (declared travel box, homed axes, the one controller link the motion page
already owns) and expresses the gate as "the scale must be one of the MPOS unit
ratios this build understands".  The ``mock`` branch still goes to the original
handler untouched, so nothing about the simulated workflow changes.
"""
from __future__ import annotations

from tuner.motion import MotionPlanner
from tuner.operation_log import operation_log
from tuner.vna import MockVNAController
from tuner.vna_drivers import DEFAULT_GPIB_RESOURCE, create_vna
from tuner.web import handlers
from tuner.web.state import DEFAULT_FREQUENCIES
from tuner.zmotion_motion import build_tuner_config

from .motion_adapter import MPOS_COUNTS_PER_MM

__all__ = ['init_hardware', 'VALID_FEEDBACK_SCALES']

#: ``feedback_scale`` values that are physically meaningful on this controller:
#: 100 when MPOS is reported in 10 µm counts (this machine), 1.0 for a
#: millimetre-reporting controller.  The scale self-check tells the operator
#: which one applies; the gate only has to reject anything else.
VALID_FEEDBACK_SCALES = (1.0, MPOS_COUNTS_PER_MM)


def init_hardware(state, body: dict):
    """Same contract as :func:`handlers.studio_init_hardware`."""
    motion_source = str(body.get('motion_source', 'mock')).strip().lower()
    if motion_source not in ('mock', 'real'):
        raise ValueError("motion_source must be 'mock' or 'real'")
    if motion_source == 'mock':
        return handlers.studio_init_hardware(state, body)
    return _init_real(state, body)


def _init_real(state, body: dict):
    # Reuse the link the motion page owns: two handles on one controller is how
    # the old console managed to wedge the DLL.
    controller = state.hw.require_controller()

    x_limits = (float(body.get('x_min', 0.0)), float(body.get('x_max', 10.0)))
    z_limits = (float(body.get('z_min', 0.0)), float(body.get('z_max', 1.0)))
    safe_z = float(body.get('safe_z', z_limits[0]))
    if x_limits[0] >= x_limits[1] or z_limits[0] >= z_limits[1]:
        raise ValueError('travel limits must be ordered and non-empty')

    _check_feedback_scales(state)

    # The operator's declared travel box wins over the controller's factory soft
    # limits (±2e8), which are far wider than the machine.
    config = build_tuner_config(
        state.hw.settings.axes, x_limits, z_limits, safe_z,
        axis_count=state.hw.settings.axis_count)
    # The desktop session raises the motion budget to 60 s (X is 380 mm at
    # 10 mm/s); ``build_tuner_config`` leaves the 5 s default, which reports a
    # long layer-boundary traverse as a timeout while the axis is still moving.
    # Inherit the controller's actual budget so the calibration grid uses it.
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

    vna, vna_source_used = _build_vna(state, body, config, motion)

    with state.studio_lock:
        state.studio['config'] = config
        state.studio['motion'] = motion
        state.studio['planner'] = planner
        state.studio['vna'] = vna
        state.studio['vna_source'] = vna_source_used
        state.studio['motion_source'] = 'real'
        state.studio['hardware_context'] = state.hw.context()
        state.studio['homed_axes'] = homed_axes
        state.studio['manager'] = None
        state.studio['database'] = None
        state.studio['data'] = None
        state.studio['search_engine'] = None
        state.studio['last_candidates'] = []
        state.studio['validation_report'] = None
        state.studio['calibration_state'] = 'idle'
        state.studio['calibration_progress'] = (0, 0)
    return state.studio_overview()


def _check_feedback_scales(state) -> None:
    """Refuse only a scale that no MPOS unit on this machine would produce."""
    problems = []
    for name in state.hw.axes:
        params = state.hw.settings.axes.get(name)
        if params is None:
            continue
        scale = float(params.feedback_scale)
        if not any(abs(scale - value) <= 0.05 * value
                   for value in VALID_FEEDBACK_SCALES):
            problems.append(f'{name}（feedback_scale={scale:g}）')
    if problems:
        raise RuntimeError(
            '实机运动前必须先确认标度：' + '、'.join(problems) + ' 与 MPOS 单位不符。'
            f'本机 MPOS 为 10 µm 计数，feedback_scale 应为 {MPOS_COUNTS_PER_MM:g}；'
            '请到「轴移动控制」做一次标度自检，比值接近 1 即可直接继续。')


def _build_vna(state, body: dict, config, motion):
    """Real instrument when asked, otherwise the synthetic one (original rules)."""
    if body.get('vna_source', 'mock') == 'real':
        vendor = str(body.get('vna_vendor') or 'auto')
        host = str(body.get('vna_ip') or '192.168.2.111')
        port = int(body.get('vna_port', 5025))
        timeout = float(body.get('vna_timeout_ms', 10000)) / 1000.0
        interface = str(body.get('vna_interface') or 'tcpip').strip().lower()
        resource = str(body.get('vna_resource') or DEFAULT_GPIB_RESOURCE).strip()
        start = float(body.get('vna_start_hz', 1e9))
        stop = float(body.get('vna_stop_hz', 20e9))
        points = int(body.get('vna_points', 401))
        # A dialect pinned by the SCPI debug tool also applies to a real-VNA
        # library build, otherwise the wizard would still collect -113s.
        dialect = body.get('vna_dialect')
        if dialect is None and state is not None:
            dialect = (getattr(state, 'vna', {}) or {}).get('config', {}).get('dialect')
        vna = create_vna(vendor, host, port, timeout,
                         interface=interface, resource=resource,
                         dialect=dialect or None)
        try:
            vna.connect()
            vna.set_sweep(start, stop, points)
        except Exception:
            try:
                vna.close()
            except Exception:
                pass
            raise
        return vna, 'real'
    vna = MockVNAController(config, motion, DEFAULT_FREQUENCIES)
    vna.initialize()
    return vna, 'mock'

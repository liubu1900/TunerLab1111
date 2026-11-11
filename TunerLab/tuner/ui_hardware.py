"""Hardware control pages: communication setup, axis motion and axis parameters.

The three widgets are independent pages so they can be hosted by the main window or by
a standalone dialog. Each page owns its own UI only; all hardware access goes through
the injected :class:`HardwareSession`, which is the single owner of the controller link.

Safety notes shown in the UI:

* the emergency stop is latched and must be reset explicitly,
* each axis is independent: X is not gated on the probe positions
  (``TunerConfig.independent_axes`` defaults to True),
* the software emergency stop does not replace a hardware interlock; with the
  retract-before-X interlock removed, physical collision avoidance is entirely
  the operator's and the hardware interlock's responsibility.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer, Signal, QObject, QRunnable, QThreadPool
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .axis_params import (
    AxisParams, CONTROLLER_AXES, TUNER_MODELS,
    default_axis_params, tuner_axes, COORDINATE_FIELDS,
)
from .motion import MotionError
from .operation_log import operation_log
from .settings import SettingsStore, TunerSettings
from .tuner_units import TunerUnit, axis_map_text
from .ui_theme import numeric_font, text_font
from .mock_hardware import MockZmotionController, build_virtual_config
from .zmotion import ZmotionController, ZmotionError, probe_controller
from .zmotion_motion import ZmotionMotionController, build_tuner_config

__all__ = ['HardwareSession', 'CommunicationPage', 'AxisMotionPage',
           'AxisParameterPage', 'OperationLogPage']

#: Axis parameter columns shown in the parameter table: label, kind, default.
_PARAM_COLUMNS = (
    ('atype', '轴类型 ATYPE', 'int'),
    ('units', '脉冲当量 UNITS', 'float'),
    ('speed', '速度 SPEED (mm/s)', 'float'),
    ('accel', '加速度 ACCEL (mm/s²)', 'float'),
    ('decel', '减速度 DECEL (mm/s²)', 'float'),
    ('creep', '回零爬行 CREEP (mm/s)', 'float'),
    ('jog_speed', '点动速度 JOG (mm/s)', 'float'),
    ('invert_step', '脉冲模式 INVERT_STEP', 'int'),
    ('datum_in', '原点输入口 DATUM_IN', 'int'),
    ('fs_limit', '正向限位 FS_LIMIT (mm)', 'float'),
    ('rs_limit', '负向限位 RS_LIMIT (mm)', 'float'),
    ('datum_mode', '回零模式 DATUM_MODE', 'int'),
    ('feedback_scale', '反馈标度 FEEDBACK_SCALE', 'float'),
)


class HardwareSession(QObject):
    """Owns the controller connection and the shared settings.

    This is a non-visual coordinator: it is the single owner of the controller link and
    the settings file, and it notifies the pages through signals.
    """

    status_changed = Signal(str)
    connection_changed = Signal(bool)
    settings_changed = Signal()
    position_changed = Signal(object)

    def __init__(self, store: SettingsStore | None = None, parent=None):
        super().__init__(parent)
        self.store = store or SettingsStore()
        self.settings: TunerSettings = self._load_settings()
        self.link: ZmotionController | None = None
        self.controller: ZmotionMotionController | None = None
        self._calibration_stale: bool = False

    # -- settings --------------------------------------------------------
    def _load_settings(self) -> TunerSettings:
        try:
            return self.store.load()
        except ValueError:
            # A broken file must not block startup; the operator is told and the
            # defaults are used until the file is repaired.
            return TunerSettings()

    def save_settings(self) -> Path:
        path = self.store.save(self.settings)
        self.settings_changed.emit()
        return path

    def set_connection_settings(self, ip: str, connect_on_start: bool | None = None) -> None:
        from .settings import ConnectionSettings
        self.settings.connection = ConnectionSettings(
            ip=ip,
            connect_on_start=(self.settings.connection.connect_on_start
                              if connect_on_start is None else connect_on_start),
        )
        self.save_settings()

    #: Fields whose change moves the physical reference of a position, so the
    #: axis must be re-homed before it may move again.
    _HOME_INVALIDATING = {'units', 'invert_step', 'datum_in', 'datum_mode',
                          'feedback_scale'}

    def set_axis_parameters(self, axis_params: dict[str, AxisParams]) -> None:
        """Merge new per-axis parameters into the stored settings and persist them.

        Only the axes present in ``axis_params`` are replaced; the parameters of the
        other axes are preserved, so saving one axis never silently drops the others.

        Editing a coordinate-frame field (UNITS, direction, limits, origin or feedback
        scale) clears the affected axes' homing state -- the machine must be re-homed
        before it may move -- and marks any loaded calibration as stale, because a
        saved position no longer maps to the same physical point.
        """
        merged = dict(self.settings.axes)
        home_invalidated: set[str] = set()
        calib_invalidated = False
        for name, params in axis_params.items():
            previous = merged.get(name)
            merged[name] = params
            if previous is None:
                continue
            changed = {f for f in COORDINATE_FIELDS
                       if getattr(previous, f, None) != getattr(params, f, None)}
            if changed:
                calib_invalidated = True
            if changed & self._HOME_INVALIDATING:
                home_invalidated.add(name)
        self.settings.axes = merged
        # Keep the per-tuner map in sync, so switching to the other tuner and
        # back does not resurrect the pre-edit parameters.
        self.settings.axes_by_unit[self.settings.active_unit] = dict(merged)
        if home_invalidated and self.controller is not None:
            for name in home_invalidated:
                self.controller.homed_axes.discard(name)
            self.controller.homed = (
                len(self.controller.homed_axes) == len(self.axes))
        if calib_invalidated:
            self.mark_calibration_stale()
        self.save_settings()

    def mark_calibration_stale(self) -> None:
        """Flag that any loaded calibration no longer matches the machine.

        Called when geometry, limits or scale change.  The desktop UI reacts to
        ``settings_changed`` to clear stale tuning results; the web console clears
        its ``last_tuned`` on the next relevant request.
        """
        self._calibration_stale = True
        self.settings_changed.emit()

    # -- tuner model -----------------------------------------------------
    @property
    def axes(self) -> tuple[str, ...]:
        """Axes of the active tuner model."""
        return self.settings.active_axes

    @property
    def probe_count(self) -> int:
        """Number of probe axes, i.e. the axes besides the shared X axis."""
        return self.settings.probe_count

    # -- tuner units -----------------------------------------------------
    @property
    def units(self) -> dict[str, TunerUnit]:
        """All configured tuners, keyed by their unit key."""
        return self.settings.units

    @property
    def active_unit(self) -> TunerUnit:
        """The tuner the console currently commands."""
        return self.settings.unit

    def available_units(self) -> list[dict[str, object]]:
        """Serializable description of every configured tuner."""
        clashes = self.settings.overlapping_units()
        return [
            {
                'key': unit.key,
                'label': unit.label,
                'axis_base': unit.axis_base,
                'axis_count': unit.axis_count,
                'axes': list(unit.axes),
                'physical_axes': list(unit.physical_axes),
                'axis_map': unit.axis_map(),
                'op_base': unit.op_base,
                'datum_in_base': unit.datum_in_base,
                'active': unit.key == self.settings.active_unit,
                'overlaps': clashes.get(unit.key, []),
            }
            for unit in self.settings.units.values()
        ]

    def set_active_unit(self, key: str) -> None:
        """Select which tuner the console commands.

        The link is *not* reopened: one controller drives both tuners, so only
        the motion backend is rebuilt with the new tuner's physical axis block.
        The homing state is dropped, because the controller reports the homing
        of the new axes only after they were homed themselves.
        """
        key = str(key)
        if key not in self.settings.units:
            raise ValueError(f'unknown tuner unit {key!r}; '
                             f'expected one of {sorted(self.settings.units)}')
        if key == self.settings.active_unit:
            return
        previous = self.settings.unit
        self.settings.set_unit(key)
        self.save_settings()
        self._rebuild_controller()
        self.settings_changed.emit()
        self.connection_changed.emit(self.connected)
        operation_log.info(
            '系统', '切换 Tuner',
            f'{previous.label} → {self.active_unit.label} · '
            f'物理轴 {", ".join(str(a) for a in self.active_unit.physical_axes)}',
            unit=key, previous=previous.key)

    def _rebuild_controller(self) -> None:
        """Rebuild the motion backend for the active tuner.

        The link stays open.  Only the axis block, the axis set and the wiring
        offsets change, so this is cheap and does not disturb the machine.
        """
        if self.controller is None:
            return
        unit = self.active_unit
        count = unit.axis_count
        axes = self.settings.unit_axes()
        if self.controller.simulated:
            controller = MockZmotionController(
                build_virtual_config(axes, axis_count=count),
                axes, axis_count=count,
                axis_base=unit.axis_base, op_base=unit.op_base,
                link=self.controller.link)
            controller.initialize()
            controller.home()
            self.controller = controller
            # The link models the controller, not the tuner: it stays the
            # same object, only the axis block it answers for changes.
            self.link = controller.link
            return
        self.controller = ZmotionMotionController(
            build_tuner_config(axes, axis_count=count),
            self.link, axes, axis_count=count,
            axis_base=unit.axis_base, op_base=unit.op_base,
            datum_in_base=unit.datum_in_base)

    def set_axis_count(self, axis_count: int) -> None:
        """Switch the tuner model and persist it.

        The parameters of the axes that leave the model are kept, so switching
        back and forth does not lose a tuned axis.  The motion backend is
        rebuilt, because it caches the axis set it commands.
        """
        count = int(axis_count)
        if count not in TUNER_MODELS:
            raise ValueError(
                f'unsupported axis count {count}; expected one of {sorted(TUNER_MODELS)}')
        if count == self.settings.axis_count:
            return
        self.settings.set_unit_axis_count(self.settings.active_unit, count)
        self.save_settings()
        self._rebuild_controller()
        self.settings_changed.emit()
        clashes = self.settings.overlapping_units()
        if clashes:
            operation_log.warn(
                '系统', '切换 tuner 型号',
                f'{count} 轴后两台 Tuner 的物理轴有重叠，'
                '请确认接线（当前机器只运行一种型号）')
        operation_log.info('系统', '切换 tuner 型号',
                           f'{count} 轴: {", ".join(tuner_axes(count))}',
                           axis_count=count)

    # -- connection ------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self.link is not None and self.link.connected

    def connect(self, ip: str | None = None, mode: str | None = None) -> None:
        """Open the controller link and prepare the motion backend.

        ``mode='virtual'`` connects a simulated controller instead of the real
        one.  Everything the console does afterwards is identical, so an
        operator can rehearse a procedure without touching the machine.
        """
        target_mode = str(mode or self.mode).strip().lower()
        if target_mode not in ('real', 'virtual'):
            raise ValueError(f"unknown controller mode: {target_mode!r}")
        target_ip = ip or self.settings.connection.ip
        if target_mode == 'virtual':
            self._connect_virtual()
            return
        if self.link is None:
            self.link = ZmotionController()
        try:
            self.link.open(target_ip)
            self.settings.connection.ip = target_ip
            unit = self.active_unit
            axes = self.settings.unit_axes()
            self.controller = ZmotionMotionController(
                build_tuner_config(axes, axis_count=unit.axis_count),
                self.link, axes,
                axis_count=unit.axis_count,
                axis_base=unit.axis_base, op_base=unit.op_base,
                datum_in_base=unit.datum_in_base)
            self.controller.initialize()
        except Exception as exc:  # noqa: BLE001
            operation_log.exception('通讯', '连接控制器', exc, ip=target_ip)
            raise
        self.connection_changed.emit(True)
        message = (f'已连接 {target_ip} · 固件 {self.link.firmware or "未知"} · '
                   f'{unit.label} 轴 {", ".join(str(a) for a in unit.physical_axes)}')
        self.status_changed.emit(message)
        operation_log.info('通讯', '连接控制器',
                           f'{target_ip} · 固件 {self.link.firmware or "未知"} · '
                           f'{unit.label}',
                           ip=target_ip, firmware=self.link.firmware, unit=unit.key)

    def _connect_virtual(self) -> None:
        """Connect the simulated controller and seed it with plausible data."""
        unit = self.active_unit
        axes = self.settings.unit_axes()
        controller = MockZmotionController(
            build_virtual_config(axes, axis_count=unit.axis_count),
            axes, axis_count=unit.axis_count,
            axis_base=unit.axis_base, op_base=unit.op_base)
        controller.initialize()
        controller.home()
        self.controller = controller
        self.link = controller.link
        self.connection_changed.emit(True)
        axes_text = ', '.join(self.axes)
        message = (f'虚拟控制器已连接 · {unit.label} '
                   f'轴 {", ".join(str(a) for a in unit.physical_axes)} ({axes_text})')
        self.status_changed.emit(message)
        operation_log.info('通讯', '连接虚拟控制器',
                           f'{unit.label} 轴 {", ".join(str(a) for a in unit.physical_axes)}'
                           f' ({axes_text})，已自动回零并生成初始位置',
                           unit=unit.key, axis_count=unit.axis_count)

    @property
    def mode(self) -> str:
        """'real' or 'virtual'."""
        return self.settings.mode

    def set_mode(self, mode: str) -> None:
        """Switch between the real and the virtual controller.

        Switching drops the current link: the two controllers have nothing in
        common, and a half-open link would be the worst of both.
        """
        value = str(mode).strip().lower()
        if value not in ('real', 'virtual'):
            raise ValueError(f"unknown controller mode: {mode!r}")
        if value == self.settings.mode:
            return
        if self.connected:
            self.disconnect()
        self.settings.mode = value
        self.save_settings()
        operation_log.info('系统', '切换控制器模式',
                           '真实控制器' if value == 'real' else '虚拟控制器',
                           mode=value)
        self.settings_changed.emit()

    def disconnect(self) -> None:
        """Close the controller link."""
        if self.link is not None:
            self.link.close()
        self.controller = None
        self.connection_changed.emit(False)
        self.status_changed.emit('连接已断开')
        operation_log.info('通讯', '断开控制器', f'模式: {self.mode}')

    def controller_errors(self) -> dict:
        """Read the controller's own error number and the per-axis faults."""
        controller = self.require_controller()
        result = controller.read_controller_errors()
        faults = [entry for entry in result['axes'] if entry['status_fault']]
        if faults:
            operation_log.error('系统', '读取控制器错误码',
                                '；'.join(f"{e['axis']}: {e['status_text']}" for e in faults))
        else:
            operation_log.info('系统', '读取控制器错误码', result['text'])
        return result

    def apply_scale_correction(self, axis: str, units: float) -> dict:
        """Write a corrected ``UNITS`` value to the controller and store it.

        ``UNITS`` is the controller's pulse equivalent.  When it does not match the
        axis' real pulse equivalent, one commanded unit is not one millimetre and every
        calibration point and every soft limit is off by that factor.  Writing the
        corrected value makes the controller's user unit a millimetre.

        Changing ``UNITS`` moves the coordinate system, so the axis is marked as not
        homed afterwards and has to be homed again before it may move.
        """
        controller = self.require_controller()
        name = str(axis)
        if name not in self.axes:
            raise ValueError(f'unknown axis: {axis!r}')
        value = float(units)
        if value <= 0:
            raise ValueError('UNITS must be positive')
        params = dict(self.settings.axes)
        previous = params[name]
        controller.set_axis_units(name, value)
        updated = previous.to_dict()
        updated['units'] = value
        # The command scale now matches the feedback scale again.
        updated['feedback_scale'] = 1.0
        params[name] = AxisParams(**updated)
        self.set_axis_parameters(params)
        controller.axis_params = params
        controller.homed_axes.discard(name)
        controller.homed = len(controller.homed_axes) == len(self.axes)
        operation_log.warn('参数', '写入修正后的 UNITS',
                           f'{name}: UNITS {previous.units:g} → {value:g}，'
                           f'该轴需重新回零',
                           axis=name, units=value, previous_units=previous.units)
        return {
            'axis': name,
            'units': value,
            'previous_units': previous.units,
            'feedback_scale': 1.0,
            'needs_rehome': True,
        }

    def require_controller(self) -> ZmotionMotionController:
        if self.controller is None or not self.connected:
            raise MotionError('尚未连接控制器，请先在通讯设置页连接')
        return self.controller

    # -- axis parameters -------------------------------------------------
    def apply_parameters_to_controller(self) -> None:
        """Write the stored axis parameters to the controller."""
        controller = self.require_controller()
        controller.apply_axis_parameters(self.settings.axes)

    def read_parameters_from_controller(self) -> dict[str, dict[str, float]]:
        """Read the controller's current parameters."""
        controller = self.require_controller()
        return controller.read_axis_parameters()


class _MotionTask(QRunnable):
    """Run a blocking motion callable off the Qt UI thread.

    Manual jog / absolute positioning / homing / scale-probe all block while the
    controller reports ``IDLE``.  Running them on the UI thread freezes the event
    loop, so the operator cannot press *Stop* until the move finishes (P0-02).
    This task moves the work to a worker thread; its ``finished`` / ``failed``
    signals are queued back to the UI thread so the page can refresh and report.

    Stop / emergency-stop intentionally do *not* go through this task -- they are
    issued synchronously on the UI thread so they always preempt a running move.
    """

    class _Hub(QObject):
        finished = Signal()
        failed = Signal(object)

    def __init__(self, action):
        super().__init__()
        self._action = action
        self.hub = self._Hub()

    def run(self):
        try:
            self._action()
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI thread below
            self.hub.failed.emit(exc)
        else:
            self.hub.finished.emit()


class _SearchTask(QRunnable):
    """Run the network search off the Qt UI thread.

    The direct probe opens a link to the controller, which blocks for a few
    seconds while the controller answers.  Freezing the window for that long
    would make the application look hung, so the work moves to a worker thread
    and the result is queued back to the UI thread.
    """

    class _Hub(QObject):
        finished = Signal(object)
        failed = Signal(object)

    def __init__(self, action):
        super().__init__()
        self._action = action
        self.hub = self._Hub()

    def run(self):
        try:
            result = self._action()
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI thread below
            self.hub.failed.emit(exc)
        else:
            self.hub.finished.emit(result)


class _Page(QWidget):
    """Base class providing logging and error reporting for the hardware pages."""

    def __init__(self, session: HardwareSession, parent=None):
        super().__init__(parent)
        self.session = session

    def log(self, message: str) -> None:
        self.session.status_changed.emit(message)

    def report_error(self, exc: Exception) -> None:
        self.log(f'{type(exc).__name__}: {exc}')
        QMessageBox.warning(self, '操作失败', str(exc))

    def run_action(self, action) -> None:
        """Run a callable, turning any exception into a message box."""
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - surface every failure to the operator
            self.report_error(exc)


class CommunicationPage(_Page):
    """Connection settings: IP address, connect/disconnect and link diagnostics."""

    def __init__(self, session: HardwareSession, parent=None):
        super().__init__(session, parent)
        root = QVBoxLayout(self)
        root.setSpacing(12)

        link_box = QGroupBox('控制器连接')
        form = QFormLayout(link_box)
        self.ip_edit = QLineEdit(session.settings.connection.ip)
        self.ip_edit.setPlaceholderText('192.168.0.11')
        self.ip_edit.setEnabled(session.settings.mode == 'real')
        form.addRow('控制器 IP', self.ip_edit)
        self.auto_connect = QCheckBox('启动时自动连接')
        self.auto_connect.setChecked(session.settings.connection.connect_on_start)
        form.addRow('', self.auto_connect)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem('真实控制器 (Zmotion)', 'real')
        self.mode_combo.addItem('虚拟控制器 (无硬件演练)', 'virtual')
        self.mode_combo.setCurrentIndex(
            self.mode_combo.findData(session.settings.mode))
        self.mode_combo.setToolTip(
            '真实控制器：驱动 Zmotion 运动控制卡。\n'
            '虚拟控制器：内置模拟控制器，无需硬件即可演练全部操作流程，\n'
            '并自动生成位置、状态与标度自检数据。')
        form.addRow('控制器模式', self.mode_combo)
        self.model_combo = QComboBox()
        for count in sorted(TUNER_MODELS):
            self.model_combo.addItem(
                f'{count} 轴 tuner ({", ".join(tuner_axes(count))})', count)
        self.model_combo.setCurrentIndex(
            self.model_combo.findData(session.settings.axis_count))
        self.model_combo.setToolTip(
            '选择本机的 tuner 型号：3 轴为 X/Z1/Z2（2 个探针），'
            '4 轴为 X/Z1/Z2/Z3（3 个探针）。\n'
            '切换后所有页面只显示该型号的轴，控制器物理轴序号不变。')
        form.addRow('tuner 型号', self.model_combo)
        row = QHBoxLayout()
        self.connect_button = QPushButton('连接')
        self.connect_button.setProperty('accent', True)
        self.disconnect_button = QPushButton('断开')
        self.disconnect_button.setEnabled(False)
        self.search_button = QPushButton('广播搜索')
        row.addWidget(self.connect_button)
        row.addWidget(self.disconnect_button)
        row.addWidget(self.search_button)
        row.addStretch()
        form.addRow('', row)
        root.addWidget(link_box)

        info_box = QGroupBox('连接状态')
        info = QFormLayout(info_box)
        self.state_label = QLabel('未连接')
        self.firmware_label = QLabel('—')
        self.firmware_label.setFont(numeric_font(10, bold=True))
        self.address_label = QLabel('—')
        self.address_label.setFont(numeric_font(10))
        self.library_label = QLabel('—')
        self.library_label.setWordWrap(True)
        self.library_label.setFont(numeric_font(9))
        info.addRow('状态', self.state_label)
        info.addRow('固件版本', self.firmware_label)
        info.addRow('控制器地址', self.address_label)
        info.addRow('SDK 库', self.library_label)
        root.addWidget(info_box)

        settings_box = QGroupBox('设置文件')
        settings_layout = QVBoxLayout(settings_box)
        self.settings_path_label = QLabel(str(session.store.path))
        self.settings_path_label.setFont(numeric_font(9))
        self.settings_path_label.setWordWrap(True)
        settings_layout.addWidget(self.settings_path_label)
        buttons = QHBoxLayout()
        save_button = QPushButton('保存通讯设置')
        reveal_button = QPushButton('打开所在文件夹')
        buttons.addWidget(save_button)
        buttons.addWidget(reveal_button)
        buttons.addStretch()
        settings_layout.addLayout(buttons)
        root.addWidget(settings_box)

        root.addWidget(QLabel(
            '控制器使用以太网口与上位机通信，ZAux_OpenEth 通过 UDP 广播在本网段内定位设备，'
            '因此上位机 IP 必须与控制器处于同一网段。'))
        root.addStretch()

        self.connect_button.clicked.connect(self.on_connect)
        self.disconnect_button.clicked.connect(self.on_disconnect)
        self.search_button.clicked.connect(self.on_search)
        save_button.clicked.connect(self.on_save)
        reveal_button.clicked.connect(self.on_reveal)
        self.model_combo.currentIndexChanged.connect(self.on_model_changed)
        self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)

        session.connection_changed.connect(self.on_connection_changed)
        session.status_changed.connect(self.on_status)
        self.refresh_library_label()
        self.on_connection_changed(session.connected)

    # -- helpers ---------------------------------------------------------
    def refresh_library_label(self) -> None:
        from .zmotion import vendor_dir
        path = vendor_dir() / 'zauxdll.dll'
        self.library_label.setText(str(path) if path.is_file() else
                                   f'{path}（未安装）')

    def on_connection_changed(self, connected: bool) -> None:
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.state_label.setText('已连接' if connected else '未连接')
        if connected and self.session.link is not None:
            self.firmware_label.setText(self.session.link.firmware or '未知')
            self.address_label.setText(self.session.link.ip)

    def on_status(self, message: str) -> None:
        self.state_label.setText(message)

    # -- actions ---------------------------------------------------------
    def on_connect(self) -> None:
        mode = self.mode_combo.currentData()
        ip = self.ip_edit.text().strip()
        if mode == 'real' and not ip:
            QMessageBox.warning(self, '通讯设置', '请填写控制器 IP 地址')
            return
        self.connect_button.setEnabled(False)
        try:
            self.session.connect(ip, mode=mode)
            if mode == 'real':
                self.session.set_connection_settings(
                    ip, connect_on_start=self.auto_connect.isChecked())
        except Exception as exc:  # noqa: BLE001
            self.report_error(exc)
            self.on_connection_changed(self.session.connected)
        finally:
            self.connect_button.setEnabled(not self.session.connected)

    def on_disconnect(self) -> None:
        self.run_action(self.session.disconnect)

    def on_search(self) -> None:
        """Look for controllers on the network.

        Two mechanisms are needed because neither alone is enough on this bench.
        The subnet broadcast (``ZAux_SearchEthlist``) finds a controller whose
        address is unknown, but it is unreliable on a multi-NIC workstation and
        the controller rate-limits the replies.  A direct link (``ZAux_OpenEth``)
        answers reliably, but it needs the address and it cannot be opened while
        the session already holds one.  Both are tried and the operator is told
        which one answered.
        """
        ip = self.ip_edit.text().strip()
        already_connected = bool(self.session.connected)
        session_firmware = ''
        if already_connected:
            link = getattr(self.session, 'link', None)
            session_firmware = getattr(link, 'firmware', '') or ''

        def action():
            controller = ZmotionController()
            found = controller.search_list(timeout_ms=1500)
            if already_connected:
                # Opening a second link to a controller that is already linked
                # would disturb the live session, so the probe is skipped.
                probe = {'ip': ip, 'reachable': True, 'firmware': session_firmware,
                         'error': ''}
            elif ip:
                probe = probe_controller(ip, timeout=3.0)
            else:
                probe = {'ip': ip, 'reachable': False, 'firmware': '',
                         'error': '未填写 IP 地址'}
            return found, probe

        def done(result):
            found, probe = result
            lines = []
            if found:
                lines.append('广播搜索发现控制器：' + '、'.join(found))
            if probe.get('reachable'):
                firmware = probe.get('firmware') or '未知'
                lines.append(f'{probe.get("ip")} 可连接，固件 {firmware}')
            elif probe.get('error'):
                lines.append(f'{ip or "（未填 IP）"} 未能连接：{probe["error"]}')
            if not lines:
                lines.append('未发现控制器。请确认控制器已上电、网线连接正常，'
                             '且上位机与控制器处于同一网段。')
            QMessageBox.information(self, '搜索控制器', '\n'.join(lines))

        def failed(exc):
            self.report_error(exc)

        self.search_button.setEnabled(False)
        task = _SearchTask(action)
        # The page keeps the reference so the worker is not collected mid-flight.
        self._search_task = task
        task.hub.finished.connect(lambda result: (done(result),
                                                  self.search_button.setEnabled(True)))
        task.hub.failed.connect(lambda exc: (failed(exc),
                                             self.search_button.setEnabled(True)))
        QThreadPool.globalInstance().start(task)

    def on_save(self) -> None:
        def action():
            ip = self.ip_edit.text().strip()
            self.session.set_connection_settings(
                ip, connect_on_start=self.auto_connect.isChecked())
            self.log(f'通讯设置已保存到 {self.session.store.path}')
            QMessageBox.information(self, '保存成功', f'设置已保存到\n{self.session.store.path}')
        self.run_action(action)

    def on_mode_changed(self) -> None:
        """Apply the selected controller backend to the session."""
        mode = self.mode_combo.currentData()
        try:
            self.session.set_mode(str(mode))
        except Exception as exc:  # noqa: BLE001
            self.report_error(exc)
            return
        label = '真实控制器' if mode == 'real' else '虚拟控制器'
        self.ip_edit.setEnabled(mode == 'real')
        self.state_label.setText(f'模式: {label}')
        self.log(f'控制器模式已切换为 {label}')

    def on_model_changed(self) -> None:
        """Apply the selected tuner model to the session."""
        count = self.model_combo.currentData()
        try:
            self.session.set_axis_count(int(count))
        except Exception as exc:  # noqa: BLE001
            self.report_error(exc)
            return
        self.state_label.setText(
            f'tuner 型号: {count} 轴 ({", ".join(tuner_axes(int(count)))})')
        self.log(f'tuner 型号已切换为 {count} 轴')

    def on_reveal(self) -> None:
        path = Path(self.session.store.path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            import os
            os.startfile(str(path.parent))  # noqa: S606 - Windows shell open
        except Exception as exc:  # noqa: BLE001
            self.report_error(exc)


class AxisMotionPage(_Page):
    """Manual movement of the tuner axes: jog, absolute positioning, homing, stop.

    Each axis (X / Z1 / Z2 / Z3) gets its own graphical panel so the four axes are
    controlled independently.  A read-only summary table on top still shows every
    axis at once.  The rows and panels follow the active tuner model, so a 3-axis
    tuner shows X/Z1/Z2 and a 4-axis tuner shows X/Z1/Z2/Z3.
    """

    # Commanded-unit window mapped onto each axis' vertical position bar.  The
    # controller's FS/RS_LIMIT are still the uncommissioned defaults, so a fixed
    # window keeps the bar meaningful; the numeric DPOS is shown alongside it.
    _BAR_MIN = -10.0
    _BAR_MAX = 10.0

    def __init__(self, session: HardwareSession, parent=None):
        super().__init__(session, parent)
        root = QVBoxLayout(self)
        root.setSpacing(12)

        status_box = QGroupBox('实时状态')
        grid = QGridLayout(status_box)
        headers = ('轴', '指令位置 DPOS\n(控制器单位)', '反馈位置 MPOS\n(编码器单位)',
                   '跟随误差\n(控制器−编码器)', '空闲', '回零', 'AXISSTATUS\n(状态码)',
                   '状态说明', '停止原因')
        for column, title in enumerate(headers):
            label = QLabel(title)
            label.setStyleSheet('font-weight:600;')
            label.setWordWrap(True)
            grid.addWidget(label, 0, column)
        self.position_labels: dict[str, list[QLabel]] = {}
        self.status_flags: dict[str, QLabel] = {}
        self.status_grid = grid
        self.headers = headers
        for row, name in enumerate(session.axes, start=1):
            axis_label = QLabel(name)
            axis_label.setStyleSheet('font-weight:700;')
            grid.addWidget(axis_label, row, 0)
            cells = []
            for column in range(1, len(headers)):
                value = QLabel('—')
                value.setFont(numeric_font(9 if column >= 7 else 10))
                value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                grid.addWidget(value, row, column)
                cells.append(value)
            self.position_labels[name] = cells
            self.status_flags[name] = cells[6]
        root.addWidget(status_box)

        # --- per-axis graphical panels (each axis controlled independently) ---
        panel_box = QGroupBox('各轴独立控制（X / Z1 / Z2 / Z3 各自分开）')
        self.axis_panel_layout = QHBoxLayout(panel_box)
        self.axis_panels: dict[str, dict] = {}
        self._build_axis_panels()
        root.addWidget(panel_box)

        # --- global safety / helpers (shared across all axes) ---
        control_box = QGroupBox('全局操作')
        control_layout = QVBoxLayout(control_box)
        button_row = QHBoxLayout()
        self.stop_button = QPushButton('停止')
        self.errors_button = QPushButton('读取控制器错误码')
        self.errors_button.setToolTip(
            '读取控制器自身的错误号，并把 AXISSTATUS / AXIS_STOPREASON 的状态位翻译成文字。')
        button_row.addWidget(self.stop_button)
        button_row.addWidget(self.errors_button)
        button_row.addStretch()
        control_layout.addLayout(button_row)

        emergency_row = QHBoxLayout()
        self.estop_button = QPushButton('急停 (EMERGENCY STOP)')
        self.estop_button.setProperty('danger', True)
        self.estop_button.setMinimumHeight(44)
        self.reset_button = QPushButton('复位急停')
        self.reset_button.setMinimumHeight(44)
        emergency_row.addWidget(self.estop_button, 3)
        emergency_row.addWidget(self.reset_button, 1)
        control_layout.addLayout(emergency_row)
        root.addWidget(control_box)

        root.addWidget(QLabel(
            '各轴独立运动：X 轴不再受探针位置约束，软件不会自动回缩探针。'
            '移动 X 前请自行确认探针与 DUT 无碰撞风险；软件急停不能替代硬件安全联锁。'))

        self.scale_label = QLabel('')
        self.scale_label.setWordWrap(True)
        self.scale_label.setStyleSheet('color:#8a5a00;')
        root.addWidget(self.scale_label)

        self.apply_scale_button = QPushButton('采纳并写入修正后的 UNITS')
        self.apply_scale_button.setProperty('danger', True)
        self.apply_scale_button.setVisible(False)
        self.apply_scale_button.setToolTip(
            '把标度自检算出的 UNITS 写入控制器并保存到设置文件。\n'
            'UNITS 决定"一个指令单位等于多少毫米"，改写后坐标系随之改变，该轴必须重新回零。')
        self._pending_scale = None
        root.addWidget(self.apply_scale_button)

        self.error_label = QLabel('')
        self.error_label.setWordWrap(True)
        root.addWidget(self.error_label)
        root.addStretch()

        self.stop_button.clicked.connect(self.on_stop)
        self.estop_button.clicked.connect(self.on_emergency_stop)
        self.reset_button.clicked.connect(self.on_reset_emergency)
        self.errors_button.clicked.connect(self.on_read_errors)
        self.apply_scale_button.clicked.connect(self.on_apply_scale)

        # No motion task may run before the page is fully built.
        self._motion_busy = False

        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.refresh_positions)
        self.timer.start()

        # A model switch changes the axis list, so the panels and rows are rebuilt.
        session.settings_changed.connect(self.rebuild_for_model)

    # -- refresh ---------------------------------------------------------
    def _rebuild_summary_rows(self) -> None:
        """Recreate the read-only status rows for the active tuner model."""
        grid = self.status_grid
        # Remove the old rows (the header row stays).
        for item in self.position_labels.values():
            for label in item:
                grid.removeWidget(label)
                label.deleteLater()
        self.position_labels.clear()
        self.status_flags.clear()
        for row, name in enumerate(self.session.axes, start=1):
            axis_label = QLabel(name)
            axis_label.setStyleSheet('font-weight:700;')
            grid.addWidget(axis_label, row, 0)
            cells = []
            for column in range(1, len(self.headers)):
                value = QLabel('—')
                value.setFont(numeric_font(9 if column >= 7 else 10))
                value.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                grid.addWidget(value, row, column)
                cells.append(value)
            self.position_labels[name] = cells
            self.status_flags[name] = cells[6]

    def _build_axis_panels(self) -> None:
        """Create one graphical control panel per active axis."""
        for name in self.session.axes:
            panel = self._build_axis_panel(name)
            self.axis_panel_layout.addWidget(panel)

    def _build_axis_panel(self, axis: str) -> QWidget:
        """A self-contained panel: home light, vertical position bar, jog, absolute, home, probe."""
        # The title carries the controller axis number, because that is what
        # the vendor tool shows and what the wiring diagrams use.
        physical = self._physical_axis_hint(axis)
        panel = QGroupBox(axis if physical is None else f'{axis} · 轴 {physical}')
        vbox = QVBoxLayout(panel)
        vbox.setSpacing(6)

        home_row = QHBoxLayout()
        light = QLabel('●')
        light.setStyleSheet('color:#c0392b; font-size:15px;')
        home_text = QLabel('未回零')
        home_text.setStyleSheet('color:#c0392b;')
        home_row.addWidget(light)
        home_row.addWidget(home_text)
        home_row.addStretch()
        vbox.addLayout(home_row)

        bar = QProgressBar()
        bar.setOrientation(Qt.Orientation.Vertical)
        bar.setRange(0, 1000)
        bar.setValue(500)
        bar.setFixedHeight(130)
        bar.setTextVisible(False)
        bar.setToolTip('指令位置 DPOS（按固定窗口映射到填充高度）')
        vbox.addWidget(bar, alignment=Qt.AlignmentFlag.AlignHCenter)
        dpos_label = QLabel('指令 0.0000')
        dpos_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dpos_label.setFont(numeric_font(10))
        vbox.addWidget(dpos_label)

        step = QDoubleSpinBox()
        step.setRange(0.0001, 100.)
        step.setDecimals(4)
        step.setValue(0.1)
        step.setSuffix(' mm')
        step.setToolTip('点动步长（指令单位）')
        vbox.addWidget(step)

        jog_row = QHBoxLayout()
        jog_neg = QPushButton('点动 −')
        jog_pos = QPushButton('点动 +')
        jog_row.addWidget(jog_neg)
        jog_row.addWidget(jog_pos)
        vbox.addLayout(jog_row)

        target = QDoubleSpinBox()
        target.setRange(-1e8, 1e8)
        target.setDecimals(4)
        target.setValue(0.)
        target.setSuffix(' mm')
        target.setToolTip('绝对目标位置（指令单位）')
        vbox.addWidget(target)
        abs_btn = QPushButton('定位')
        vbox.addWidget(abs_btn)

        home_btn = QPushButton('回零')
        vbox.addWidget(home_btn)
        probe_btn = QPushButton('标度自检')
        probe_btn.setToolTip(
            '让该轴走一个极小步长再回到原位，测量「编码器位移 ÷ 指令位移」。'
            '比值不为 1 说明 UNITS 与反馈标度不匹配，轴的实际行程远大于指令数字。')
        vbox.addWidget(probe_btn)

        self.axis_panels[axis] = {
            'panel': panel, 'light': light, 'home_text': home_text,
            'bar': bar, 'dpos_label': dpos_label, 'step': step,
            'jog_neg': jog_neg, 'jog_pos': jog_pos, 'target': target,
            'abs_btn': abs_btn, 'home_btn': home_btn, 'probe_btn': probe_btn,
        }
        # Capture ``axis`` by value so each panel drives its own axis.
        jog_neg.clicked.connect(lambda _=False, a=axis: self.on_jog(a, -1))
        jog_pos.clicked.connect(lambda _=False, a=axis: self.on_jog(a, +1))
        abs_btn.clicked.connect(lambda _=False, a=axis: self.on_absolute(a))
        home_btn.clicked.connect(lambda _=False, a=axis: self.on_home(a))
        probe_btn.clicked.connect(lambda _=False, a=axis: self.on_probe_scale(a))
        return panel

    def rebuild_for_model(self) -> None:
        """Recreate both the summary rows and the per-axis panels for the model."""
        self._rebuild_summary_rows()
        for name in list(self.axis_panels):
            entry = self.axis_panels.pop(name)
            entry['panel'].deleteLater()
        self._build_axis_panels()

    def _physical_axis_hint(self, axis: str) -> int | None:
        """Controller axis number of ``axis``, when it is known.

        The number comes from the active tuner's wiring, so it is right even
        while the controller is disconnected -- which is exactly when the
        operator needs to check it against the cable diagram.
        """
        try:
            return self.session.active_unit.physical_axis(axis)
        except Exception:  # noqa: BLE001 - a hint must never break the panel
            return None

    def refresh_positions(self) -> None:
        """Poll the controller for positions; silently skips when not connected."""
        controller = self.session.controller
        if controller is None or not self.session.connected:
            return
        try:
            snapshot = controller.axis_status_snapshot()
        except Exception:  # noqa: BLE001 - polling must never raise into the event loop
            return
        for entry in snapshot:
            name = entry['axis']
            cells = self.position_labels.get(name)
            if cells:
                dpos, mpos = entry['dpos'], entry['mpos']
                cells[0].setText(f'{dpos:.4f}')
                cells[1].setText(f'{mpos:.4f}')
                cells[2].setText(f'{dpos - mpos:.4f}')
                cells[3].setText('是' if entry['idle'] else '否')
                cells[4].setText('已回零' if entry['home_status'] == 1 else '未回零')
                cells[5].setText(str(int(entry['status'])))
                status_text = entry.get('status_text', '正常')
                cells[6].setText(status_text)
                cells[7].setText(entry.get('stop_reason_text', '—'))
                fault = bool(entry.get('status_fault'))
                colour = '#c0392b' if fault else ('#8a5a00' if status_text != '正常' else '#1f2937')
                cells[5].setStyleSheet(f'color:{colour}; font-weight:{"700" if fault else "400"};')
                cells[6].setStyleSheet(f'color:{colour};')
            # Per-axis graphical panel (independent of the summary table).
            panel = self.axis_panels.get(name)
            if panel:
                dpos = entry['dpos']
                frac = (dpos - self._BAR_MIN) / (self._BAR_MAX - self._BAR_MIN)
                frac = max(0.0, min(1.0, frac))
                panel['bar'].setValue(int(frac * 1000))
                panel['dpos_label'].setText(f'指令 {dpos:.4f}')
                homed = entry['home_status'] == 1
                hcolour = '#1f7a4d' if homed else '#c0392b'
                panel['light'].setStyleSheet(f'color:{hcolour}; font-size:15px;')
                panel['home_text'].setStyleSheet(f'color:{hcolour};')
                panel['home_text'].setText('已回零' if homed else '未回零')

    # -- actions ---------------------------------------------------------
    def on_jog(self, axis: str, sign: int) -> None:
        step = self.axis_panels[axis]['step'].value()
        self._run_motion(lambda: self._jog(axis, sign * step))

    def _jog(self, axis: str, delta: float) -> None:
        controller = self.session.require_controller()
        try:
            controller.move_axis_to(axis, controller.link.get_dpos(
                controller.physical_axis(axis)) + delta)
        except Exception as exc:  # noqa: BLE001
            operation_log.exception('运动', '点动', exc, axis=axis, delta=delta)
            raise
        operation_log.info('运动', '点动', f'{axis} {delta:+g}', axis=axis, delta=delta)
        self.refresh_positions()

    def on_absolute(self, axis: str) -> None:
        target = self.axis_panels[axis]['target'].value()
        self._run_motion(lambda: self._absolute(axis, target))

    def _absolute(self, axis: str, target: float) -> None:
        controller = self.session.require_controller()
        try:
            controller.move_axis_to(axis, target)
        except Exception as exc:  # noqa: BLE001
            operation_log.exception('运动', '绝对定位', exc, axis=axis, target=target)
            raise
        operation_log.info('运动', '绝对定位', f'{axis} → {target}', axis=axis, target=target)
        self.refresh_positions()

    def on_home(self, axis: str) -> None:
        self._run_motion(lambda: self._home(axis))

    def _home(self, axis: str) -> None:
        controller = self.session.require_controller()
        try:
            # Route through the guarded entry point: it refuses to home while the
            # emergency stop or a fault is latched, rejects axes that are not part
            # of the active tuner model, enforces "probes before X", and refreshes
            # the internal ``homed_axes`` so the displayed state stays truthful
            # (P0-03).
            controller.home_axis_safe(axis, controller.config.motion_timeout)
            homed = axis in controller.homed_axes
        except Exception as exc:  # noqa: BLE001
            operation_log.exception('回零', '单轴回零', exc, axis=axis)
            raise
        operation_log.log('回零', '单轴回零', f'{axis} 回零{"完成" if homed else "未完成"}',
                          'info' if homed else 'warn', axis=axis,
                          home_status=int(homed))
        self.log(f'{axis} 回零结束，状态码 {int(homed)}')
        self.refresh_positions()

    def _run_motion(self, action) -> None:
        """Submit a blocking motion to a worker thread (P0-02).

        The call returns immediately so the Qt event loop stays free to process
        the *Stop* / *Emergency stop* buttons.  Overlapping motion requests are
        ignored while one is in flight; *Stop* and *Emergency stop* deliberately
        bypass this path and run synchronously on the UI thread so they always
        preempt the running move.
        """
        if self._motion_busy:
            self.log('已有运动任务在执行，忽略本次请求')
            return
        self._motion_busy = True
        self._set_motion_controls_enabled(False)
        task = _MotionTask(action)
        task.hub.finished.connect(self._on_motion_done)
        task.hub.failed.connect(self._on_motion_error)
        QThreadPool.globalInstance().start(task)

    def _on_motion_done(self) -> None:
        self._motion_busy = False
        self._set_motion_controls_enabled(True)
        self.refresh_positions()

    def _on_motion_error(self, exc: Exception) -> None:
        self._motion_busy = False
        self._set_motion_controls_enabled(True)
        self.report_error(exc)

    def _set_motion_controls_enabled(self, enabled: bool) -> None:
        """Toggle every per-axis motion control; *Stop* and *Emergency stop* stay live."""
        for entry in self.axis_panels.values():
            for widget in (entry['jog_neg'], entry['jog_pos'], entry['abs_btn'],
                          entry['home_btn'], entry['probe_btn'], entry['step'],
                          entry['target']):
                widget.setEnabled(enabled)

    def on_stop(self) -> None:
        # Stop is issued synchronously on the UI thread so it never waits for a
        # blocking move to finish -- it must always be able to preempt (P0-02).
        self.run_action(self._stop)

    def _stop(self) -> None:
        controller = self.session.require_controller()
        controller.stop_all()
        operation_log.warn('运动', '停止', '已向全部轴发送停止指令')
        self.log('已发送停止指令')
        self.refresh_positions()

    def on_probe_scale(self, axis: str) -> None:
        self._run_motion(lambda: self._probe_scale(axis))

    def _probe_scale(self, axis: str) -> None:
        """Measure the commanded-to-feedback ratio of the given axis."""
        controller = self.session.require_controller()
        try:
            result = controller.probe_axis_scale(axis, distance=0.005)
        except Exception as exc:  # noqa: BLE001
            operation_log.exception('标度', '标度自检', exc, axis=axis)
            raise
        ratio = result['ratio']
        self.refresh_positions()
        if abs(ratio - 1.0) <= 0.05:
            message = (f'{axis} 标度自检：指令 {result["commanded"]:.4f} → '
                       f'编码器 {result["feedback"]:+.4f}，比值 {ratio:.3f}（一致）')
            self.scale_label.setStyleSheet('color:#1f7a4d;')
            self.apply_scale_button.setVisible(False)
            self._pending_scale = None
            operation_log.info('标度', '标度自检',
                               f'{axis} 指令 {result["commanded"]:g} → 编码器 '
                               f'{result["feedback"]:+.6f}，比值 {ratio:.3f}',
                               axis=axis, ratio=ratio)
        else:
            suggested = float(result.get('suggested_units', result.get('units', 0.0)))
            units = float(result.get('units', 0.0))
            message = (f'{axis} 标度自检：指令 {result["commanded"]:.4f} → '
                       f'编码器 {result["feedback"]:+.4f}，比值 {ratio:.2f}。'
                       f'该轴实际行程约为指令数字的 {ratio:.1f} 倍，'
                       f'说明 UNITS 与反馈标度不匹配。'
                       f'当前 UNITS={units:g}，若要"一个指令单位 = 1 mm"，'
                       f'UNITS 应改为约 {suggested:g}（= 当前 UNITS ÷ {ratio:.1f}）。')
            self.scale_label.setStyleSheet('color:#c0392b; font-weight:600;')
            self._pending_scale = (axis, suggested)
            self.apply_scale_button.setText(
                f'采纳并写入修正后的 UNITS（{axis} → {suggested:g}）')
            self.apply_scale_button.setVisible(True)
            operation_log.warn('标度', '标度自检',
                               f'{axis} 比值 {ratio:.2f}，UNITS 与反馈标度不匹配，'
                               f'建议 UNITS {units:g} → {suggested:g}',
                               axis=axis, ratio=ratio, suggested_units=suggested)
        self.scale_label.setText(message)
        self.log(message)

    def on_apply_scale(self) -> None:
        self.run_action(self._apply_scale)

    def _apply_scale(self) -> None:
        """Write the corrected UNITS to the controller and store it."""
        if not self._pending_scale:
            raise MotionError('请先做一次标度自检')
        axis, units = self._pending_scale
        result = self.session.apply_scale_correction(axis, units)
        self.apply_scale_button.setVisible(False)
        self._pending_scale = None
        message = (f'{axis} 的 UNITS 已改为 {result["units"]:g} 并保存到设置文件。'
                   f'坐标系随之改变，请重新回零后再运动。')
        self.scale_label.setStyleSheet('color:#1f7a4d;')
        self.scale_label.setText(message)
        self.log(message)

    def on_read_errors(self) -> None:
        self.run_action(self._read_errors)

    def _read_errors(self) -> None:
        """Read the controller's error number and show the decoded axis faults."""
        result = self.session.controller_errors()
        faults = [entry for entry in result['axes'] if entry['status_fault']]
        text = f"控制器错误码：{result['text']}"
        if faults:
            text += '　报警轴：' + '、'.join(
                f"{entry['axis']}（{entry['status_text']}）" for entry in faults)
            self.error_label.setStyleSheet('color:#c0392b; font-weight:600;')
        else:
            text += '　各轴无报警'
            self.error_label.setStyleSheet('color:#1f7a4d;')
        self.error_label.setText(text)
        self.log(text)
        self.refresh_positions()

    def on_emergency_stop(self) -> None:
        self.run_action(self._emergency_stop)

    def _emergency_stop(self) -> None:
        controller = self.session.require_controller()
        controller.emergency_stop()
        operation_log.error('急停', '触发急停', '软件急停已锁存，需复位后重新回零')
        self.log('急停已触发，软件状态已锁定，需复位后重新回零')
        self.refresh_positions()

    def on_reset_emergency(self) -> None:
        self.run_action(self._reset_emergency)

    def _reset_emergency(self) -> None:
        controller = self.session.require_controller()
        controller.reset_emergency()
        operation_log.warn('急停', '复位急停', '急停已复位，请重新回零后再运动')
        self.log('急停已复位，请重新回零后再运动')
        self.refresh_positions()


class AxisParameterPage(_Page):
    """Per-axis parameter table: read from the controller, edit, write and save."""

    def __init__(self, session: HardwareSession, parent=None):
        super().__init__(session, parent)
        root = QVBoxLayout(self)
        root.setSpacing(12)

        toolbar = QHBoxLayout()
        self.read_button = QPushButton('从控制器读取')
        self.write_button = QPushButton('写入控制器')
        self.save_button = QPushButton('保存到文件')
        self.load_button = QPushButton('从文件加载')
        self.export_button = QPushButton('导出…')
        self.import_button = QPushButton('导入…')
        for widget in (self.read_button, self.write_button, self.save_button,
                       self.load_button, self.export_button, self.import_button):
            toolbar.addWidget(widget)
        toolbar.addStretch()
        root.addLayout(toolbar)

        table_box = QGroupBox('轴参数')
        table_layout = QVBoxLayout(table_box)
        self.table = QTableWidget(len(session.axes), len(_PARAM_COLUMNS) + 1)
        self.table.setHorizontalHeaderLabels(
            ['轴'] + [title for _, title, _ in _PARAM_COLUMNS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, header.ResizeMode.ResizeToContents)
        for column in range(1, len(_PARAM_COLUMNS) + 1):
            header.setSectionResizeMode(column, header.ResizeMode.Stretch)
        table_layout.addWidget(self.table)
        root.addWidget(table_box)

        self.hint_label = QLabel('')
        self.hint_label.setWordWrap(True)
        root.addWidget(self.hint_label)

        root.addWidget(QLabel(
            '参数修改后需点击「写入控制器」才会下发到设备；'
            '「保存到文件」只更新本机配置，供下次启动时自动载入。'))
        root.addStretch()

        self.read_button.clicked.connect(self.on_read)
        self.write_button.clicked.connect(self.on_write)
        self.save_button.clicked.connect(self.on_save)
        self.load_button.clicked.connect(self.on_load)
        self.export_button.clicked.connect(self.on_export)
        self.import_button.clicked.connect(self.on_import)

        # A model switch changes the axis list, so the rows have to be rebuilt.
        session.settings_changed.connect(self.rebuild_rows)
        self.fill_table(session.settings.axes)

    # -- table -----------------------------------------------------------
    def rebuild_rows(self) -> None:
        """Resize the table to the active tuner model and refill it."""
        self.table.setRowCount(len(self.session.axes))
        self.fill_table(self.session.settings.axes)

    def fill_table(self, axis_params: dict[str, AxisParams]) -> None:
        """Populate the table from a parameter mapping."""
        for row, name in enumerate(self.session.axes):
            params = axis_params[name]
            axis_item = QTableWidgetItem(name)
            axis_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            axis_item.setFont(text_font(10, bold=True))
            self.table.setItem(row, 0, axis_item)
            for column, (key, _, kind) in enumerate(_PARAM_COLUMNS, start=1):
                value = getattr(params, key)
                if kind == 'int':
                    text = str(int(value))
                else:
                    text = f'{value:g}'
                item = QTableWidgetItem(text)
                item.setFont(numeric_font(10))
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, column, item)

    def table_to_params(self) -> dict[str, AxisParams]:
        """Read the table back into validated parameter objects."""
        result: dict[str, AxisParams] = {}
        for row, name in enumerate(self.session.axes):
            values: dict[str, float | int | str] = {'name': name}
            for column, (key, _, kind) in enumerate(_PARAM_COLUMNS, start=1):
                text = self.table.item(row, column).text().strip()
                if not text:
                    raise ValueError(f'{name} 轴的「{key}」为空')
                try:
                    values[key] = int(text) if kind == 'int' else float(text)
                except ValueError as exc:
                    raise ValueError(f'{name} 轴的「{key}」不是有效数字: {text!r}') from exc
            result[name] = AxisParams(**values)
        return result

    # -- actions ---------------------------------------------------------
    def on_read(self) -> None:
        self.run_action(self._read)

    def _read(self) -> None:
        live = self.session.read_parameters_from_controller()
        params = dict(self.session.settings.axes)
        for name in self.session.axes:
            current = params[name]
            updated = dict(current.to_dict())
            for key, _, kind in _PARAM_COLUMNS:
                if key in live[name]:
                    updated[key] = int(live[name][key]) if kind == 'int' else live[name][key]
            params[name] = AxisParams(**updated)
        self.fill_table(params)
        self.hint_label.setText('已从控制器读取当前参数（尚未保存到文件）')
        self.log('已从控制器读取轴参数')

    def on_write(self) -> None:
        self.run_action(self._write)

    def _write(self) -> None:
        params = self.table_to_params()
        self.session.set_axis_parameters(params)
        self.session.apply_parameters_to_controller()
        self.hint_label.setText('参数已写入控制器并保存到文件')
        self.log('轴参数已写入控制器')

    def on_save(self) -> None:
        self.run_action(self._save)

    def _save(self) -> None:
        params = self.table_to_params()
        self.session.set_axis_parameters(params)
        path = self.session.store.path
        self.hint_label.setText(f'已保存到 {path}')
        self.log(f'轴参数已保存到 {path}')

    def on_load(self) -> None:
        self.run_action(self._load)

    def _load(self) -> None:
        settings = self.session.store.load()
        self.fill_table(settings.axes)
        self.hint_label.setText(f'已从 {self.session.store.path} 载入（尚未写入控制器）')
        self.log('已从文件加载轴参数')

    def on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, '导出轴参数', 'tuner_axis_parameters.json', 'JSON (*.json)')
        if not path:
            return
        params = self.table_to_params()
        self.session.store.export(path, TunerSettings(
            connection=self.session.settings.connection, axes=params))
        self.log(f'轴参数已导出到 {path}')

    def on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, '导入轴参数', '', 'JSON (*.json)')
        if not path:
            return
        settings = SettingsStore.import_from(path)
        self.fill_table(settings.axes)
        self.hint_label.setText(f'已从 {path} 导入（尚未写入控制器）')
        self.log(f'已从 {path} 导入轴参数')


class OperationLogPage(_Page):
    """Audit trail of every operator action in this session."""

    def __init__(self, session: HardwareSession, parent=None):
        super().__init__(session, parent)
        root = QVBoxLayout(self)
        root.setSpacing(12)

        toolbar = QHBoxLayout()
        self.category_combo = QComboBox()
        self.category_combo.addItem('全部', '')
        for name in ('通讯', '运动', '回零', '参数', '急停', '标度', '矢网', '校准', '系统'):
            self.category_combo.addItem(name, name)
        toolbar.addWidget(QLabel('分类'))
        toolbar.addWidget(self.category_combo)
        refresh_button = QPushButton('刷新')
        export_button = QPushButton('导出 JSON…')
        clear_button = QPushButton('清空')
        for widget in (refresh_button, export_button, clear_button):
            toolbar.addWidget(widget)
        toolbar.addStretch()
        root.addLayout(toolbar)

        table_box = QGroupBox('操作记录')
        table_layout = QVBoxLayout(table_box)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['时间', '分类', '动作', '详情', '级别'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, header.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, header.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, header.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, header.ResizeMode.Stretch)
        header.setSectionResizeMode(4, header.ResizeMode.ResizeToContents)
        table_layout.addWidget(self.table)
        root.addWidget(table_box)

        self.hint_label = QLabel('')
        self.hint_label.setWordWrap(True)
        root.addWidget(self.hint_label)
        note = QLabel(
            '日志记录运动、回零、标度自检、急停、参数写入、仪器连接与校准运行等全部操作，'
            '是排查"到底下发了什么指令、控制器回了什么"的依据。内存保留最近 2000 条。')
        # The sentence is wider than a narrow window; without wrapping it is
        # cut off instead of shown on two lines.
        note.setWordWrap(True)
        root.addWidget(note)
        root.addStretch()

        refresh_button.clicked.connect(self.refresh)
        clear_button.clicked.connect(self.on_clear)
        export_button.clicked.connect(self.on_export)
        self.category_combo.currentIndexChanged.connect(lambda _: self.refresh())

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    # -- helpers ---------------------------------------------------------
    def refresh(self) -> None:
        """Reload the log entries into the table."""
        category = self.category_combo.currentData() or None
        entries = operation_log.recent(500, category)
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            for column, value in enumerate((entry['time_text'], entry['category'],
                                            entry['action'], entry['detail'],
                                            entry['level'])):
                item = QTableWidgetItem(str(value))
                if column in (0, 4):
                    item.setFont(numeric_font(9))
                else:
                    item.setFont(text_font(9))
                if entry['level'] == 'error':
                    item.setForeground(Qt.GlobalColor.red)
                elif entry['level'] == 'warn':
                    item.setForeground(Qt.GlobalColor.darkYellow)
                self.table.setItem(row, column, item)
        self.hint_label.setText(f'共 {len(entries)} 条记录（内存上限 2000 条）')

    # -- actions ---------------------------------------------------------
    def on_clear(self) -> None:
        removed = operation_log.clear()
        self.refresh()
        self.log(f'已清空操作日志（{removed} 条）')

    def on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, '导出操作日志', 'tunerlab_operations.json', 'JSON (*.json)')
        if not path:
            return
        operation_log.export(path)
        self.log(f'操作日志已导出到 {path}')

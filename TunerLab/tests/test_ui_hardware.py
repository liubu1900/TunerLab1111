"""Offscreen tests for the three hardware pages.

The pages are driven through a stub session so no controller is required. The stub
mimics :class:`tuner.ui_hardware.HardwareSession` closely enough for the pages to be
exercised end to end.
"""
from __future__ import annotations

import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import time

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QMessageBox

from tuner.axis_params import TUNER_AXES, default_axis_params
from tuner.settings import SettingsStore, TunerSettings
from tuner.ui_hardware import (
    AxisMotionPage, AxisParameterPage, CommunicationPage, HardwareSession,
)


class StubLink:
    """Minimal stand-in for the controller link."""

    def __init__(self):
        self.connected = False
        self.firmware = '2.9600'
        self.ip = '192.168.0.11'
        self.positions = {0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0}
        self.params = {
            name: {'atype': 4, 'units': 2500.0 if name == 'X' else 10000.0,
                   'speed': 10.0, 'accel': 10.0, 'decel': 10.0, 'creep': 100.0,
                   'jog_speed': 100.0, 'invert_step': 0,
                   'datum_in': 16 + index, 'fs_limit': 200000000.0,
                   'rs_limit': -200000000.0}
            for index, name in enumerate(TUNER_AXES)}
        self.moved: list[tuple[int, float]] = []
        self.stopped = False

    def open(self, ip):
        self.connected = True
        self.ip = ip

    def close(self):
        self.connected = False

    # -- axis access -----------------------------------------------------
    def get_dpos(self, axis):
        return self.positions.get(axis, 0.0)

    def get_mpos(self, axis):
        return self.positions.get(axis, 0.0)

    def get_units(self, axis):
        return self.params[TUNER_AXES[axis]]['units']

    def get_atype(self, axis):
        return self.params[TUNER_AXES[axis]]['atype']

    def home_status(self, axis):
        return 1

    def if_idle(self, axis):
        return True

    def axis_status(self, axis):
        return 0

    def axis_stop_reason(self, axis):
        return 0

    def move_absolute(self, axis, value):
        self.moved.append((axis, float(value)))
        self.positions[axis] = float(value)

    def stop_axis(self, axis):
        pass

    def stop_axes(self, axes):
        pass

    def emergency_stop(self):
        self.stopped = True

    def home_axis(self, axis, mode):
        pass

    def wait_idle(self, axes, timeout):
        return True

    def read_all_axis_info(self, count):
        import numpy as np
        return {
            'idle': np.ones(count, dtype=int),
            'dpos': np.array([self.positions.get(i, 0.0) for i in range(count)]),
            'mpos': np.array([self.positions.get(i, 0.0) for i in range(count)]),
            'status': np.zeros(count, dtype=int),
        }


class StubController:
    """Minimal stand-in for the motion backend."""

    def __init__(self, link: StubLink):
        self.link = link
        self.axis_params = default_axis_params()
        self.stopped = False
        self.rapidstop_called = False

    def initialize(self):
        return self.read_position()

    def read_position(self):
        import numpy as np
        return np.array([self.link.get_dpos(i) for i in range(4)], dtype=float)

    def move_axis_to(self, axis, position):
        index = TUNER_AXES.index(axis)
        self.link.move_absolute(index, position)
        return position

    def stop_all(self):
        self.link.stop_axes(range(4))

    def emergency_stop(self):
        self.link.emergency_stop()
        self.stopped = True
        self.rapidstop_called = True

    def reset_emergency(self):
        self.stopped = False

    def axis_status_snapshot(self):
        return [{'axis': name, 'idle': -1, 'dpos': self.link.get_dpos(index),
                 'mpos': self.link.get_mpos(index), 'status': 0, 'home_status': 1}
                for index, name in enumerate(TUNER_AXES)]

    def apply_axis_parameters(self, params):
        self.axis_params = dict(params)

    def read_axis_parameters(self):
        return {name: dict(values) for name, values in self.link.params.items()}


class StubSession(HardwareSession):
    """Session backed by the stub link, writing settings to a temporary file."""

    def __init__(self, store):
        super().__init__(store)
        self.link = StubLink()
        self.controller = StubController(self.link)

    @property
    def connected(self):
        return self.link.connected

    def connect(self, ip=None, **kwargs):
        target = ip or self.settings.connection.ip
        self.link.open(target)
        self.settings.connection.ip = target
        self.connection_changed.emit(True)
        self.status_changed.emit(f'已连接 {target}')

    def disconnect(self):
        self.link.close()
        self.controller = None
        self.connection_changed.emit(False)
        self.status_changed.emit('连接已断开')

    def require_controller(self):
        if not self.connected:
            raise RuntimeError('尚未连接控制器')
        return self.controller


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch):
    """Keep modal message boxes from blocking the test run.

    The pages report success and failure with ``QMessageBox``; in an offscreen run
    those dialogs would wait forever for a click that never comes.
    """
    for name in ('warning', 'information', 'critical', 'question'):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *args, **kwargs: None))


@pytest.fixture
def session(tmp_path):
    store = SettingsStore(tmp_path / 'settings.json')
    return StubSession(store)


def test_communication_page_renders_and_toggles(session, app):
    page = CommunicationPage(session)
    assert page.connect_button.isEnabled()
    assert not page.disconnect_button.isEnabled()
    session.connect('192.168.0.11')
    app.processEvents()
    assert page.disconnect_button.isEnabled()
    assert page.firmware_label.text() == '2.9600'
    page.disconnect_button.click()
    app.processEvents()
    assert page.connect_button.isEnabled()


def test_communication_page_saves_ip(session, app):
    page = CommunicationPage(session)
    page.ip_edit.setText('192.168.0.42')
    page.on_save()
    assert session.settings.connection.ip == '192.168.0.42'
    assert session.store.path.is_file()


def test_motion_page_shows_all_axes(session, app):
    page = AxisMotionPage(session)
    session.connect()
    page.refresh_positions()
    app.processEvents()
    for name in TUNER_AXES:
        assert len(page.position_labels[name]) == 8
        assert page.position_labels[name][0].text() == '0.0000'


def test_motion_page_jog_and_absolute(session, app):
    page = AxisMotionPage(session)
    session.connect()
    page.axis_panels['Z1']['step'].setValue(0.5)
    page.on_jog('Z1', +1)
    # Motion now runs on a worker thread; wait for it before asserting.
    QThreadPool.globalInstance().waitForDone(2000)
    app.processEvents()
    assert session.link.moved[-1] == (1, 0.5)
    page.axis_panels['Z1']['target'].setValue(2.0)
    page.on_absolute('Z1')
    QThreadPool.globalInstance().waitForDone(2000)
    app.processEvents()
    assert session.link.moved[-1] == (1, 2.0)


def test_motion_stop_is_preemptive(session, app):
    """P0-02: a blocking move must not delay the Stop button.

    A real move blocks while the controller reports IDLE.  Here the stub link
    sleeps during the move; pressing Stop must dispatch immediately (well under
    the move duration) instead of queuing behind it.
    """
    class BlockingLink(StubLink):
        def move_absolute(self, axis, value):
            time.sleep(0.3)  # simulate a blocking move waiting on IDLE
            self.positions[axis] = float(value)
            self.moved.append((axis, float(value)))

        def stop_axes(self, axes):
            self.stop_axes_called = True

    session.link = BlockingLink()
    session.controller = StubController(session.link)
    page = AxisMotionPage(session)
    session.connect()
    page.axis_panels['Z1']['step'].setValue(0.5)

    page.on_jog('Z1', +1)            # submits a 0.3 s blocking move to a worker
    started = time.monotonic()
    page.on_stop()                   # must run on the UI thread and return now
    elapsed = time.monotonic() - started
    assert elapsed < 0.2, f'Stop was blocked by the move ({elapsed:.3f}s)'
    assert session.link.stop_axes_called

    QThreadPool.globalInstance().waitForDone(2000)
    app.processEvents()
    # The move still completed in the background.
    assert session.link.moved[-1] == (1, 0.5)


def test_motion_page_emergency_stop_latches(session, app):
    page = AxisMotionPage(session)
    session.connect()
    page.on_emergency_stop()
    assert session.controller.stopped
    page.on_reset_emergency()
    assert not session.controller.stopped


def test_parameter_page_reads_from_controller(session, app):
    page = AxisParameterPage(session)
    page.on_read()
    table = page.table
    assert table.rowCount() == 4
    # X uses a pulse equivalent of 2500, the probe axes 10000.
    assert table.item(0, 1).text() == '4'
    assert table.item(0, 2).text() == '2500'
    assert table.item(1, 2).text() == '10000'


def test_parameter_page_writes_to_controller_and_file(session, app):
    page = AxisParameterPage(session)
    session.connect()
    page.table.item(0, 3).setText('25')      # X speed
    page.on_write()
    assert session.controller.axis_params['X'].speed == 25.0
    assert session.store.path.is_file()
    assert session.store.load().axes['X'].speed == 25.0


def test_parameter_page_rejects_non_numeric(session, app):
    page = AxisParameterPage(session)
    page.table.item(0, 3).setText('fast')
    with pytest.raises(ValueError):
        page.table_to_params()


def test_parameter_page_round_trips_all_columns(session, app):
    page = AxisParameterPage(session)
    session.connect()
    params = page.table_to_params()
    assert set(params) == set(TUNER_AXES)
    assert params['Z3'].datum_in == 19
    assert params['X'].units == 2500.0

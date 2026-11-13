"""0.5.13.3 desktop regressions for the read-only VNA control page.

Covers the click-to-unlock sweep fields, the new read-back button, and the
three actions that must never write scan parameters (connect / single scan /
read-back) versus the one action that may (下发扫描参数).
"""
from __future__ import annotations

import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tunerlab_app import pages_vna
from tunerlab_app.pages_vna import (
    APPLY_LABEL, CONNECT_LABEL, READ_CONFIG_LABEL, VnaPage,
)


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def _state():
    cfg = {
        'host': '192.168.2.111', 'port': 5025, 'timeout_ms': 10000,
        'vendor_key': 'auto', 'interface': 'tcpip',
        'resource': 'GPIB0::17::INSTR',
        'start_hz': 1e9, 'stop_hz': 20e9, 'points': 401,
        'power_dbm': -10.0, 'ifbw_hz': 1000.0,
    }
    state = SimpleNamespace(
        vna_lock=threading.RLock(),
        vna={'controller': None, 'connected': False, 'idn': None,
             'vendor': None, 'last_error': None, 'config': cfg},
    )
    state.vna_status = lambda: {
        'connected': state.vna['connected'], 'idn': state.vna['idn'],
        'vendor': state.vna['vendor'], 'last_error': state.vna['last_error'],
        'config': state.vna['config'], 'live_config': None,
    }
    return state


def _close(page):
    page.timer.stop()
    page.close()


@contextmanager
def _synchronous_background():
    """Deliver background results immediately on the calling thread."""
    def run(function, on_done=None, on_error=None, description=''):
        try:
            result = function()
        except BaseException as exc:  # noqa: BLE001
            if on_error is not None:
                on_error(exc)
        else:
            if on_done is not None:
                on_done(result)

    with mock.patch.object(pages_vna, 'run_in_background', side_effect=run):
        yield


def _click(widget):
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton)


def _focus_out(widget):
    QApplication.sendEvent(widget, QFocusEvent(QEvent.Type.FocusOut))


def _spins(page):
    return (page.start_spin, page.stop_spin, page.points_spin,
            page.step_spin, page.power_spin, page.ifbw_spin)


def test_sweep_fields_are_locked_until_clicked_and_relock_on_focus_out(app):
    page = VnaPage(_state())
    try:
        assert len(page._param_locks) == 6
        assert all(spin.isReadOnly() for spin in _spins(page))
        assert all(guard.locked for guard in page._param_locks)

        line = page.start_spin.lineEdit()
        _click(line)
        assert not page.start_spin.isReadOnly()
        assert all(spin.isReadOnly() for spin in _spins(page)[1:])

        _focus_out(line)
        assert page.start_spin.isReadOnly()
        assert all(guard.locked for guard in page._param_locks)
    finally:
        _close(page)


def test_programmatic_fill_keeps_fields_visible_and_locked(app):
    page = VnaPage(_state())
    try:
        page.start_spin.setValue(7.5)
        assert page.start_spin.value() == pytest.approx(7.5)
        assert page.start_spin.isReadOnly()
        # A programmatic change must not raise the "parameters edited" warning.
        assert not page.stale_label.isVisible()
    finally:
        _close(page)


def test_new_buttons_exist_and_follow_connection_state(app):
    page = VnaPage(_state())
    try:
        assert page.connect_button.text() == CONNECT_LABEL
        assert page.read_button.text() == READ_CONFIG_LABEL
        assert page.apply_button.text() == APPLY_LABEL
        assert not page.read_button.isEnabled()
        assert not page.apply_button.isEnabled()

        page._render({'connected': True, 'idn': 'FAKE', 'vendor': 'x'})
        assert page.read_button.isEnabled()
        assert page.apply_button.isEnabled()

        page._set_param_busy(True, 'read')
        assert page.read_button.text() == '读取中…'
        assert not page.read_button.isEnabled()
        assert not page.apply_button.isEnabled()
        page._set_param_busy(False)
        assert page.read_button.text() == READ_CONFIG_LABEL
        assert page.apply_button.text() == APPLY_LABEL

        page._render({'connected': False, 'idn': None, 'vendor': None})
        assert not page.read_button.isEnabled()
        assert not page.apply_button.isEnabled()
    finally:
        _close(page)


def test_connect_sends_no_sweep_values(app):
    page = VnaPage(_state())
    seen: dict = {}

    def fake_connect(state, body):
        seen.update(body)
        return {'connected': True, 'idn': 'FAKE', 'vendor': 'x',
                'last_error': None, 'config': state.vna['config']}

    try:
        with mock.patch.object(pages_vna.handlers, 'vna_connect',
                               side_effect=fake_connect), \
                _synchronous_background():
            page.on_connect()
        for key in ('start_hz', 'stop_hz', 'points', 'step_hz',
                    'power_dbm', 'ifbw_hz'):
            assert key not in seen
        assert seen['host'] == '192.168.2.111'
        assert page.connect_button.text() == CONNECT_LABEL
    finally:
        _close(page)


def test_single_scan_only_fetches_and_triggers_once(app):
    page = VnaPage(_state())
    page._is_connected = True
    page._show_scan = mock.Mock()
    calls: list[str] = []

    def fake_fetch(state, body):
        calls.append('fetch')
        return {'frequencies_hz': [], 's': {}}

    def forbidden(*args, **kwargs):
        raise AssertionError('a scan must not write parameters or trigger twice')

    try:
        with mock.patch.object(pages_vna.handlers, 'vna_fetch',
                               side_effect=fake_fetch), \
                mock.patch.object(pages_vna.handlers, 'vna_set_sweep',
                                  side_effect=forbidden), \
                mock.patch.object(pages_vna.handlers, 'vna_set_power',
                                  side_effect=forbidden), \
                mock.patch.object(pages_vna.handlers, 'vna_set_ifbw',
                                  side_effect=forbidden), \
                mock.patch.object(pages_vna.handlers, 'vna_trigger',
                                  side_effect=forbidden), \
                _synchronous_background():
            page.on_scan()
        assert calls == ['fetch']
        page._show_scan.assert_called_once()
    finally:
        _close(page)


def test_apply_sweep_is_the_only_action_that_writes(app):
    page = VnaPage(_state())
    page._is_connected = True
    page.start_spin.lineEdit()  # ensure the widget tree is built
    calls: list[str] = []

    def record(name):
        return lambda state, body: calls.append(name) or {
            'connected': True, 'idn': 'FAKE', 'vendor': 'x',
            'last_error': None, 'config': state.vna['config']}

    try:
        with mock.patch.object(pages_vna.handlers, 'vna_set_sweep',
                               side_effect=record('sweep')), \
                mock.patch.object(pages_vna.handlers, 'vna_set_power',
                                  side_effect=record('power')), \
                mock.patch.object(pages_vna.handlers, 'vna_set_ifbw',
                                  side_effect=record('ifbw')), \
                mock.patch.object(pages_vna.handlers, 'vna_status',
                                  side_effect=record('status')), \
                _synchronous_background():
            page.on_apply_sweep()
        assert calls == ['sweep', 'power', 'ifbw', 'status']
        assert all(spin.isReadOnly() for spin in _spins(page))
    finally:
        _close(page)


def test_read_config_fills_form_and_relocks_without_stale_warning(app):
    page = VnaPage(_state())
    page._is_connected = True
    live = {
        'start_hz': 2e9, 'stop_hz': 6e9, 'points': 201,
        'power_dbm': -3.0, 'ifbw_hz': 2000.0,
        'read': ['start_hz', 'stop_hz', 'points', 'power_dbm', 'ifbw_hz'],
        'fallback': [], 'source': 'instrument',
    }
    _click(page.power_spin.lineEdit())  # operator had unlocked one field
    try:
        with mock.patch.object(pages_vna.handlers, 'vna_read_config',
                               return_value={'config': live}), \
                _synchronous_background():
            page.on_read_config()
        assert page.start_spin.value() == pytest.approx(2.0)
        assert page.stop_spin.value() == pytest.approx(6.0)
        assert page.points_spin.value() == 201
        assert page.power_spin.value() == pytest.approx(-3.0)
        assert page.ifbw_spin.value() == pytest.approx(2000.0)
        assert '201 点' in page.span_label.text()
        assert all(spin.isReadOnly() for spin in _spins(page))
        assert not page.stale_label.isVisible()
    finally:
        _close(page)

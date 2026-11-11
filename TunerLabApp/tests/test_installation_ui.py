"""A07/A08/A17: the actual desktop pages share one installation context."""
import copy
from dataclasses import replace

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QThreadPool

from tuner.settings import SettingsStore
from tunerlab_app.session import TunerSession
from tunerlab_app.pages_comm import CommunicationPage
from tunerlab_app.pages_motion import SafeAxisMotionPage
from tunerlab_app.pages_params import SafeAxisParameterPage


@pytest.fixture
def pages(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: warnings.append(args[-1]))
    hw = TunerSession(SettingsStore(tmp_path / 'settings.json'))
    hw.set_layout('dual_3')
    comm = CommunicationPage(hw)
    motion = SafeAxisMotionPage(hw)
    params = SafeAxisParameterPage(hw)
    motion.timer.stop()
    yield app, hw, comm, motion, params, warnings
    QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()
    for page in (comm, motion, params):
        page.close()


def test_A07_all_three_pages_switch_together_and_parameter_rows_have_physical_ids(pages):
    app, hw, comm, motion, params, warnings = pages
    hw.connect(mode='virtual')
    old_button = motion.axis_panels['X']['abs_btn']
    old_context = old_button.property('installation_context')
    comm.unit_combo.setCurrentIndex(comm.unit_combo.findData('right'))
    assert hw.settings.active_unit == 'right'
    assert motion.installation_bar.selector.currentData() == 'right'
    assert params.installation_bar.selector.currentData() == 'right'
    assert '轴3' in params.table.item(0, 0).text()
    assert params.table.rowCount() == 3
    assert old_context != hw.context()
    assert not warnings


@pytest.mark.parametrize('choice,switched,saved', [
    (QMessageBox.StandardButton.Cancel, False, False),
    (QMessageBox.StandardButton.Discard, True, False),
    (QMessageBox.StandardButton.Save, True, True),
])
def test_A08_dirty_draft_has_save_discard_cancel_without_hardware_writes(pages, monkeypatch, choice, switched, saved):
    app, hw, comm, motion, params, warnings = pages
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: choice)
    old = hw.settings.axes['X'].speed
    params.table.item(0, 3).setText('27')
    assert params.dirty
    comm.unit_combo.setCurrentIndex(comm.unit_combo.findData('right'))
    assert (hw.settings.active_unit == 'right') == switched
    assert hw.settings.profiles['dual_3']['left']['X'].speed == (27 if saved else old)
    assert not hw.connected
    if not switched:
        assert params.dirty
        assert params.table.item(0, 3).text() == '27'


def test_A17_queued_old_panel_cannot_target_new_tuner(pages):
    app, hw, comm, motion, params, warnings = pages
    hw.connect(mode='virtual')
    old_button = motion.axis_panels['X']['abs_btn']
    hw.set_active_unit('right')
    # deleteLater has not run yet: a queued click is still possible.
    old_button.click()
    app.processEvents()
    assert any('版本' in warning for warning in warnings)
    assert not motion._motion_busy


def test_A01_A04_layout_selector_rebuilds_all_pages_and_hides_invalid_tuners(pages):
    app, hw, comm, motion, params, warnings = pages
    for layout, axes in [('single_4', ('X', 'Z1', 'Z2', 'Z3')),
                         ('single_3', ('X', 'Z1', 'Z2'))]:
        comm.model_combo.setCurrentIndex(comm.model_combo.findData(layout))
        assert hw.axes == axes
        assert params.table.rowCount() == len(axes)
        assert set(motion.axis_panels) == set(axes)
        assert comm.unit_combo.count() == 1
        assert not params.installation_bar.selector.isEnabled()
    assert not warnings


def test_A11_read_does_not_replace_draft_and_copy_does_not_copy_wiring(pages):
    app, hw, comm, motion, params, warnings = pages
    hw.connect(mode='virtual')
    original_wiring = hw.settings.axes['X'].datum_in
    source = hw.settings.profiles['single_4']['tuner_1']['X']
    hw.settings.profiles['single_4']['tuner_1']['X'] = replace(source, speed=23, datum_in=30, home_output=8)
    params.copy_parameters('single_4', 'tuner_1')
    assert params.table_to_params()['X'].speed == 23
    assert params.table_to_params()['X'].datum_in == original_wiring
    assert params.table_to_params()['X'].home_output != 8
    params.on_read()
    QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()
    assert params.table_to_params()['X'].speed == 23
    assert hw.settings.axes['X'].speed != 23
    assert params.dirty
    assert params._live


def test_A07_new_panel_after_connect_has_a_current_context_and_can_move(pages):
    app, hw, comm, motion, params, warnings = pages
    hw.connect(mode='virtual')
    panel = motion.axis_panels['Z1']
    panel['target'].setValue(.1)
    panel['abs_btn'].click()
    QThreadPool.globalInstance().waitForDone(2000)
    app.processEvents()
    assert hw.link.get_dpos(1) == pytest.approx(.1)
    assert not warnings


def test_axis_wiring_polarity_and_output_are_independent_of_physical_axis(pages):
    from tunerlab_app.motion_adapter import TunerMotionController
    from tuner.zmotion_motion import build_tuner_config
    from tuner.axis_params import default_axis_params
    from types import SimpleNamespace
    params = default_axis_params(3)
    params['Z1'] = replace(params['Z1'], home_output=9, datum_in=27, datum_active=0)
    link = SimpleNamespace(connected=True, _basic_query=lambda cmd: '0')
    ctrl = TunerMotionController(build_tuner_config(params, axis_count=3), link, params,
                                 axis_count=3, axis_base=3)
    assert ctrl.physical_axis('Z1') == 4
    assert ctrl.output('Z1') == 9
    assert ctrl.datum_channel('Z1') == 27
    assert ctrl.origin_is_on('Z1') is True
    ctrl.axis_params['Z1'] = replace(params['Z1'], home_output=-1, datum_in=-1)
    assert ctrl.datum_channel('Z1') == -1
    with pytest.raises(Exception, match='输出未配置'):
        ctrl.output('Z1')

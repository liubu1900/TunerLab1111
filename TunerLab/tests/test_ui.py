"""Offscreen widget workflow test: both mechanical variants use the same UI."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from tuner.ui import MainWindow

@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])

@pytest.mark.parametrize('variant',[0,1])
def test_ui_mock_workflow(app,tmp_path,variant):
    window=MainWindow()
    window.probes.setCurrentIndex(variant)
    window.initialize_hardware()
    assert window.config.probes==variant+2
    assert window.stack.count()==8
    window.x_count.setValue(2); window.z_count.setValue(2)
    window.path.setText(str(tmp_path/f'ui_{variant}.h5'))
    window.prepare_calibration()
    assert window.manager.run()=='completed'
    window.load_database_path(window.path.text())
    assert window.data['valid'].all()
    window.search_target()
    assert len(window.last_candidates)>0
    window.move_candidate()
    assert 'RF impedance not yet measured' in window.tuning_result.text()
    window.chart.resize(500,400); window.chart.grab()
    window.close()

def test_zero_valid_checkpoint_resume_and_verify(app,tmp_path):
    window=MainWindow(); window.initialize_hardware()
    window.x_count.setValue(2); window.z_count.setValue(2)
    window.path.setText(str(tmp_path/'fresh.h5')); window.prepare_calibration()
    window.load_database_path(window.path.text(),resume=True)
    assert window.search_engine is None
    assert window.manager.run()=='completed'
    window.load_database_path(window.path.text())
    window.search_target(); window.move_candidate(); window.verify_target()
    assert window.validation_report['simulated']
    assert not window.validation_report['rf_hardware_verified']
    window.config.x_limits=(0.,9.)
    with pytest.raises(ValueError,match='Full hardware'):
        window.move_candidate()
    window.initialize_hardware()
    assert window.data is None and window.search_engine is None
    assert not window.last_candidates
    window.close()


def test_repeatability_runs_both_positions(app,tmp_path):
    window=MainWindow(); window.initialize_hardware()
    window.x_count.setValue(2); window.z_count.setValue(2)
    window.path.setText(str(tmp_path/'repeat.h5')); window.prepare_calibration()
    window.manager.run(); window.load_database_path(window.path.text())
    window.validate_repeatability()
    assert window.worker.wait(10000)
    app.processEvents()
    assert set(window.validation_report['positions'])=={'A','B'}
    assert window.validation_report['simulated']
    window.close()

def test_background_calibration_worker(app,tmp_path):
    from PySide6.QtCore import QEventLoop,QTimer
    window=MainWindow(); window.initialize_hardware()
    window.x_count.setValue(2); window.z_count.setValue(2)
    window.path.setText(str(tmp_path/'worker.h5')); window.prepare_calibration()
    loop=QEventLoop(); window.run_calibration()
    window.worker.finished.connect(loop.quit)
    QTimer.singleShot(10000,loop.quit)
    loop.exec(); app.processEvents()
    assert not window.worker.isRunning()
    assert window.manager.state=='completed'
    assert window.data['valid'].all()
    window.close()

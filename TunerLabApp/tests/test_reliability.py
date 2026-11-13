"""Windows paths, worker delivery, lifecycle and release regression checks."""
import importlib.util
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_data_override_and_unique_names(tmp_path, monkeypatch):
    from tuner import appdirs
    monkeypatch.setenv('TUNERLAB_DATA_DIR', str(tmp_path))
    assert appdirs.data_dir() == tmp_path
    names = {appdirs.default_calibration_path() for _ in range(100)}
    assert len(names) == 100
    assert all(p.parent == tmp_path / 'calibrations' for p in names)


def test_windows_redirected_documents(monkeypatch, tmp_path):
    import ctypes
    from tuner import appdirs
    def get_folder(window, folder, token, flags, buffer):
        assert folder == 5
        buffer.value = str(tmp_path / 'OneDrive' / 'Documents')
        return 0
    monkeypatch.delenv('TUNERLAB_DATA_DIR', raising=False)
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(
        shell32=SimpleNamespace(SHGetFolderPathW=get_folder)), raising=False)
    monkeypatch.setattr(appdirs.sys, 'platform', 'win32')
    assert appdirs.data_dir() == tmp_path / 'OneDrive' / 'Documents' / 'TunerLab'


@pytest.mark.parametrize('fail', [False, True])
def test_background_callbacks_run_on_gui_thread(app, fail):
    from tunerlab_app.bg import run_in_background, _pending
    gui_thread = threading.get_ident()
    workers, callbacks, results = [], [], []
    loop = QEventLoop()
    def work():
        workers.append(threading.get_ident())
        if fail:
            raise ValueError('expected failure')
        return 42
    def received(value):
        callbacks.append(threading.get_ident())
        results.append(value)
        loop.quit()
    task = run_in_background(work, received, received)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert workers and workers[0] != gui_thread
    assert callbacks == [gui_thread]
    assert isinstance(results[0], ValueError) if fail else results == [42]
    assert task not in _pending


def test_cancel_exit_preserves_console(app, monkeypatch):
    from tunerlab_app.shell import MainWindow
    window = MainWindow(smoke_test=True)
    stopped, cancelled = [], []
    monkeypatch.setattr(window.console, 'stop', lambda: stopped.append(True))
    window.state.studio['calibration_thread'] = SimpleNamespace(is_alive=lambda: True)
    window.state.studio['manager'] = SimpleNamespace(cancel=lambda: cancelled.append(True))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.No)
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted() and not stopped and not cancelled
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    window.closeEvent(event)
    assert not event.isAccepted() and not stopped and cancelled == [True]
    window.state.studio['calibration_thread'] = None
    window.state.studio['manager'] = None
    window.close()
    assert stopped == [True]


def test_console_binds_once_and_restarts(app, monkeypatch):
    from tunerlab_app.web_console import WebConsole
    from tunerlab_app import web_console
    from tuner.web.state import AppState
    import urllib.request
    monkeypatch.setattr(web_console, 'free_port', lambda *a: pytest.fail('port probe race'))
    console = WebConsole(AppState())
    for _ in range(2):
        try:
            url = console.start(open_browser=False)
            assert console.start(open_browser=False) == url
            with urllib.request.urlopen(url, timeout=5) as response:
                assert response.status == 200
                assert b'<html' in response.read().lower()
        finally:
            console.stop()
        assert not console.running


def test_release_rejects_equal_size_asset_corruption(tmp_path):
    spec = importlib.util.spec_from_file_location('verify_release',
        Path(__file__).resolve().parents[1] / 'packaging' / 'verify_release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    folder = tmp_path / 'TunerLab-test'
    assets = folder / '_internal/tuner/web/static'
    assets.mkdir(parents=True)
    (folder / 'TunerLab.exe').write_bytes(b'executable')
    for name in ('index.html', 'app.js', 'style.css'):
        (assets / name).write_bytes(b'original')
    archive = tmp_path / 'release.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        for path in folder.rglob('*'):
            if path.is_file():
                z.write(path, path.relative_to(tmp_path))
    assert module.verify(folder, archive)
    (assets / 'app.js').write_bytes(b'modified')
    assert not module.verify(folder, archive)


def test_windows_release_versions_agree():
    from tunerlab_app.version import __version__
    root = Path(__file__).resolve().parents[1] / 'packaging'
    resource = (root / 'version_info.txt').read_text(encoding='utf-8')
    installer = (root / 'installer.iss').read_text(encoding='utf-8')
    assert f'"{__version__}"' in installer
    # A normal X.Y.Z release displays as X.Y.Z.0 in the Windows resource;
    # a four-part patch release (e.g. 0.5.13.2) already fills the quadruplet.
    parts = __version__.split('.')
    four_parts = len(parts) == 4
    display = __version__ if four_parts else __version__ + '.0'
    numeric = ', '.join(parts if four_parts else parts + ['0'])
    assert repr(display) in resource
    assert f'filevers=({numeric})' in resource
    assert f'prodvers=({numeric})' in resource

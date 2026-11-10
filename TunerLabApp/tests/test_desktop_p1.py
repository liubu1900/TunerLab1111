"""Desktop P1 widget tests (F-10 / F-11 / F-12 / F-19 / F-21 / F-24).

The desktop app is the product the operator actually runs, so its P1 widgets
need their own tests.  They run offscreen (``QT_QPA_PLATFORM=offscreen``) and
never touch the 576-point production library: the browser page works against a
synthetic library written into ``tmp_path``.
"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from tuner.database import CalibrationDatabase
from tuner.models import TunerConfig, Measurement
from tunerlab_app.pages_p1 import DeviationCard, ReferencePlanePanel
from tunerlab_app.point_cloud import ProgressCloudWidget
from tunerlab_app.smith_spec import SmithSpec


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


# ----------------------------------------------------------------------
# F-24: one formatting vocabulary
# ----------------------------------------------------------------------
def test_smith_spec_formats_every_quantity():
    spec = SmithSpec()
    metrics = spec.metrics(0.3 + 0.4j)
    assert metrics['gamma_label'] == '+0.5000∠+53.1°'
    assert metrics['gamma_rect'] == '+0.3000+0.4000j'
    assert metrics['z_label'].endswith('Ω')
    assert metrics['vswr'] == '3.00'
    assert metrics['return_loss'] == '6.02 dB'
    assert metrics['band_key'] == 'mid'
    assert '中' in metrics['band_label']


def test_smith_spec_handles_the_degenerate_ends():
    spec = SmithSpec()
    assert spec.vswr(1 + 0j) == '∞'          # total reflection
    assert spec.return_loss(0 + 0j) == '∞'    # perfect match
    assert spec.vswr(None) == '—'


def test_comparison_card_uses_the_same_format():
    spec = SmithSpec(tolerance=0.05)
    report = spec.comparison_card(0.3 + 0.4j, 0.32 + 0.41j)
    assert report['within_tolerance'] is True
    assert report['predicted_card']['gamma_rect'] == '+0.3000+0.4000j'
    assert report['measured_card']['gamma_rect'] == '+0.3200+0.4100j'
    assert report['gamma_band_label']
    # A high-|Gamma| point suggests a relaxed tolerance, as standard prescribes.
    high = spec.comparison_card(0.95 + 0j, 0.96 + 0j)
    assert high['gamma_band'] == 'high'
    assert high['suggested_tolerance'] > 0.05


# ----------------------------------------------------------------------
# F-11: the point cloud widget
# ----------------------------------------------------------------------
def test_progress_cloud_widget_paints_without_data(app):
    widget = ProgressCloudWidget()
    widget.set_cloud(None)
    widget.grab()          # must not raise


def test_progress_cloud_widget_accepts_a_cloud(app):
    from tuner.progress_cloud import progress_cloud
    config = TunerConfig()
    xv = np.linspace(config.x_limits[0], config.x_limits[1], 3)
    zv = np.linspace(config.z_limits[0], config.z_limits[1], 2)
    positions = config.grid(xv, zv)
    valid = np.zeros(len(positions), bool)
    valid[:4] = True
    cloud = progress_cloud({'positions': positions, 'valid': valid})
    widget = ProgressCloudWidget()
    widget.set_cloud(cloud)
    widget.grab()
    assert widget._layers                       # the cloud reached the widget
    assert '4 /' in widget._caption


# ----------------------------------------------------------------------
# F-19: the deviation card
# ----------------------------------------------------------------------
def test_deviation_card_renders_a_report(app):
    spec = SmithSpec(tolerance=0.05)
    report = spec.comparison_card(0.3 + 0.4j, 0.32 + 0.41j)
    card = DeviationCard()
    card.show_report(report)
    assert card.table.rowCount() == 6            # Γ×2, Z, z, VSWR, RL
    assert '达标' in card.verdict.text()
    card.grab()


def test_deviation_card_shows_an_out_of_tolerance_result(app):
    spec = SmithSpec(tolerance=0.01)
    report = spec.comparison_card(0.3 + 0.4j, 0.5 + 0.6j)
    card = DeviationCard()
    card.show_report(report)
    assert '超差' in card.verdict.text()


# ----------------------------------------------------------------------
# F-10: the reference-plane panel
# ----------------------------------------------------------------------
class _StubState:
    """The panel only needs ``studio`` for the handler call.

    ``studio['data']`` carries the library frequencies so the S2P coverage gate
    has something to check against — the same way a loaded library feeds it.
    """

    def __init__(self, frequencies=(5e9,)):
        self.studio = {'data': {'frequencies': np.asarray(frequencies, float)}}
        self.studio_lock = __import__('threading').Lock()


def test_reference_plane_panel_lists_six_planes(app):
    from tuner.web import handlers
    panel = ReferencePlanePanel(_StubState())
    assert panel.plane.count() == 6
    assert panel.reference_plane() == 'standard'


def test_reference_plane_panel_flags_a_missing_frequency(app, tmp_path):
    """A S2P that does not cover the library frequency must abort, not interpolate."""
    s2p = tmp_path / 'fixture.s2p'
    s2p.write_text('# Hz S RI R 50\n1e9 0 0 0 0 0 0 0 0\n', encoding='utf-8')
    panel = ReferencePlanePanel(_StubState())
    panel.s2p_path.setText(str(s2p))
    setup = panel.check()
    assert setup is not None
    coverage = setup['coverage']
    assert coverage['passed'] is False
    assert coverage['missing']
    assert '中止' in panel.status.text()


def test_reference_plane_panel_accepts_full_coverage(app, tmp_path):
    s2p = tmp_path / 'fixture.s2p'
    s2p.write_text('# Hz S RI R 50\n5e9 0 0 0 0 0 0 0 0\n', encoding='utf-8')
    panel = ReferencePlanePanel(_StubState())
    panel.s2p_path.setText(str(s2p))
    setup = panel.check()
    assert setup is not None
    assert setup['coverage']['passed'] is True
    assert '覆盖完整' in panel.status.text()


# ----------------------------------------------------------------------
# Smoke-test hardware isolation (safety regression)
# ----------------------------------------------------------------------
def test_smoke_test_never_reaches_the_real_controller(app, monkeypatch):
    """A smoke test must not open a link to the machine.

    The smoke test used to honour "connect on start", so it opened a live link
    to the real controller — and the motion page's startup path can issue a
    positioning command on it.  An unattended installation check must never do
    that, so the smoke test now skips the auto-connect and forces the virtual
    controller *in memory only* (the operator's saved settings must survive it
    untouched).
    """
    from tunerlab_app import shell

    calls = []
    monkeypatch.setattr(shell.TunerSession, 'connect',
                        lambda self, ip=None, mode=None: calls.append('connect'))

    window = shell.MainWindow(smoke_test=True)
    try:
        assert window._smoke_test is True
        # The in-memory mode is virtual, so nothing can reach the machine.
        assert window.session.settings.mode == 'virtual'
        # No auto-connect was scheduled.
        assert not calls
    finally:
        window.close()


def test_smoke_test_does_not_rewrite_the_settings_file(app):
    """The smoke test must not persist the virtual mode to disk.

    ``session.set_mode()`` saves; writing ``settings.mode`` directly does not.
    If this ever regresses, an installation check would silently rewrite the
    operator's configuration — e.g. switching a workshop machine from the real
    controller to the simulator without anybody asking.
    """
    from tunerlab_app import shell
    from tuner.settings import default_settings_path

    path = default_settings_path()
    before = path.read_bytes() if path.is_file() else None

    window = shell.MainWindow(smoke_test=True)
    try:
        assert window.session.settings.mode == 'virtual'
    finally:
        window.close()

    after = path.read_bytes() if path.is_file() else None
    assert after == before, 'the smoke test rewrote the operator settings file'


# ----------------------------------------------------------------------
# Layout regression (the screenshot that started this)
# ----------------------------------------------------------------------
def test_setup_page_layout_has_no_overlaps(app):
    """The setup page must not draw its form rows on top of each other.

    The screenshot showed every input crushed to a few pixels and the labels
    painted over them.  The cause was not a colour or a font: the page needed
    923 px while the window offered 824 px, and Qt resolves that shortfall by
    shrinking the nested grids below their minimum height.

    The page is now a two-column workbench *and* it lives in a scroll area, so
    it keeps its natural height at any window size.  This test measures the
    real geometry and fails if anything is crushed or overlaps again.
    """
    from tunerlab_app.layout_check import check_page
    from tunerlab_app.shell import MainWindow

    window = MainWindow(smoke_test=True)
    try:
        window.resize(1340, 880)
        window.show()
        app.processEvents()
        window.go('studio-setup')
        app.processEvents()
        problems = check_page('studio-setup', window.pages['studio-setup'])
        assert not problems, '\n'.join(str(p) for p in problems)
    finally:
        window.close()


def test_setup_page_fits_a_small_window(app):
    """At the 1140x720 minimum the page must scroll, not crush its fields."""
    from tunerlab_app.shell import MainWindow

    window = MainWindow(smoke_test=True)
    try:
        window.resize(1140, 720)
        window.show()
        app.processEvents()
        window.go('studio-setup')
        app.processEvents()
        view = window.views['studio-setup']
        # The view is a scroll area, and the page inside keeps its height.
        assert view.__class__.__name__ == 'QScrollArea'
        page = window.pages['studio-setup']
        assert page.height() >= page.minimumSizeHint().height() - 1
    finally:
        window.close()


# ----------------------------------------------------------------------
# Desktop ↔ browser console: one state, two faces
# ----------------------------------------------------------------------
def test_the_console_serves_the_console_assets(app):
    """The browser console must be servable from inside the desktop process."""
    import json
    import urllib.request

    from tuner.settings import SettingsStore
    from tunerlab_app.session import TunerSession
    from tunerlab_app.shell import SharedState
    from tunerlab_app.web_console import WebConsole

    state = SharedState(TunerSession(SettingsStore()))
    console = WebConsole(state)
    url = console.start(open_browser=False)
    try:
        assert console.running
        base = url.rstrip('/')
        for path, minimum in (('/', 1000), ('/static/app.js', 1000),
                              ('/static/style.css', 200)):
            with urllib.request.urlopen(base + path, timeout=5) as response:
                assert response.status == 200
                assert len(response.read()) > minimum
        # A request that needs the shared state proves the two faces agree.
        # It is a *read* on purpose: the test must not leave a pattern file in
        # the operator's own folder.
        with urllib.request.urlopen(base + '/api/studio/patterns',
                                    timeout=5) as response:
            payload = json.loads(response.read().decode('utf-8'))
        assert payload['ok'] is True
        assert 'patterns' in payload['data']
    finally:
        console.stop()
    assert not console.running


def test_the_console_uses_a_free_port(app):
    from tuner.settings import SettingsStore
    from tunerlab_app.session import TunerSession
    from tunerlab_app.shell import SharedState
    from tunerlab_app.web_console import WebConsole, free_port

    assert free_port() > 0
    state = SharedState(TunerSession(SettingsStore()))
    console = WebConsole(state)
    url = console.start(open_browser=False)
    try:
        assert console.port > 0
        assert str(console.port) in url
    finally:
        console.stop()


def test_both_faces_use_the_same_directory_policy(app):
    """The console and the shell must resolve the same folders."""
    from tuner.appdirs import pattern_dir
    from tunerlab_app import paths

    assert paths.pattern_dir() == pattern_dir()
    assert paths.calibration_dir() == __import__(
        'tuner.appdirs', fromlist=['calibration_dir']).calibration_dir()


# ----------------------------------------------------------------------
# F-05: the frequency mode drives the VNA-frequency panel
# ----------------------------------------------------------------------
def _setup_page(app):
    """The setup page with the frequency step on screen."""
    from tunerlab_app.shell import MainWindow

    window = MainWindow(smoke_test=True)
    window.resize(1340, 880)
    window.show()
    app.processEvents()
    window.go('studio-setup')
    app.processEvents()
    page = window.pages['studio-setup']
    page.wizard.go(2)          # step 3 — 频率方案
    app.processEvents()
    return window, page


def test_each_frequency_mode_shows_only_the_fields_it_uses(app):
    """Switching the mode must reveal exactly the fields that mode reads.

    A greyed-out field still looks like a setting the operator forgot; hiding
    the whole row is the only unambiguous presentation.
    """
    window, page = _setup_page(app)
    try:
        expected = {
            'fo_list': {'fo'},
            'freq_sweep': {'start', 'stop', 'points'},
            'harmonic_list': {'fo', 'harmonics'},
            'arbitrary_list': {'arbitrary'},
        }
        for mode, want in expected.items():
            page.freq_mode.setCurrentIndex(page.freq_mode.findData(mode))
            app.processEvents()
            shown = {key for key, (_label, widget) in page._freq_rows.items()
                     if not widget.isHidden()}
            labels = {key for key, (label, _widget) in page._freq_rows.items()
                      if not label.isHidden()}
            assert shown == want, f'{mode}: 控件可见 {shown} ≠ {want}'
            assert labels == want, f'{mode}: 标签可见 {labels} ≠ {want}'
    finally:
        window.close()


def test_the_frequency_preview_shows_the_real_grid(app):
    """The preview must state the grid that will really be measured."""
    window, page = _setup_page(app)
    try:
        page.freq_mode.setCurrentIndex(page.freq_mode.findData('fo_list'))
        app.processEvents()
        text = page.freq_preview.text()
        assert '1 个频点' in text
        assert '5.0000' in text

        page.freq_mode.setCurrentIndex(page.freq_mode.findData('freq_sweep'))
        app.processEvents()
        text = page.freq_preview.text()
        assert '401 个频点' in text
        assert '1.0000' in text and '20.0000' in text
        assert '47.500' in text          # step in MHz

        page.freq_mode.setCurrentIndex(page.freq_mode.findData('harmonic_list'))
        app.processEvents()
        assert '3 个频点' in page.freq_preview.text()

        page.freq_mode.setCurrentIndex(page.freq_mode.findData('arbitrary_list'))
        app.processEvents()
        assert '3 个频点' in page.freq_preview.text()
    finally:
        window.close()


def test_an_invalid_frequency_plan_is_reported_not_crashed(app):
    """A bad plan must show an error, never take the page down."""
    window, page = _setup_page(app)
    try:
        page.freq_mode.setCurrentIndex(page.freq_mode.findData('arbitrary_list'))
        page.arbitrary_edit.setText('abc')
        app.processEvents()
        assert '无效' in page.freq_preview.text()
        # And it recovers as soon as the value is valid again.
        page.arbitrary_edit.setText('5,10,15')
        app.processEvents()
        assert '实际频率方案' in page.freq_preview.text()
    finally:
        window.close()


# ----------------------------------------------------------------------
# F-03: the premise gate no longer blocks, but its criteria stay visible
# ----------------------------------------------------------------------
def test_the_criteria_dialog_explains_every_check(app):
    """The operator must be able to read *why* a check could fail."""
    from tuner.web import handlers

    data = handlers.studio_prerequisite_criteria(None, {})
    items = data['criteria']['items']
    assert len(items) == 6
    for item in items:
        assert item['method'], item['id']
        assert item['hard'], item['id']
        assert item['warn'], item['id']
    assert '判定方法' in data['text']
    assert '硬阻止' in data['text']


def test_a_failed_check_no_longer_blocks_the_library(app):
    """The gate was dropped from the build flow — the verdict is only recorded."""
    from tunerlab_app.shell import MainWindow

    window = MainWindow(smoke_test=True)
    try:
        page = window.pages['studio-setup']
        # The setup page must not ask the backend to enforce the gate.
        assert 'require_prerequisites' not in page._plan_body()
    finally:
        window.close()


def test_vna_summary_survives_a_page_without_kpi_tiles(app):
    """The scan summary must not depend on KPI tiles the page does not have.

    Regression (2026-11-08): ``VnaPage._summarise`` updated ``kpi_points`` and
    ``kpi_step`` unconditionally, while every other update site on that page
    guards with ``hasattr``.  The page has no KPI tiles, so a real sweep raised
    ``AttributeError`` inside the background-task callback -- the sweep itself
    succeeded, but the summary line never appeared and the operator was left
    looking at "尚未扫描".
    """
    from PySide6.QtWidgets import QLabel

    from tunerlab_app.pages_vna import VnaPage

    page = VnaPage.__new__(VnaPage)
    page.summary_label = QLabel('尚未扫描')
    assert not hasattr(page, 'kpi_points')

    frequencies = np.linspace(1e9, 6e9, 21)
    page._summarise(frequencies, {'points': 21, 'step_hz': 250e6})

    text = page.summary_label.text()
    assert '21' in text
    assert 'GHz' in text


def test_vna_summary_reports_a_disagreement_with_the_request(app):
    """A sweep that does not match the request must say so."""
    from PySide6.QtWidgets import QLabel

    from tunerlab_app.pages_vna import VnaPage

    page = VnaPage.__new__(VnaPage)
    page.summary_label = QLabel('尚未扫描')
    page._summarise(np.linspace(1e9, 6e9, 11), {'points': 21, 'step_hz': 250e6})
    assert '≠' in page.summary_label.text()

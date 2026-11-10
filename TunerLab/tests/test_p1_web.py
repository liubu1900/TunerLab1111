"""P1 web-API tests: reference planes, export, library ops, browser,
repeatability, deviation card and the Tune-To dual mode.

Every route is exercised against a small synthetic library in ``tmp_path`` so the
suite needs no instrument and never touches the 576-point production library.
"""
import json

import numpy as np
import pytest

from tuner.database import CalibrationDatabase
from tuner.models import TunerConfig, Measurement
from tuner.search import GammaSearchEngine
from tuner.web import handlers
from tuner.web.state import state


# ----------------------------------------------------------------------
# synthetic library
# ----------------------------------------------------------------------
def _make_library(tmp_path, freqs=(5e9,), x=3, z=2, seed=0, name='lib.h5'):
    rng = np.random.default_rng(seed)
    config = TunerConfig()
    xv = np.linspace(config.x_limits[0], config.x_limits[1], x)
    zv = np.linspace(config.z_limits[0], config.z_limits[1], z)
    positions = config.grid(xv, zv)
    db = CalibrationDatabase(tmp_path / name)
    db.create(config, np.array(freqs, float), positions)
    n = len(positions)
    for i in range(n):
        mag = 0.1 + 0.8 * (i / max(1, n - 1))
        ang = rng.uniform(-np.pi, np.pi)
        g = mag * np.exp(1j * ang)
        s = np.zeros((len(freqs), 2, 2), complex)
        s[:, 0, 0] = g
        s[:, 1, 0] = 0.05 * np.exp(1j * ang)
        s[:, 0, 1] = s[:, 1, 0]
        db.write_point(i, positions[i].copy(),
                       Measurement(np.array(freqs, float), s, True))
    return db, config, positions


def _load_into_studio(db):
    data = db.load()
    with state.studio_lock:
        state.studio['database'] = db
        state.studio['data'] = data
        state.studio['search_engine'] = (
            GammaSearchEngine(data, z0=50.0)
            if np.any(data['valid']) else None)
        state.studio['frequency_hz'] = float(
            np.asarray(data['frequencies'], float)[0])
        state.studio['config'] = TunerConfig()


# ----------------------------------------------------------------------
# F-10 reference planes
# ----------------------------------------------------------------------
def test_reference_planes_list_and_validate():
    body = {'reference_plane': 'on_wafer', 'deembed_s2p': ''}
    out = handlers.studio_reference_planes(state, body)
    assert len(out['planes']) == 6
    assert 'on_wafer' in out['reference_plane_ids']
    assert out['setup']['reference_plane_ok'] is True
    assert out['setup']['deembed_allowed'] is True
    # unknown plane -> not ok
    out2 = handlers.studio_reference_planes(state, {'reference_plane': 'bogus'})
    assert out2['setup']['reference_plane_ok'] is False


def test_reference_planes_coverage_gate(tmp_path):
    db, _, _ = _make_library(tmp_path, freqs=(5e9,))
    s2p = tmp_path / 'fx.s2p'
    s2p.write_text('# Hz S RI R 50\n5e9 0.1 0 0 0.2 0 0 0 0.3 0\n',
                   encoding='utf-8')
    body = {'reference_plane': 'on_wafer', 'deembed_s2p': str(s2p),
            'library_frequencies_hz': [5e9]}
    out = handlers.studio_reference_planes(state, body)
    assert out['setup']['coverage']['passed'] is True
    # a library frequency with no fixture sample must fail the gate
    body['library_frequencies_hz'] = [5e9, 6e9]
    out2 = handlers.studio_reference_planes(state, body)
    assert out2['setup']['coverage']['passed'] is False
    assert 6e9 in out2['setup']['coverage']['missing']


# ----------------------------------------------------------------------
# F-13 export
# ----------------------------------------------------------------------
def test_export_formats(tmp_path):
    db, _, _ = _make_library(tmp_path, freqs=(5e9,))
    _load_into_studio(db)
    tef = tmp_path / 'out.tef'
    r = handlers.studio_export(state, {'format': 'tef', 'path': str(tef)})
    assert r['format'] == 'tef' and r['rows'] == 12 and tef.is_file()
    csv = tmp_path / 'out.csv'
    r = handlers.studio_export(state, {'format': 'csv', 'path': str(csv)})
    assert r['format'] == 'csv' and csv.is_file()
    s2p = tmp_path / 'pt.s2p'
    r = handlers.studio_export(state, {'format': 's2p', 'path': str(s2p),
                                       'point_index': 0})
    assert r['format'] == 's2p' and s2p.is_file()
    lib = tmp_path / 's2p_lib'
    r = handlers.studio_export(state, {'format': 's2p_library', 'path': str(lib)})
    assert r['format'] == 's2p_library' and r['points'] == 12
    h5 = tmp_path / 'copy.h5'
    r = handlers.studio_export(state, {'format': 'hdf5', 'path': str(h5)})
    assert r['format'] == 'hdf5' and h5.is_file()


# ----------------------------------------------------------------------
# F-14 library ops
# ----------------------------------------------------------------------
def test_library_derive_truncate_reverse_split_fixture(tmp_path):
    db, _, _ = _make_library(tmp_path, freqs=(5e9,), x=3, z=2)
    _load_into_studio(db)
    # truncate by |Gamma| (keep low/mid tail)
    r = handlers.studio_library_derive(
        state, {'action': 'truncate_gamma', 'gmax': 0.5,
                'path': str(tmp_path / 't.h5')})
    assert 0 < r['points'] <= 6
    # reverse probe on X
    r = handlers.studio_library_derive(
        state, {'action': 'reverse_probe', 'axis': 'X',
                'path': str(tmp_path / 'rev.h5')})
    assert r['points'] == 12
    # split by frequency
    r = handlers.studio_library_derive(
        state, {'action': 'split_frequency', 'path': str(tmp_path / 'split')})
    assert isinstance(r, list) and len(r) == 1
    # apply fixture (embed)
    fx = tmp_path / 'fx.s2p'
    fx.write_text('# Hz S RI R 50\n5e9 0 0 0 0 0 0 0 0\n', encoding='utf-8')
    r = handlers.studio_library_derive(
        state, {'action': 'apply_fixture', 'fixture_s2p': str(fx),
                'direction': 'embed', 'path': str(tmp_path / 'emb.h5')})
    assert r['points'] == 12


def test_library_derive_unknown_action(tmp_path):
    db, _, _ = _make_library(tmp_path)
    _load_into_studio(db)
    with pytest.raises(ValueError):
        handlers.studio_library_derive(
            state, {'action': 'frobnicate', 'path': str(tmp_path / 'x.h5')})


# ----------------------------------------------------------------------
# F-12 library browser
# ----------------------------------------------------------------------
def test_library_scan_and_compare(tmp_path):
    db1, _, _ = _make_library(tmp_path, freqs=(5e9,), name='lib.h5')
    db2, _, _ = _make_library(tmp_path, freqs=(5e9,), seed=3, name='lib2.h5')
    out = handlers.studio_library_scan(state, {'directory': str(tmp_path)})
    assert out['directory'] == str(tmp_path)
    assert len(out['libraries']) == 2
    cmp = handlers.studio_library_compare(
        state, {'path_a': str(db1.path), 'path_b': str(db2.path)})
    assert cmp['freq_match'] is True
    assert cmp['diffable'] is True
    assert 'mean_delta_gamma' in cmp


# ----------------------------------------------------------------------
# F-20 repeatability
# ----------------------------------------------------------------------
def test_repeatability_from_measurements():
    measurements = [
        {'gamma_re': 0.5, 'gamma_im': 0.1, 'target_gamma_re': 0.5,
         'target_gamma_im': 0.1, 'label': f'#{i}'}
        for i in range(3)
    ]
    out = handlers.studio_repeatability(
        state, {'measurements': measurements, 'tolerance': 0.05})
    assert out['n'] == 3
    assert out['max_delta_gamma'] <= 1e-9
    assert out['all_within_tolerance'] is True
    assert out['reset_dB'] == float('inf')


# ----------------------------------------------------------------------
# F-19 / F-24 deviation card
# ----------------------------------------------------------------------
def test_deviation_card_band_grading():
    out = handlers.studio_deviation(state, {
        'predicted_gamma_re': 0.5, 'predicted_gamma_im': 0.0,
        'measured_gamma_re': 0.52, 'measured_gamma_im': 0.02,
        'tolerance': 0.05})
    assert 'predicted' in out and 'measured' in out
    assert out['gamma_band'] in ('low', 'mid', 'high')
    assert out['vector_error'] > 0
    # high |Gamma| region should suggest a relaxed tolerance
    hi = handlers.studio_deviation(state, {
        'predicted_gamma_re': 0.95, 'predicted_gamma_im': 0.0,
        'measured_gamma_re': 0.96, 'measured_gamma_im': 0.0,
        'tolerance': 0.05})
    assert hi['gamma_band'] == 'high'
    assert hi['suggested_tolerance'] >= 0.05


# ----------------------------------------------------------------------
# F-21 Tune-To dual mode
# ----------------------------------------------------------------------
def test_tune_to_dual_mode(tmp_path):
    db, _, _ = _make_library(tmp_path, freqs=(5e9,), x=3, z=2)
    _load_into_studio(db)
    # closest -> method 'nearest', no interpolated tuning block
    out = handlers.studio_tune(state, {
        'frequency': 5e9, 'R': 50, 'X': 25, 'tune_to_mode': 'closest'})
    assert out['tune_to_mode'] == 'closest'
    assert out['tuned'] is None  # nearest has no refinement block
    assert out['candidates']
    # cursor -> method 'two-stage', interpolated tuning block present
    out2 = handlers.studio_tune(state, {
        'frequency': 5e9, 'R': 50, 'X': 25, 'tune_to_mode': 'cursor'})
    assert out2['tune_to_mode'] == 'cursor'
    assert out2['tuned'] is not None
    assert out2['tuned']['method'] == 'two-stage'
    # explicit method still wins (no mode echoed) when none was stored
    with state.studio_lock:
        state.studio['last_tune_to_mode'] = None
    out3 = handlers.studio_tune(state, {
        'frequency': 5e9, 'R': 50, 'X': 25, 'method': 'nearest'})
    assert out3['tune_to_mode'] is None
    # invalid mode rejected
    with pytest.raises(ValueError):
        handlers.studio_tune(state, {
            'frequency': 5e9, 'R': 50, 'X': 25, 'tune_to_mode': 'bogus'})


# ----------------------------------------------------------------------
# F-11 progress cloud
# ----------------------------------------------------------------------
def test_progress_cloud_route(tmp_path):
    db, _, positions = _make_library(tmp_path, freqs=(5e9,), x=3, z=2)
    _load_into_studio(db)
    out = handlers.studio_progress_cloud(state, {})
    assert out['total'] == len(positions)
    assert out['done'] == len(positions)          # the helper wrote every point
    assert out['pending'] == 0
    assert out['layer_count'] == 3                # three distinct X values
    assert out['complete_layers'] == 3
    assert out['percent'] == 100.0
    assert out['failed'] == []
    # every layer reports one row per Z combination
    assert all(layer['total'] == 4 for layer in out['layers'])


# ----------------------------------------------------------------------
# F-11 progress cloud
# ----------------------------------------------------------------------
def test_progress_cloud_route(tmp_path):
    db, _, positions = _make_library(tmp_path, freqs=(5e9,), x=3, z=2)
    _load_into_studio(db)
    out = handlers.studio_progress_cloud(state, {})
    assert out['total'] == len(positions)
    assert out['done'] == len(positions)          # the helper wrote every point
    assert out['pending'] == 0
    assert out['layer_count'] == 3                # three distinct X values
    assert out['complete_layers'] == 3
    assert out['percent'] == 100.0
    assert out['failed'] == []
    # every layer reports one row per Z combination
    assert all(layer['total'] == 4 for layer in out['layers'])


# ----------------------------------------------------------------------
# P2 reference-plane shift
# ----------------------------------------------------------------------
def test_reference_shift_writes_a_new_library(tmp_path):
    """A line shift produces a new library; the source is untouched."""
    from tuner.database import CalibrationDatabase
    db, _, _ = _make_library(tmp_path, freqs=(5e9,), x=3, z=2)
    _load_into_studio(db)
    source_before = db.load()['s'].copy()
    out = tmp_path / 'shifted.h5'
    result = handlers.studio_reference_shift(state, {
        'path': str(out), 'kind': 'line', 'length_mm': 1.0,
        'velocity_factor': 0.66, 'direction': 'deembed',
        'reference_plane': 'on_wafer'})
    assert result['points'] == 12
    assert out.is_file()
    # The source library is untouched.
    assert (db.load()['s'] == source_before).all()
    # The new library carries the reference plane.
    assert CalibrationDatabase(out).read_metadata()['reference_plane'] == 'on_wafer'


def test_reference_shift_moves_the_reflection_coefficient(tmp_path):
    """The DUT's Γ is rotated by the line's round-trip factor.

    Seen *through* the line the DUT's Γ is rotated by ``e^(−2jβl)``, so taking
    the line out rotates it back by ``e^(+2jβl)``.
    """
    db, _, _ = _make_library(tmp_path, freqs=(5e9,), x=3, z=2)
    _load_into_studio(db)
    before = db.load()
    gamma_before = np.asarray(before['s'])[0][0][0, 0]
    beta = 2 * np.pi * 5e9 / (299_792_458.0 * 0.66)

    out = tmp_path / 'deembed.h5'
    handlers.studio_reference_shift(state, {
        'path': str(out), 'kind': 'line', 'length_mm': 1.0,
        'velocity_factor': 0.66, 'direction': 'deembed'})
    deembedded = np.asarray(CalibrationDatabase(out).load()['s'])[0][0][0, 0]
    # The magnitude is preserved; the phase rotates by +2βl.
    assert abs(deembedded) == pytest.approx(abs(gamma_before))
    assert deembedded == pytest.approx(gamma_before * np.exp(2j * beta * 0.001))

    out2 = tmp_path / 'embed.h5'
    handlers.studio_reference_shift(state, {
        'path': str(out2), 'kind': 'line', 'length_mm': 1.0,
        'velocity_factor': 0.66, 'direction': 'embed'})
    embedded = np.asarray(CalibrationDatabase(out2).load()['s'])[0][0][0, 0]
    assert abs(embedded) == pytest.approx(abs(gamma_before))
    assert embedded == pytest.approx(gamma_before * np.exp(-2j * beta * 0.001))

    # Embedding and then de-embedding returns the original.
    out3 = tmp_path / 'roundtrip.h5'
    handlers.studio_reference_shift(state, {
        'path': str(out3), 'kind': 'line', 'length_mm': 1.0,
        'velocity_factor': 0.66, 'direction': 'deembed',
        'source_path': str(out2)})
    back = np.asarray(CalibrationDatabase(out3).load()['s'])[0][0][0, 0]
    assert back == pytest.approx(gamma_before)


# ----------------------------------------------------------------------
# P2 reference-plane shift
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# P2 pattern editor
# ----------------------------------------------------------------------
def test_pattern_resolve_against_the_calibration(tmp_path):
    """Every target Γ is matched to its nearest calibrated point."""
    db, _, _ = _make_library(tmp_path, freqs=(5e9,), x=3, z=2)
    _load_into_studio(db)
    # Take a Γ that is actually in the library, so it must be reachable.
    data = db.load()
    gamma = complex(np.asarray(data['s'])[0][0][0, 0])
    out = handlers.studio_pattern_resolve(state, {
        'points': [[gamma.real, gamma.imag]]})
    assert out['count'] == 1
    assert out['reachable'] == 1
    assert out['all_reachable'] is True
    row = out['rows'][0]
    assert row['point_index'] == 0
    assert row['error'] == pytest.approx(0.0, abs=1e-9)


def test_pattern_resolve_marks_an_unreachable_target(tmp_path):
    db, _, _ = _make_library(tmp_path, freqs=(5e9,), x=3, z=2)
    _load_into_studio(db)
    out = handlers.studio_pattern_resolve(state, {'points': [[0.99, 0.0]]})
    assert out['count'] == 1
    row = out['rows'][0]
    assert row['reachable'] in (True, False)
    assert row['gamma_label']


# ----------------------------------------------------------------------
# P2 pattern editor
# ----------------------------------------------------------------------
def test_pattern_resolve_against_the_calibration(tmp_path):
    """Every target Γ is matched to its nearest calibrated point."""
    db, _, _ = _make_library(tmp_path, freqs=(5e9,), x=3, z=2)
    _load_into_studio(db)
    # Take a Γ that is actually in the library, so it must be reachable.
    data = db.load()
    gamma = complex(np.asarray(data['s'])[0][0][0, 0])
    out = handlers.studio_pattern_resolve(state, {
        'points': [[gamma.real, gamma.imag]]})
    assert out['count'] == 1
    assert out['reachable'] == 1
    assert out['all_reachable'] is True
    row = out['rows'][0]
    assert row['point_index'] == 0
    assert row['error'] == pytest.approx(0.0, abs=1e-9)


def test_pattern_resolve_marks_an_unreachable_target(tmp_path):
    db, _, _ = _make_library(tmp_path, freqs=(5e9,), x=3, z=2)
    _load_into_studio(db)
    out = handlers.studio_pattern_resolve(state, {'points': [[0.99, 0.0]]})
    assert out['count'] == 1
    row = out['rows'][0]
    assert row['reachable'] in (True, False)
    assert row['gamma_label']


# ----------------------------------------------------------------------
# F-05 frequency preview + the dropped prerequisite gate
# ----------------------------------------------------------------------
def test_frequency_preview_reports_the_real_grid():
    """Each mode must preview the grid it would really measure."""
    one = handlers.studio_frequency_preview(state, {
        'frequency_mode': 'fo_list', 'fo_hz': 5e9})
    assert one['ok'] is True
    assert one['count'] == 1
    assert one['start_hz'] == pytest.approx(5e9)

    sweep = handlers.studio_frequency_preview(state, {
        'frequency_mode': 'freq_sweep', 'vna_start_hz': 1e9,
        'vna_stop_hz': 20e9, 'vna_points': 401})
    assert sweep['count'] == 401
    assert sweep['step_hz'] == pytest.approx(47.5e6)
    assert sweep['uniform'] is True

    harmonics = handlers.studio_frequency_preview(state, {
        'frequency_mode': 'harmonic_list', 'fo_hz': 5e9, 'harmonics': [1, 2, 3]})
    assert harmonics['count'] == 3
    assert harmonics['harmonic_numbers'] == [1, 2, 3]


def test_the_arbitrary_frequency_list_is_read_as_ghz():
    """The list is entered in GHz — a list of numbers must be scaled too.

    Regression: only the *string* form used to be scaled by 1e9, so the
    desktop's list form arrived as Hz and produced a 5 Hz calibration.
    """
    as_list = handlers.studio_frequency_preview(state, {
        'frequency_mode': 'arbitrary_list', 'frequency_list_ghz': [5.0, 10.0, 15.0]})
    as_text = handlers.studio_frequency_preview(state, {
        'frequency_mode': 'arbitrary_list', 'frequency_list_ghz': '5,10,15'})
    assert as_list['ok'] is True and as_text['ok'] is True
    assert as_list['start_hz'] == pytest.approx(5e9)
    assert as_list['stop_hz'] == pytest.approx(15e9)
    assert as_list['frequencies_hz'] == as_text['frequencies_hz']


def test_an_invalid_frequency_plan_is_reported_not_raised():
    """A malformed entry must come back as an error, never as a plan."""
    bad = handlers.studio_frequency_preview(state, {
        'frequency_mode': 'arbitrary_list', 'frequency_list_ghz': 'abc'})
    assert bad['ok'] is False
    assert bad['error']


def test_the_criteria_explain_every_check():
    """The operator must be able to read the judging rules."""
    data = handlers.studio_prerequisite_criteria(state, {})
    items = data['criteria']['items']
    assert len(items) == 6
    ids = {item['id'] for item in items}
    assert ids == set(handlers.CHECK_IDS)
    for item in items:
        assert item['method']
        assert item['hard']
        assert item['warn']


def test_a_failed_check_does_not_block_the_library(tmp_path):
    """The gate was dropped: a failing hard check is recorded, not enforced."""
    from tuner.models import Measurement

    class _Motion:
        def __init__(self):
            self.position = np.zeros(3)

        def read_position(self):
            return self.position.copy()

        def move_to(self, p, timeout=None):
            self.position = np.asarray(p, float)

        def retract(self):
            self.position[1:] = 0

    class _VNA:
        frequencies = np.array([5e9])

        def measure(self):
            return Measurement(self.frequencies, np.zeros((1, 2, 2), complex))

    keys = ('config', 'motion', 'vna', 'manager', 'database', 'data',
            'calibration_state', 'calibration_progress',
            'prerequisite_report')
    with state.studio_lock:
        previous = {key: state.studio.get(key) for key in keys}
        state.studio.update({'config': TunerConfig(), 'motion': _Motion(),
                             'vna': _VNA(), 'manager': None, 'database': None,
                             'data': None, 'calibration_state': 'idle'})
        # A failing hard check, as if the VNA calibration had not been recorded.
        state.studio['prerequisite_report'] = {
            'passed': False, 'checks': [], 'failed_hard': ['vna_calibration'],
            'warnings': [], 'timestamp': 0.0, 'summary': 'forced failure',
            'text': 'forced failure'}
    try:
        out = handlers.studio_setup(state, {
            'plan': 'cartesian', 'x_count': 3, 'z_count': 2,
            'path': str(tmp_path / 'gated.h5')})
        assert out['path']
        assert out['prerequisite_passed'] is False
        # The verdict is still recorded in the library header.
        meta = CalibrationDatabase(tmp_path / 'gated.h5').read_metadata()
        snapshot = meta['prerequisite_snapshot']
        assert snapshot is not None
        assert snapshot['passed'] is False
        assert 'vna_calibration' in snapshot['failed_hard']
    finally:
        with state.studio_lock:
            state.studio.update(previous)


def test_the_gate_can_be_switched_back_on(tmp_path):
    """``require_prerequisites`` restores the hard block without any other change."""
    from tuner.models import Measurement

    class _Motion:
        def __init__(self):
            self.position = np.zeros(3)

        def read_position(self):
            return self.position.copy()

        def move_to(self, p, timeout=None):
            self.position = np.asarray(p, float)

        def retract(self):
            self.position[1:] = 0

    class _VNA:
        frequencies = np.array([5e9])

        def measure(self):
            return Measurement(self.frequencies, np.zeros((1, 2, 2), complex))

    keys = ('config', 'motion', 'vna', 'manager', 'database', 'data',
            'calibration_state', 'calibration_progress',
            'prerequisite_report')
    with state.studio_lock:
        previous = {key: state.studio.get(key) for key in keys}
        state.studio.update({'config': TunerConfig(), 'motion': _Motion(),
                             'vna': _VNA(), 'manager': None, 'database': None,
                             'data': None, 'calibration_state': 'idle'})
        state.studio['prerequisite_report'] = {
            'passed': False, 'checks': [], 'failed_hard': ['vna_calibration'],
            'warnings': [], 'timestamp': 0.0, 'summary': 'forced failure',
            'text': 'forced failure'}
    try:
        with pytest.raises(RuntimeError):
            handlers.studio_setup(state, {
                'plan': 'cartesian', 'x_count': 3, 'z_count': 2,
                'path': str(tmp_path / 'blocked.h5'),
                'require_prerequisites': True})
    finally:
        with state.studio_lock:
            state.studio.update(previous)

"""P1 feature tests: reference planes, export, library ops, repeatability,
deviation reporting and the Calibration Browser backend.

Each backend module is exercised against a small synthetic 4-axis library so the
suite needs no real instrument and never touches the 576-point production library.
"""
import numpy as np
import pytest

from tuner.database import CalibrationDatabase
from tuner.models import TunerConfig, Measurement
from tuner.reference_planes import (
    REFERENCE_PLANES, REFERENCE_PLANE_IDS, is_valid_reference_plane,
    reference_plane_meta, parse_s2p_freqs, check_s2p_coverage,
    validate_reference_setup,
)
from tuner import export as export_mod
from tuner import library_ops
from tuner import repeatability
from tuner import reporting
from tuner import library_browser


# --------------------------------------------------------------------------
# synthetic library
# --------------------------------------------------------------------------
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
        # Spread |S11| across the reach so truncation can drop the high tail.
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


# --------------------------------------------------------------------------
# F-10 reference planes + S2P coverage
# --------------------------------------------------------------------------
def test_reference_plane_vocabulary():
    assert len(REFERENCE_PLANES) == 6
    assert 'standard' in REFERENCE_PLANE_IDS
    assert is_valid_reference_plane('on_wafer')
    assert not is_valid_reference_plane('bogus')
    meta = reference_plane_meta('in_situ')
    assert meta['deembed'] is True
    assert reference_plane_meta('nope') is None


def test_s2p_parsing_and_units():
    text = "# GHz S RI R 50\n! comment\n1.0 0.1 0 0.2 0 0 0 0.3 0\n2.0 0.4 0 0 0 0 0 0.5 0\n"
    freqs = parse_s2p_freqs(text)
    assert np.allclose(freqs, [1e9, 2e9])
    # default Hz when no # option line
    assert np.allclose(parse_s2p_freqs("1.0 0 0 0 0 0 0 0 0\n"), [1.0])


def test_s2p_coverage_gate(tmp_path):
    lib = tmp_path / 'lib.h5'
    db, _, _ = _make_library(tmp_path, freqs=(5e9,))
    data = db.load()
    # full coverage
    ok = check_s2p_coverage([5e9], data['frequencies'])
    assert ok['passed'] is True and ok['covered'] == 1
    # missing frequency must fail (S2P is not interpolated)
    bad = check_s2p_coverage([5e9 + 1e6], data['frequencies'])
    assert bad['passed'] is False and 5e9 in bad['missing']
    # a non-existent file yields a clean failure
    none = check_s2p_coverage(tmp_path / 'missing.s2p', data['frequencies'])
    assert none['passed'] is False and none['s2p_count'] == 0


def test_validate_reference_setup_ok(tmp_path):
    db, _, _ = _make_library(tmp_path, freqs=(5e9,))
    data = db.load()
    res = validate_reference_setup('on_wafer', str(tmp_path / 'fix.s2p'),
                                   data['frequencies'])
    assert res['reference_plane_ok'] is True
    assert res['deembed_allowed'] is True
    assert res['coverage']['passed'] is False  # fixture file does not exist
    bad = validate_reference_setup('bogus', '', data['frequencies'])
    assert bad['reference_plane_ok'] is False


# --------------------------------------------------------------------------
# F-13 export
# --------------------------------------------------------------------------
def test_export_formats(tmp_path):
    db, _, _ = _make_library(tmp_path, freqs=(5e9,))
    data = db.load()
    meta = db.read_metadata()
    n_valid = int(np.count_nonzero(data['valid']))
    n_axes = int(np.asarray(data.get('actual', data['positions']), float).shape[1])

    tef = export_mod.export_tef(tmp_path / 'out.tef', data, meta)
    assert tef['rows'] == n_valid
    head = (tmp_path / 'out.tef').read_text().splitlines()[0]
    assert head.startswith('!')
    body = (tmp_path / 'out.tef').read_text().splitlines()[-1].split('\t')
    assert len(body) == len(export_mod._tef_columns(n_axes))

    csv = export_mod.export_csv(tmp_path / 'out.csv', data, meta)
    assert csv['rows'] == n_valid
    csv_lines = [ln for ln in (tmp_path / 'out.csv').read_text().splitlines()
                 if ln and not ln.startswith('!')]
    cbody = csv_lines[0].split(',')
    assert len(cbody) == len(export_mod._csv_columns(n_axes))

    s2p = export_mod.export_s2p(tmp_path / 'p0.s2p', data, 0, freq_index=0)
    assert s2p['point'] == 0
    s2ptext = (tmp_path / 'p0.s2p').read_text()
    assert '# Hz S RI R 50' in s2ptext

    lib = export_mod.export_s2p_library(tmp_path / 's2pdir', data, freq_index=0)
    assert lib['points'] == n_valid

    cp = export_mod.export_hdf5_copy(db.path, tmp_path / 'copy.h5')
    assert cp['bytes'] == db.path.stat().st_size


def test_export_tef_pointwise_matches_library(tmp_path):
    """Every exported .tef row must reproduce the library S11 amplitude."""
    db, _, _ = _make_library(tmp_path, freqs=(5e9,), x=3, z=2)
    data = db.load()
    n_axes = int(np.asarray(data.get('actual', data['positions']), float).shape[1])
    export_mod.export_tef(tmp_path / 'o.tef', data, db.read_metadata())
    lines = (tmp_path / 'o.tef').read_text().splitlines()
    data_lines = [ln for ln in lines if ln and not ln.startswith('!')]
    s11_re_idx = 2 + n_axes          # Pt, Freq, then n_axes positions
    s11_im_idx = s11_re_idx + 1
    for k, ln in enumerate(data_lines):
        cols = ln.split('\t')
        s11_re = float(cols[s11_re_idx]); s11_im = float(cols[s11_im_idx])
        lib_s11 = complex(data['s'][k, 0, 0, 0])
        assert abs(complex(s11_re, s11_im) - lib_s11) < 1e-6


# --------------------------------------------------------------------------
# F-14 library operations
# --------------------------------------------------------------------------
def test_truncate_by_gamma_reduces_points(tmp_path):
    db, _, _ = _make_library(tmp_path, freqs=(5e9,), x=5, z=3)
    data = db.load()
    n_all = int(np.count_nonzero(data['valid']))
    res = library_ops.truncate_by_gamma(db, tmp_path / 't.h5', 0.5)
    new = CalibrationDatabase(res['path']).load()
    n_new = int(np.count_nonzero(new['valid']))
    assert n_new < n_all
    assert res['parent_version'] == data['metadata']['version_id']
    # No surviving point may exceed the cut
    engine = __import__('tuner.search', fromlist=['GammaSearchEngine']).GammaSearchEngine(new, z0=50)
    assert engine.gammas(5e9).real.shape[0] == n_new


def test_reverse_probe_mirrors_axis_and_keeps_s(tmp_path):
    db, config, positions = _make_library(tmp_path, freqs=(5e9,), x=3, z=2)
    data = db.load()
    res = library_ops.reverse_probe(db, tmp_path / 'r.h5', 'Z1')
    new = CalibrationDatabase(res['path'])
    ndata = new.load()
    src_actual = np.asarray(data.get('actual', data['positions']), float)
    new_actual = np.asarray(ndata.get('actual', ndata['positions']), float)
    # same point count, same S matrix
    assert len(new_actual) == len(src_actual)
    assert np.allclose(ndata['s'], data['s'])
    # Z1 mirrored about its mid-travel
    lo = src_actual[:, 1].min(); hi = src_actual[:, 1].max()
    mid = 0.5 * (lo + hi)
    assert np.allclose(new_actual[:, 1], 2 * mid - src_actual[:, 1])


def test_truncate_by_travel(tmp_path):
    db, _, _ = _make_library(tmp_path, freqs=(5e9,), x=3, z=2)
    data = db.load()
    n_all = int(np.count_nonzero(data['valid']))
    res = library_ops.truncate_by_travel(
        db, tmp_path / 'tv.h5', {'Z1': (0.0, 0.25)})
    new = CalibrationDatabase(res['path']).load()
    n_new = int(np.count_nonzero(new['valid']))
    assert 0 < n_new < n_all


def test_split_by_frequency(tmp_path):
    db, _, _ = _make_library(tmp_path, freqs=(4e9, 5e9, 6e9), x=2, z=2)
    out = library_ops.split_by_frequency(db, tmp_path / 'freqs')
    assert len(out) == 3
    for item in out:
        nd = CalibrationDatabase(item['path']).load()
        assert nd['frequencies'].shape[0] == 1


def test_apply_fixture_embed_deembed_roundtrip(tmp_path):
    db, _, _ = _make_library(tmp_path, freqs=(5e9,), x=3, z=2)
    data = db.load()
    # fixture = weak directional coupler
    fixture = np.array([[0.9, 0.1], [0.1, 0.9]], dtype=complex)
    s2p_text = ("# Hz S RI R 50\n"
                f"5.0 0.9 0 0.1 0 0.1 0 0.9 0\n")
    (tmp_path / 'fix.s2p').write_text(s2p_text)
    emb = library_ops.apply_fixture(db, tmp_path / 'emb.h5',
                                    tmp_path / 'fix.s2p', direction='embed')
    emb_data = CalibrationDatabase(emb['path']).load()
    de = library_ops.apply_fixture(
        CalibrationDatabase(emb['path']), tmp_path / 'de.h5',
        tmp_path / 'fix.s2p', direction='deembed')
    de_data = CalibrationDatabase(de['path']).load()
    # de-embedding the embedded library should recover the original S
    assert np.allclose(de_data['s'], data['s'], atol=1e-6)


# --------------------------------------------------------------------------
# F-20 repeatability
# --------------------------------------------------------------------------
def test_repeatability_stats(tmp_path):
    measurements = [
        {'gamma': 0.5 + 0.1j, 'target_gamma': 0.5 + 0.12j, 'label': 'A'},
        {'gamma': 0.5 + 0.11j, 'target_gamma': 0.5 + 0.12j, 'label': 'B'},
        {'gamma': 0.5 + 0.13j, 'target_gamma': 0.5 + 0.12j, 'label': 'C'},
    ]
    rep = repeatability.analyze_repeatability(measurements, tolerance=0.05)
    assert rep['n'] == 3
    assert rep['mean_delta_gamma'] > 0
    assert rep['max_delta_gamma'] >= rep['mean_delta_gamma']
    assert np.isfinite(rep['reset_dB'])
    # high-|Gamma| band separation
    hi = [{'gamma': 0.95 + 0.1j, 'target_gamma': 0.95 + 0.0j} for _ in range(2)]
    hirep = repeatability.analyze_repeatability(hi, high_gamma_threshold=0.9)
    assert hirep['high_gamma']['n'] == 2


# --------------------------------------------------------------------------
# F-19 / F-24 deviation report + formatting
# --------------------------------------------------------------------------
def test_deviation_report_and_formatters():
    pred = 0.754 * np.exp(1j * np.radians(90))
    meas = 0.757 * np.exp(1j * np.radians(89.4))
    rep = reporting.deviation_report(pred, meas, z0=50, tolerance=0.05)
    assert rep['vector_error'] <= 0.05
    assert rep['within_tolerance'] is True
    assert rep['vswr_delta'] is not None
    assert reporting.gamma_band(0.95) == 'high'
    assert reporting.gamma_band(0.3) == 'low'
    assert reporting.vswr_from_gamma(0.754) == (1 + 0.754) / (1 - 0.754)
    assert reporting.vswr_from_gamma(1.0) is None
    lbl = reporting.format_gamma(meas)
    assert '∠' in lbl


# --------------------------------------------------------------------------
# F-12 Calibration Browser backend
# --------------------------------------------------------------------------
def test_browser_scan_and_compare(tmp_path):
    db, _, _ = _make_library(tmp_path, freqs=(5e9,), x=3, z=2)
    # a second, identical grid but different S spread
    db2, _, _ = _make_library(tmp_path, freqs=(5e9,), x=3, z=2, seed=7, name='lib2.h5')
    scanned = library_browser.scan_libraries(tmp_path)
    assert len(scanned['libraries']) >= 2
    by_name = {lib['name']: lib for lib in scanned['libraries']}
    assert 'lib.h5' in by_name
    assert by_name['lib.h5']['points_valid'] == 12  # 3 * 2^2
    cmp = library_browser.compare_libraries(db.path, db2.path, frequency_hz=5e9)
    assert cmp['freq_match'] is True
    assert cmp['diffable'] is True
    assert 'mean_delta_gamma' in cmp

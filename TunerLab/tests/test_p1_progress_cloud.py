"""P1 progress-cloud tests (F-11).

The point cloud is the operator's view of a running acquisition, so these
tests check the *guarantee* it rests on: a point counts as done only after it
has been written to the library.  A measured-but-not-committed point is still
pending.
"""
import numpy as np
import pytest

from tuner.database import CalibrationDatabase
from tuner.models import TunerConfig, Measurement
from tuner.progress_cloud import layer_of_point, progress_cloud


def _make_library(tmp_path, freqs=(5e9,), x=3, z=2, name='cloud.h5'):
    """A library where only the first ``done_count`` points are written."""
    config = TunerConfig()
    xv = np.linspace(config.x_limits[0], config.x_limits[1], x)
    zv = np.linspace(config.z_limits[0], config.z_limits[1], z)
    positions = config.grid(xv, zv)
    db = CalibrationDatabase(tmp_path / name)
    db.create(config, np.array(freqs, float), positions)
    return db, config, positions


def _write(db, index, positions, freqs, gamma):
    s = np.zeros((len(freqs), 2, 2), complex)
    s[:, 0, 0] = gamma
    db.write_point(index, positions[index].copy(),
                   Measurement(np.array(freqs, float), s, True))


def test_layer_grouping_follows_the_grid(tmp_path):
    """One layer per distinct X; rows are the Z combinations inside it."""
    db, config, positions = _make_library(tmp_path)
    data = db.load()
    layers = layer_of_point(data)
    # TunerConfig() defaults to 2 probes -> X/Z1/Z2 -> x * z^2 points.
    assert len(positions) == 3 * 2 ** 2
    assert len(layers) == 3                      # three distinct X values
    assert all(layer['total'] == 2 ** 2 for layer in layers)
    # X values increase monotonically from left to right.
    assert [layer['x'] for layer in layers] == sorted(layer['x'] for layer in layers)


def test_points_are_pending_until_written(tmp_path):
    db, config, positions = _make_library(tmp_path)
    freqs = (5e9,)
    _write(db, 0, positions, freqs, 0.2 + 0j)
    _write(db, 1, positions, freqs, 0.3 + 0j)
    data = db.load()
    cloud = progress_cloud(data, data.get('errors'))
    assert cloud['total'] == len(positions)
    assert cloud['done'] == 2
    assert cloud['pending'] == len(positions) - 2
    assert 0.0 < cloud['percent'] < 100.0
    assert cloud['complete_layers'] == 0        # no layer is fully written yet


def test_a_layer_completes_when_all_its_points_are_written(tmp_path):
    db, config, positions = _make_library(tmp_path)
    freqs = (5e9,)
    for index in range(4):                      # first X layer = 4 points
        _write(db, index, positions, freqs, 0.2 + 0j)
    data = db.load()
    cloud = progress_cloud(data, data.get('errors'))
    assert cloud['done'] == 4
    assert cloud['complete_layers'] == 1
    first = cloud['layers'][0]
    assert first['complete'] is True
    assert first['done_rows'] == [True] * 4
    assert first['pending'] == 0


def test_failed_points_are_reported(tmp_path):
    db, config, positions = _make_library(tmp_path)
    freqs = (5e9,)
    for index in range(len(positions)):
        _write(db, index, positions, freqs, 0.2 + 0j)
    db.record_failure(0, 'VNA timeout')
    data = db.load()
    cloud = progress_cloud(data, data.get('errors'))
    assert cloud['done'] == len(positions) - 1
    assert len(cloud['failed']) == 1
    assert 'VNA timeout' in cloud['failed'][0]['error']
    assert cloud['failed'][0]['index'] == 0


def test_empty_library_does_not_crash():
    cloud = progress_cloud({'positions': np.zeros((0, 3)), 'valid': np.zeros(0, bool)})
    assert cloud['total'] == 0
    assert cloud['percent'] == 0.0
    assert cloud['layers'] == []


def test_mismatched_valid_length_is_tolerated():
    """A truncated ``valid`` array must not crash the cloud."""
    cloud = progress_cloud({'positions': np.zeros((5, 3)),
                            'valid': np.ones(2, bool)})
    assert cloud['total'] == 5
    assert cloud['done'] == 0

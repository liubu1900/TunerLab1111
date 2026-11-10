"""P2 audit trail tests.

The audit is the evidence behind "what did this machine actually measure", so
the tests check that it reads the libraries correctly and that a broken file is
reported rather than hidden.
"""
import json

import numpy as np
import pytest

from tuner.audit import audit_libraries, audit_report, read_operation_log
from tuner.database import CalibrationDatabase
from tuner.models import TunerConfig, Measurement


def _make_library(tmp_path, name='cal.h5', freqs=(5e9,), x=3, z=2, valid=12):
    config = TunerConfig()
    xv = np.linspace(config.x_limits[0], config.x_limits[1], x)
    zv = np.linspace(config.z_limits[0], config.z_limits[1], z)
    positions = config.grid(xv, zv)
    db = CalibrationDatabase(tmp_path / name)
    db.create(config, np.array(freqs, float), positions)
    rng = np.random.default_rng(0)
    for index in range(valid):
        mag = 0.1 + 0.8 * (index / max(1, len(positions) - 1))
        angle = rng.uniform(-np.pi, np.pi)
        g = mag * np.exp(1j * angle)
        s = np.zeros((len(freqs), 2, 2), complex)
        s[:, 0, 0] = g
        s[:, 1, 0] = 0.05 * np.exp(1j * angle)
        s[:, 0, 1] = s[:, 1, 0]
        db.write_point(index, positions[index].copy(),
                       Measurement(np.array(freqs, float), s, True))
    return db


def test_a_library_is_audited_with_its_header_and_hash(tmp_path):
    _make_library(tmp_path)
    result = audit_libraries(tmp_path)
    assert len(result['libraries']) == 1
    row = result['libraries'][0]
    assert row['name'] == 'cal.h5'
    assert row['points_valid'] == 12
    assert row['points_total'] == 12
    assert row['points_percent'] == pytest.approx(100.0)
    assert row['cal_id']
    assert row['file_sha256'] and len(row['file_sha256']) == 64   # SHA-256
    assert row['frequency_ghz'] == pytest.approx([5.0])


def test_a_partly_measured_library_is_counted(tmp_path):
    _make_library(tmp_path, valid=5)
    row = audit_libraries(tmp_path)['libraries'][0]
    assert row['points_valid'] == 5
    assert row['points_total'] == 12
    assert row['points_percent'] == pytest.approx(100.0 * 5 / 12)


def test_a_validation_record_is_picked_up(tmp_path):
    db = _make_library(tmp_path)
    db.save_validation({'kind': 'interpolation', 'passed': True, 'n': 5})
    db.save_validation({'kind': 'repeatability_auto', 'passed': False})
    row = audit_libraries(tmp_path)['libraries'][0]
    assert row['validation_count'] == 2
    assert 'interpolation' in row['validation_kinds']


def test_a_broken_file_is_reported_not_hidden(tmp_path):
    _make_library(tmp_path, name='good.h5')
    (tmp_path / 'broken.h5').write_bytes(b'not an hdf5 file')
    result = audit_libraries(tmp_path)
    assert len(result['libraries']) == 1
    assert len(result['errors']) == 1
    assert result['errors'][0]['path'].endswith('broken.h5')


def test_a_missing_directory_is_not_an_error(tmp_path):
    result = audit_libraries(tmp_path / 'nope')
    assert result['libraries'] == []
    assert result['errors'] == []


def test_the_operation_log_is_summarised(tmp_path):
    log = tmp_path / 'operations.jsonl'
    rows = [
        {'seq': 1, 'timestamp': '2026-11-08T10:00:00', 'time_text': '10:00:00',
         'category': '通讯', 'action': '连接控制器', 'detail': 'ok', 'level': 'info'},
        {'seq': 2, 'timestamp': '2026-11-08T10:01:00', 'time_text': '10:01:00',
         'category': '运动', 'action': '绝对定位', 'detail': 'X → 0',
         'level': 'error'},
        {'seq': 3, 'timestamp': '2026-11-08T10:02:00', 'time_text': '10:02:00',
         'category': '校准', 'action': '建立校准库', 'detail': '576 点',
         'level': 'info'},
    ]
    log.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    summary = read_operation_log(log)
    assert summary['entries'] == 3
    assert summary['by_category']['通讯'] == 1
    assert summary['by_level']['error'] == 1
    assert len(summary['faults']) == 1
    assert summary['faults'][0]['action'] == '绝对定位'
    assert len(summary['calibrations']) == 1
    assert summary['first'] == '10:00:00'
    assert summary['last'] == '10:02:00'


def test_a_malformed_log_line_is_counted_not_fatal(tmp_path):
    log = tmp_path / 'operations.jsonl'
    log.write_text('{"category": "x", "action": "y", "level": "info"}\n'
                   'not json\n', encoding='utf-8')
    summary = read_operation_log(log)
    assert summary['entries'] == 1
    assert summary['malformed'] == 1


def test_a_missing_log_file_is_reported(tmp_path):
    summary = read_operation_log(tmp_path / 'nope.jsonl')
    assert summary['exists'] is False
    assert summary['entries'] == 0


def test_the_whole_report_gathers_every_source(tmp_path):
    db = _make_library(tmp_path)
    db.save_validation({'kind': 'interpolation', 'passed': True})
    log = tmp_path / 'operations.jsonl'
    log.write_text(json.dumps({'category': '校准', 'action': '建立校准库',
                               'level': 'info', 'time_text': '10:00:00'}),
                   encoding='utf-8')
    report = audit_report(tmp_path, log)
    assert report['library_count'] == 1
    assert report['validation_count'] == 1
    assert report['total_points'] == 12
    assert report['log']['entries'] == 1

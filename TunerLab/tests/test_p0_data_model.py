"""P0 data-layer tests: schema-v2 header, per-point records, VNA cal status.

All HDF5 fixtures are built under ``tmp_path``; the real calibration library is
never touched.  Legacy libraries are synthesised with the *old* on-disk shape
(no optional point-record datasets, ``schema_version == 1``) so safe-degradation
and backward compatibility can be asserted.
"""
import hashlib
import json
import time
from unittest import mock

import h5py
import numpy as np
import pytest

from tuner import vna_calibration as vc
from tuner.database import CalibrationDatabase
from tuner.metadata import (
    DEFAULT_METADATA,
    METADATA_FIELDS,
    SCHEMA_VERSION,
    compute_file_sha256,
    new_calibration_metadata,
    normalise_metadata,
)
from tuner.models import Measurement, TunerConfig, comparable_config_dict


@pytest.fixture(autouse=True)
def _reset_vna_calibration():
    """Keep the process-wide calibration state isolated between tests."""
    vc.clear()
    yield
    vc.clear()


# --------------------------------------------------------------------------- #
# Legacy (schema v1) HDF5 fixture
# --------------------------------------------------------------------------- #
def _make_legacy_library(path):
    """Write a v1 library with the classic datasets only (no F-02 datasets)."""
    config = TunerConfig()
    f = np.array([1e9, 2e9], dtype=float)
    p = np.array([[0., 0., 0.], [1., .5, .5]], dtype=float)
    s = np.zeros((len(p), len(f), 2, 2), dtype=complex)
    s[:, 1, 0] = 0.5
    valid = np.array([True, False], dtype=bool)
    meta = {
        'config': comparable_config_dict(config),
        'simulated': True,
        'schema_version': 1,
        'version_id': 'legacy-version-id',
        'created': 1234567890.0,
        'source': 'legacy',
    }
    with h5py.File(path, 'w') as h:
        h.attrs['metadata'] = json.dumps(meta)
        h.create_dataset('frequencies', data=f)
        h.create_dataset('positions', data=p)
        h.create_dataset('actual', data=p.copy())
        h.create_dataset('s', data=s)
        h.create_dataset('valid', data=valid)
        h.create_dataset(
            'errors', data=np.array(['', 'oops'], dtype=h5py.string_dtype()))
        h.create_group('validation')
    return meta, f, p, s, valid


# --------------------------------------------------------------------------- #
# F-01: schema-v2 header
# --------------------------------------------------------------------------- #
def test_normalise_metadata_keeps_legacy_values():
    legacy = {
        'schema_version': 1,
        'config': {'probes': 2},
        'simulated': True,
        'version_id': 'legacy-version-id',
        'created': 123.0,
        'source': 'legacy',
    }
    out = normalise_metadata(legacy)
    # Legacy values must NOT be rewritten to v2.
    assert out['schema_version'] == 1
    assert out['version_id'] == 'legacy-version-id'
    assert out['created'] == 123.0
    assert out['simulated'] is True
    assert out['config'] == {'probes': 2}
    assert out['source'] == 'legacy'          # extra keys preserved
    assert out is not legacy                  # returns a copy
    for field in METADATA_FIELDS:             # missing v2 fields filled
        assert field in out
    assert out['sha256'] == ''
    assert isinstance(out['vna_cal'], dict)
    assert isinstance(out['frequency_list'], list)
    assert isinstance(out['harmonics'], list)
    # Mutable defaults are independent, not shared with DEFAULT_METADATA.
    out['vna_cal']['mutated'] = 1
    out['frequency_list'].append(99)
    assert 'mutated' not in DEFAULT_METADATA['vna_cal']
    assert DEFAULT_METADATA['frequency_list'] == []


def test_new_calibration_metadata_overrides_and_fresh_ids():
    a = new_calibration_metadata(
        {'probes': 2}, True,
        point_plan='cartesian', interpolation_mode='linear',
        reference_plane='probe tip')
    assert a['schema_version'] == SCHEMA_VERSION == 2
    assert a['point_plan'] == 'cartesian'
    assert a['interpolation_mode'] == 'linear'
    assert a['reference_plane'] == 'probe tip'
    assert a['config'] == {'probes': 2}
    assert a['simulated'] is True
    assert a['cal_id'].startswith('tunerlab-')
    for field in METADATA_FIELDS:
        assert field in a
    b = new_calibration_metadata({}, False)
    assert b['version_id'] != a['version_id']
    assert b['simulated'] is False


def test_compute_file_sha256(tmp_path):
    sample = tmp_path / 'x.bin'
    sample.write_bytes(b'tunerlab')
    assert compute_file_sha256(sample) == hashlib.sha256(b'tunerlab').hexdigest()
    assert compute_file_sha256(tmp_path / 'missing.bin') == ''


def test_new_library_header_is_v2(tmp_path):
    db = CalibrationDatabase(tmp_path / 'new.h5')
    db.create(TunerConfig(), [1e9, 2e9], [[0, 0, 0], [1, .5, .5]])
    loaded = db.load()
    # load() top-level key set is unchanged.
    assert set(loaded) == {'frequencies', 'positions', 'actual', 's',
                           'valid', 'errors', 'metadata', 'validation'}
    meta = loaded['metadata']
    assert meta['schema_version'] == SCHEMA_VERSION == 2
    for field in METADATA_FIELDS:
        assert field in meta
    assert meta['sha256'] == ''
    assert isinstance(meta['vna_cal'], dict)
    assert isinstance(meta['frequency_list'], list)
    assert isinstance(meta['harmonics'], list)
    assert 'independent_axes' not in meta['config']  # runtime policy excluded
    # read_metadata() agrees with load() and does not lose any key.
    rm = db.read_metadata()
    for key, value in meta.items():
        assert rm[key] == value


# --------------------------------------------------------------------------- #
# F-02: per-point records
# --------------------------------------------------------------------------- #
def test_write_point_records_values(tmp_path):
    db = CalibrationDatabase(tmp_path / 'rec.h5')
    db.create(TunerConfig(), [1e9, 2e9],
              [[0, 0, 0], [1, .5, .5], [2, .3, .3]])
    f = np.array([1e9, 2e9])

    # Point 0 supplies values through the dataclass 'metadata' dict.
    m0 = Measurement(f, np.zeros((2, 2, 2), complex), True,
                     metadata={'probe_idx': 7, 'loss_db': -3.5, 'attempts': 2})
    db.write_point(0, [0, 0, 0], m0)

    # Point 1 uses a dynamically attached 'info' dict (task wording) and omits
    # loss_db so it is derived safely from |S21|.
    s1 = np.zeros((2, 2, 2), complex)
    s1[:, 1, 0] = 0.5
    m1 = Measurement(f, s1, True)
    m1.info = {'probe_idx': 9, 'attempts': 4}
    db.write_point(1, [1, .5, .5], m1)

    rec = db.read_point_records()
    assert set(rec) == {'probe_idx', 'loss_db', 'timestamp', 'attempts'}
    assert all(isinstance(v, np.ndarray) for v in rec.values())
    assert all(len(v) == 3 for v in rec.values())
    assert rec['probe_idx'][0] == 7
    assert rec['loss_db'][0] == pytest.approx(-3.5)
    assert rec['attempts'][0] == 2
    assert np.isfinite(rec['timestamp'][0])
    assert rec['probe_idx'][1] == 9
    assert rec['attempts'][1] == 4
    assert np.isfinite(rec['loss_db'][1])       # derived from |S21|
    assert np.isfinite(rec['timestamp'][1])
    # Untouched point keeps defaults.
    assert rec['probe_idx'][2] == -1
    assert rec['attempts'][2] == 0
    assert np.isnan(rec['loss_db'][2])
    assert np.isnan(rec['timestamp'][2])


def test_append_positions_extends_point_records(tmp_path):
    db = CalibrationDatabase(tmp_path / 'app.h5')
    db.create(TunerConfig(), [1e9], [[0, 0, 0]])
    f = np.array([1e9])
    db.write_point(0, [0, 0, 0],
                   Measurement(f, np.zeros((1, 2, 2), complex), True,
                               metadata={'probe_idx': 3}))
    db.append_positions([[1, .5, .5], [2, .3, .3]])
    rec = db.read_point_records()
    assert all(len(v) == 3 for v in rec.values())
    assert rec['probe_idx'][0] == 3
    assert rec['probe_idx'][1] == -1
    assert rec['probe_idx'][2] == -1
    assert db.load()['positions'].shape[0] == 3


def test_legacy_library_load_and_records_are_safe(tmp_path):
    path = tmp_path / 'legacy.h5'
    meta, f, p, s, valid = _make_legacy_library(path)
    db = CalibrationDatabase(path)

    loaded = db.load()
    assert set(loaded) == {'frequencies', 'positions', 'actual', 's',
                           'valid', 'errors', 'metadata', 'validation'}
    # Main data unchanged and unchanged numeric values.
    assert np.array_equal(loaded['frequencies'], f)
    assert np.array_equal(loaded['positions'], p)
    assert np.array_equal(loaded['s'], s)
    assert np.array_equal(loaded['valid'], valid)
    assert loaded['metadata']['schema_version'] == 1
    assert loaded['metadata']['version_id'] == 'legacy-version-id'
    assert loaded['metadata']['simulated'] is True

    # No point-record datasets -> safe defaults of the correct length.
    rec = db.read_point_records()
    n = len(valid)
    assert all(len(v) == n for v in rec.values())
    assert np.all(rec['probe_idx'] == -1)
    assert np.all(rec['attempts'] == 0)
    assert np.all(np.isnan(rec['loss_db']))
    assert np.all(np.isnan(rec['timestamp']))

    # read_metadata fills v2 fields but keeps legacy values and never writes to
    # the file.
    rm = db.read_metadata()
    assert rm['schema_version'] == 1
    assert rm['version_id'] == 'legacy-version-id'
    assert 'cal_id' in rm
    assert 'cal_id' not in db.load()['metadata']  # file untouched


def test_fork_version_works_on_legacy_library(tmp_path):
    path = tmp_path / 'legacy.h5'
    _make_legacy_library(path)
    db = CalibrationDatabase(path)
    fork = db.fork_version(tmp_path / 'fork.h5', 'verification campaign')
    a = db.load()['metadata']
    b = fork.load()['metadata']
    assert a['version_id'] != b['version_id']
    assert b['parent_version'] == a['version_id']
    assert b['schema_version'] == SCHEMA_VERSION
    rec = fork.read_point_records()
    assert all(len(v) == 2 for v in rec.values())
    assert np.array_equal(fork.load()['frequencies'], db.load()['frequencies'])


# --------------------------------------------------------------------------- #
# F-04: VNA calibration state
# --------------------------------------------------------------------------- #
def test_vna_calibration_state_lifecycle():
    # pristine
    assert vc.get_state().verified is False
    assert vc.is_valid() is False
    assert vc.age_days() is None
    r = vc.status_report()
    assert r['verified'] is False and r['valid'] is False
    assert r['reason'] == '无 VNA 校准记录'

    state = vc.set_state(method='SOLT', cal_kit='85052D', timestamp=time.time(),
                         reference_plane='probe tips', verified=True, note='ok')
    assert state.verified is True
    assert vc.is_valid() is True
    assert vc.get_state().method == 'SOLT'
    # get_state returns a copy
    got = vc.get_state()
    got.method = 'MUTATED'
    assert vc.get_state().method == 'SOLT'
    assert vc.as_dict()['cal_kit'] == '85052D'
    assert set(vc.status_report()) == {'verified', 'valid', 'method', 'cal_kit',
                                       'timestamp', 'age_days',
                                       'reference_plane', 'reason'}

    with pytest.raises(ValueError):
        vc.set_state(bogus_field=1)

    # expired: 10 days old
    vc.set_state(timestamp=time.time() - 10 * 86400)
    assert vc.is_valid() is False
    assert vc.is_valid(max_age_days=30) is True
    assert '校准已超过 7 天' in vc.status_report()['reason']

    vc.clear()
    assert vc.get_state().verified is False
    assert vc.is_valid() is False
    assert vc.age_days() is None


def test_validate_via_scpi_is_read_only():
    assert vc.validate_via_scpi(None)['queried'] is False

    class NoQuery:
        pass

    assert vc.validate_via_scpi(NoQuery())['queried'] is False

    class Exploding:
        def query(self, cmd):
            raise RuntimeError('boom')

    assert vc.validate_via_scpi(Exploding())['queried'] is False

    class Fake:
        def __init__(self):
            self.cmds = []

        def query(self, cmd):
            assert cmd.endswith('?'), 'only read-only queries are allowed'
            self.cmds.append(cmd)
            return '1'

    fake = Fake()
    result = vc.validate_via_scpi(fake)
    assert result['queried'] is True
    assert fake.cmds and all(c.endswith('?') for c in fake.cmds)


def test_visa_measurement_stamps_real_calibration_state():
    from tuner.vna import PyVISAVNAController, VISAProfile

    vc.set_state(method='TRL', timestamp=time.time(), verified=True)

    class Instrument:
        def write(self, cmd):
            pass

        def query(self, cmd):
            return '1'

        def query_ascii_values(self, cmd):
            if cmd == 'freq':
                return [1e9, 2e9]
            return [1, .1, 1, .2]

        def close(self):
            pass

    class Manager:
        def open_resource(self, resource):
            return Instrument()

    profile = VISAProfile('USER_RESOURCE', 1000, 'freq', ('trigger',), 'done',
                          dict(S11='t1', S21='t2', S12='t3', S22='t4'))
    vna = PyVISAVNAController(profile, enabled=True, resource_manager=Manager())
    vna.initialize()
    m = vna.measure()
    assert m.metadata['source'] == 'physical_vna'
    assert m.metadata['resource'] == 'USER_RESOURCE'
    assert m.metadata['calibration_validated'] is True
    assert m.metadata['calibration_method'] == 'TRL'
    assert m.metadata['calibration_valid'] is True
    assert 'calibration_timestamp' in m.metadata

    vc.clear()
    m2 = vna.measure()
    assert m2.metadata['calibration_validated'] is False
    assert m2.metadata['calibration_valid'] is False


def test_tcpvna_measurement_stamps_real_calibration_state():
    from tuner import vna_drivers as V

    class FakeSocket:
        def __init__(self, n_points):
            self.n_points = n_points
            self.last_cmd = ''

        def settimeout(self, t):
            pass

        def shutdown(self, flag):
            pass

        def close(self):
            pass

        def sendall(self, data):
            self.last_cmd = data.decode('ascii').strip()

        def recv(self, n):
            cmd = self.last_cmd
            if cmd.startswith(':CALC1:DATA?'):
                vals = []
                for i in range(self.n_points):
                    vals.extend([f'{0.1 + i * 0.01:.6f}', f'{0.02 * i:.6f}'])
                return (','.join(vals) + '\n').encode('ascii')
            if cmd.endswith('?'):
                if cmd == '*IDN?':
                    return b'Keysight Technologies,E5071C,SN123,A.01\n'
                if cmd == ':SYST:ERR?':
                    return b'+0,"No error"\n'
                if cmd.endswith('*OPC?'):
                    return b'1\n'
            return b''

    fake = FakeSocket(n_points=4)
    with mock.patch.object(V.socket, 'create_connection', return_value=fake):
        vna = V.create_vna('auto', '192.168.0.99', 5025, 5.0)
        vna.connect()
        vna.set_sweep(1e9, 2e9, 4)

    vc.set_state(method='SOLT', cal_kit='kit', timestamp=time.time(),
                 reference_plane='probe tip', verified=True)
    m = vna.measure()
    # original keys preserved
    assert m.metadata['idn'] == 'Keysight Technologies,E5071C,SN123,A.01'
    assert m.metadata['resource'] == '192.168.0.99:5025'
    assert m.metadata['vendor'] == 'keysight_ena'
    assert m.metadata['source'] == 'physical_vna'
    assert m.metadata['calibration_validated'] is True
    assert m.metadata['calibration_method'] == 'SOLT'
    assert m.metadata['calibration_valid'] is True
    assert 'calibration_timestamp' in m.metadata

    vc.clear()
    m2 = vna.measure()
    assert m2.metadata['calibration_validated'] is False

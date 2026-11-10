import numpy as np
import pytest
from tuner.database import CalibrationDatabase
from tuner.models import TunerConfig, Measurement


def example(tmp_path):
    db=CalibrationDatabase(tmp_path/'test.h5'); c=TunerConfig()
    db.create(c,[1e9,2e9],[[0,0,0],[1,.5,.5]])
    return db


def test_roundtrip_checkpoint_and_validation(tmp_path):
    db=example(tmp_path)
    m=Measurement(np.array([1e9,2e9]),np.zeros((2,2,2),complex))
    db.write_point(0,[0,0,0],m)
    db.record_failure(1,'timeout')
    db.save_validation({'simulated':True,'error':.02})
    loaded=CalibrationDatabase(db.path).load()
    assert db.pending_indices()==[1]
    assert loaded['s'].shape==(2,2,2,2)
    assert loaded['metadata']['simulated'] is True
    assert loaded['validation']
    assert db.task_status()[1][1]=='failed'
    with pytest.raises(FileExistsError):
        db.create(TunerConfig(),[1e9],[[0,0,0]])


def test_single_frequency_library_roundtrip(tmp_path):
    """A one-frequency grid must survive create -> write -> load unchanged."""
    db = CalibrationDatabase(tmp_path / 'single.h5')
    db.create(TunerConfig(), [10e9], [[0, 0, 0], [1, .5, .5]])
    db.write_point(0, [0, 0, 0],
                   Measurement(np.array([10e9]), np.zeros((1, 2, 2), complex)))
    loaded = CalibrationDatabase(db.path).load()
    assert loaded['frequencies'].tolist() == [10e9]
    assert loaded['s'].shape == (2, 1, 2, 2)
    assert db.pending_indices() == [1]


def test_reject_bad_measurement(tmp_path):
    db=example(tmp_path)
    with pytest.raises(ValueError,match='provenance'):
        db.write_point(0,[0,0,0],Measurement(np.array([1e9,2e9]),np.zeros((2,2,2)),False))
    with pytest.raises(ValueError,match='frequency'):
        db.write_point(0,[0,0,0],Measurement(np.array([1e9,3e9]),np.zeros((2,2,2))))
    assert db.pending_indices()==[0,1]


def test_version_lineage_preserves_source(tmp_path):
    db=example(tmp_path)
    fork=db.fork_version(tmp_path/'v2.h5','new verification campaign')
    a=db.load()['metadata']; b=fork.load()['metadata']
    assert a['version_id'] != b['version_id']
    assert b['parent_version']==a['version_id']
    assert fork.pending_indices()==[0,1]

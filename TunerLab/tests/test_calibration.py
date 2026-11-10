import numpy as np
from tuner.models import TunerConfig, Measurement
from tuner.database import CalibrationDatabase
from tuner.calibration import CalibrationManager

class Motion:
    def __init__(self): self.position=np.zeros(3)
    def read_position(self): return self.position.copy()
    def move_to(self,p,timeout=None): self.position=np.array(p)
    def retract(self): self.position[1:]=0

class VNA:
    frequencies=np.array([1e9,2e9])
    def __init__(self): self.calls=0
    def measure(self):
        self.calls+=1
        if self.calls==1: raise TimeoutError('transient')
        return Measurement(self.frequencies,np.zeros((2,2,2),complex))


class SingleFrequencyVNA:
    frequencies = np.array([10e9])

    def __init__(self): self.calls = 0

    def measure(self):
        self.calls += 1
        return Measurement(self.frequencies, np.zeros((1, 2, 2), complex))


def test_single_frequency_calibration_runs(tmp_path):
    """End-to-end acquisition with exactly one frequency in the library."""
    config = TunerConfig(); motion = Motion(); vna = SingleFrequencyVNA()
    db = CalibrationDatabase(tmp_path / 'single.h5')
    manager = CalibrationManager(config, motion, vna, db)
    manager.prepare([[0, 0, 0], [1, .2, .3]])
    assert manager.run() == 'completed'
    loaded = db.load()
    assert loaded['frequencies'].tolist() == [10e9]
    assert loaded['s'].shape == (2, 1, 2, 2)
    assert loaded['valid'].all()


def test_pause_resume_retry_and_restart(tmp_path):
    config=TunerConfig(); motion=Motion(); vna=VNA(); db=CalibrationDatabase(tmp_path/'run.h5')
    manager=CalibrationManager(config,motion,vna,db)
    manager.prepare([[0,0,0],[1,.2,.3]])
    manager.pause()
    assert manager.run()=='paused'
    assert db.pending_indices()==[0,1]
    manager.resume()
    def progress(i,total): manager.pause()
    assert manager.run(progress)=='paused'
    assert db.pending_indices()==[1]
    resumed=CalibrationManager(config,motion,vna,CalibrationDatabase(db.path))
    assert resumed.run()=='completed'
    assert not db.pending_indices()
    assert db.task_status()[0][2]==2

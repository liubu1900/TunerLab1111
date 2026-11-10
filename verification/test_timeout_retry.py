"""Unit tests: CalibrationManager retries a MotionTimeout up to 3 times, then aborts.

No real hardware: a fake motion backend raises MotionTimeout a configurable
number of times, then succeeds. We assert how many times the latch was cleared
(reset_fault) and whether the run completed or failed.
"""
import sys

ROOT = 'D:/TunerLab_v0.1.0-20261104/TunerLab_v0.1.0-20261104'
sys.path.insert(0, ROOT + '/TunerLabApp')
sys.path.insert(0, ROOT + '/TunerLab')

import numpy as np
from tuner.motion import MotionError, MotionTimeout
from tuner.models import TunerConfig
from tuner.calibration import CalibrationManager


class FakeMotion:
    """Raises MotionTimeout ``timeouts`` times, then succeeds. Tracks latch reset."""
    def __init__(self, timeouts=0):
        self._timeouts = timeouts
        self.faulted = False
        self.resets = 0
        self.last_target = None

    def move_to(self, position, timeout=None):
        self.last_target = np.asarray(position, dtype=float)
        if self._timeouts > 0:
            self._timeouts -= 1
            self.faulted = True
            raise MotionTimeout('motion timed out before the axes reported idle')
        return self.last_target

    def read_position(self):
        return self.last_target

    def reset_fault(self):
        self.resets += 1
        self.faulted = False


class FakeVNA:
    def __init__(self):
        self.frequencies = np.array([1e9, 2e9])

    def measure(self):
        return ('S', None)


class FakeDB:
    def __init__(self, config, positions):
        self.config = config
        self.positions = positions
        self._pending = set(range(len(positions)))
        self.written = []
        self.failures = []

    def load(self):
        from dataclasses import asdict
        return {'metadata': {'config': asdict(self.config),
                             'simulated': self.config.simulated},
                'positions': self.positions}

    def pending_indices(self):
        return sorted(self._pending)

    def write_point(self, i, pos, meas):
        self._pending.discard(i)
        self.written.append(i)

    def record_failure(self, i, exc):
        self.failures.append((i, exc))


def make_config():
    return TunerConfig(probes=2, x_limits=(0.0, 380.0), z_limits=(0.0, 6.0),
                       safe_z=0.5, fixed_offsets=(0.0, 2.0), motion_timeout=60.0,
                       simulated=True)


def test_retry_then_success():
    cfg = make_config()
    pos = np.array([[10.0, 1.0, 2.0], [20.0, 2.0, 3.0]])
    motion = FakeMotion(timeouts=2)  # first two moves timeout, third succeeds
    db = FakeDB(cfg, pos)
    mgr = CalibrationManager(cfg, motion, FakeVNA(), db, motion_timeout_retries=3)
    state = mgr.run()
    assert state == 'completed', state
    # latch cleared before each of the two retries (not before the final success)
    assert motion.resets == 2, motion.resets
    assert db.written == [0, 1], db.written
    print('test_retry_then_success OK')


def test_retry_exhausted_aborts():
    cfg = make_config()
    pos = np.array([[10.0, 1.0, 2.0]])
    motion = FakeMotion(timeouts=99)  # always times out
    db = FakeDB(cfg, pos)
    mgr = CalibrationManager(cfg, motion, FakeVNA(), db, motion_timeout_retries=3)
    state = mgr.run()
    assert state == 'failed', state
    # attempt 0 -> reset+retry; 1 -> reset+retry; 2 -> reset+retry;
    # attempt 3 -> aborted (no reset). So exactly 3 resets.
    assert motion.resets == 3, motion.resets
    assert db.written == [], db.written
    print('test_retry_exhausted_aborts OK')


def test_non_timeout_motion_error_aborts_immediately():
    class HardFail(FakeMotion):
        def move_to(self, position, timeout=None):
            self.last_target = np.asarray(position, dtype=float)
            raise MotionError('limit trip')

    cfg = make_config()
    pos = np.array([[10.0, 1.0, 2.0]])
    motion = HardFail()
    db = FakeDB(cfg, pos)
    mgr = CalibrationManager(cfg, motion, FakeVNA(), db, motion_timeout_retries=3)
    state = mgr.run()
    assert state == 'failed', state
    assert motion.resets == 0, 'non-timeout fault must NOT reset the latch'
    print('test_non_timeout_motion_error_aborts_immediately OK')


def test_pause_breaks_retry_loop():
    cfg = make_config()
    pos = np.array([[10.0, 1.0, 2.0]])
    motion = FakeMotion(timeouts=99)
    db = FakeDB(cfg, pos)
    mgr = CalibrationManager(cfg, motion, FakeVNA(), db, motion_timeout_retries=3)
    mgr.pause()
    state = mgr.run()
    assert state == 'paused', state
    print('test_pause_breaks_retry_loop OK')


if __name__ == '__main__':
    test_retry_then_success()
    test_retry_exhausted_aborts()
    test_non_timeout_motion_error_aborts_immediately()
    test_pause_breaks_retry_loop()
    print('ALL_OK')

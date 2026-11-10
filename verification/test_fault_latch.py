"""Lightweight check of the stale-fault-latch auto-clear (no real hardware)."""
import sys
from types import SimpleNamespace

sys.path.insert(0, 'tunerlab_app')
sys.path.insert(0, '../TunerLab')

from tuner.motion import MotionError
from tunerlab_app.motion_adapter import TunerMotionController


class FakeLink:
    connected = True


class FakeController(TunerMotionController):
    """Bypass the real __init__ and stub the bits _require_ready touches."""

    def __init__(self, healthy, idle=True):
        self.faulted = True
        self.stopped = False
        self.initialized = True
        self.link = FakeLink()
        self.axes = ['X', 'Z1', 'Z2', 'Z3']
        self.homed_axes = set(self.axes)
        self.homed = True
        self.scale_state = {}
        self.scale_ratio = {}
        self.op_homed = set(self.axes)
        self._healthy = healthy
        self._idle = idle

    def axis_status_snapshot(self, detailed=False):
        fault = not self._healthy
        return [
            {'axis': a, 'idle': 1 if self._idle else 0,
             'status_fault': bool(fault), 'status': 1 if fault else 0}
            for a in self.axes
        ]

    def refresh_home_state(self):
        return set(self.axes)


def test_healthy_clears():
    c = FakeController(healthy=True, idle=True)
    # require_home=False avoids the unrelated re-home gate; we only test the latch.
    c._require_ready(require_home=False, axes=['X'])
    assert c.faulted is False, 'healthy hardware must clear the stale latch'
    print('PASS healthy -> latch cleared, no raise')


def test_hardware_fault_blocks():
    c = FakeController(healthy=False, idle=True)
    try:
        c._require_ready(require_home=False, axes=['X'])
    except MotionError as exc:
        assert 'latched' in str(exc), f'unexpected message: {exc}'
        print('PASS hardware fault -> still latched (raised as expected)')
        return
    raise AssertionError('should have raised on a real hardware fault')


def test_not_idle_blocks():
    c = FakeController(healthy=True, idle=False)
    try:
        c._require_ready(require_home=False, axes=['X'])
    except MotionError as exc:
        assert 'latched' in str(exc), f'unexpected message: {exc}'
        print('PASS axis not idle -> still latched (raised as expected)')
        return
    raise AssertionError('should have raised when an axis is still moving')


if __name__ == '__main__':
    test_healthy_clears()
    test_hardware_fault_blocks()
    test_not_idle_blocks()
    print('ALL OK')

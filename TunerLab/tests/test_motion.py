import threading
import time
import numpy as np
import pytest
from tuner.models import TunerConfig
from tuner.motion import MotionController, MockMotionController, MotionPlanner, MotionError

@pytest.mark.parametrize('n', [2,3])
def test_legacy_retract_before_x_when_coupled(n):
    """Opting out of independent axes restores the interlock and staged plan."""
    c=TunerConfig(probes=n, fixed_offsets=tuple(range(n)), independent_axes=False)
    m=MockMotionController(c)
    with pytest.raises(MotionError): m.home()
    m.initialize()
    with pytest.raises(MotionError): m.move_to([0]+[.2]*n)
    m.home()
    m.move_to([0]+[.4]*n)
    with pytest.raises(MotionError): m.move_to([2]+[.4]*n)
    with pytest.raises(MotionError): m.jog('X',1)
    p=MotionPlanner(c,m)
    p.move_to([2]+[.7]*n)
    assert np.allclose(m.history[-3], [0]+[0]*n)
    assert np.allclose(m.history[-2], [2]+[0]*n)
    assert np.allclose(m.history[-1], [2]+[.7]*n)
    assert np.allclose(m.read_position(), [2]+[.7]*n)


@pytest.mark.parametrize('n', [2,3])
def test_independent_axes_allow_x_with_probes_inserted(n):
    """Default policy: X is not gated on probes and the plan is one move."""
    c=TunerConfig(probes=n, fixed_offsets=tuple(range(n)))
    assert c.independent_axes
    m=MockMotionController(c)
    m.initialize(); m.home()
    m.move_to([0]+[.4]*n)                 # probes stay inserted
    m.move_to([2]+[.4]*n)                 # X moves freely
    assert np.allclose(m.read_position(), [2]+[.4]*n)
    p=MotionPlanner(c,m)
    before=len(m.history)
    p.move_to([3]+[.7]*n)                 # no automatic retraction
    assert len(m.history) == before + 1
    assert np.allclose(m.history[-1], [3]+[.7]*n)
    assert np.allclose(m.read_position(), [3]+[.7]*n)


def test_limits_jog_and_emergency():
    c=TunerConfig(); m=MotionController(c); m.initialize(); m.home()
    for p in ([11,0,0], [0,-1,0], [0,float('nan'),0], [0,0]):
        with pytest.raises(ValueError): m.move_to(p)
    m.jog('Z1',.2)
    m.jog('X',1)              # independent axes: allowed with a probe inserted
    m.retract(); m.jog('X',1) # manual retract still works, but is not required
    with pytest.raises(ValueError): m.jog('Y',1)
    m.emergency_stop()
    with pytest.raises(MotionError): m.retract()
    m.reset_emergency()
    with pytest.raises(MotionError): m.move_to([0,0,0])
    m.home()
    assert m.homed


def test_timeout_and_stop_during_move():
    m=MotionController(TunerConfig()); m.initialize(); m.home()
    m.move_duration=.05
    with pytest.raises(TimeoutError): m.move_to([1,0,0], timeout=.001)
    assert not m.homed
    assert m.read_position()[0] == 0
    m.move_duration=0; m.home(); m.move_duration=.2
    errors=[]
    def worker():
        try: m.move_to([1,0,0])
        except Exception as e: errors.append(e)
    t=threading.Thread(target=worker); t.start(); time.sleep(.01)
    m.emergency_stop(); t.join(timeout=1)
    assert not t.is_alive()
    assert isinstance(errors[0], MotionError)
    assert m.read_position()[0] == 0


def test_mock_provenance_guard():
    with pytest.raises(ValueError): MotionController(TunerConfig(simulated=False))

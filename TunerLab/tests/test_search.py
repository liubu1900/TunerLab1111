import numpy as np
import pytest
from dataclasses import asdict
from tuner.search import GammaSearchEngine
from tuner.tuning import TuningManager
from tuner.models import TunerConfig, Measurement

def data():
    s=np.zeros((4,2,2,2),complex)
    s[:,:,0,0]=np.array([0,.2,.4,.8])[:,None]
    return {'frequencies':np.array([1e9,2e9]),'positions':np.array([[0,0,0],[1,0,0],[2,0,0],[3,0,0]],float),'s':s,'valid':np.array([1,1,1,0],bool),'metadata':{'config':asdict(TunerConfig()),'simulated':True}}

def test_search_excludes_invalid_and_ranks():
    engine=GammaSearchEngine(data())
    r=engine.search(1.5e9,.21,k=10)
    assert [v.index for v in r]==[1,2,0]
    assert r[0].reachable and not r[1].reachable
    assert engine.search(1e9,.8,k=1)[0].reachable is False
    with pytest.raises(ValueError): engine.search(3e9,0)

def test_multi_and_interpolation():
    engine=GammaSearchEngine(data())
    assert engine.search_multi([1e9,2e9],[.2,.2],weights=[1,2])[0].index==1
    opt=engine.optimize_local(1e9,.3)
    assert opt['predicted'] and opt['interpolated']
    assert abs(opt['position'][0]-1.5)<1e-4
    with pytest.raises(ValueError): engine.search_multi([1e9],[0],weights=[0])

def test_tuning_separates_simulated_rf():
    class Motion:
        def move_to(self,p,timeout=None): self.p=p.copy()
        p=np.zeros(3)
        def read_position(self): return self.p
    class VNA:
        def measure(self):
            s=np.zeros((2,2,2),complex); s[:,0,0]=.2
            return Measurement(np.array([1e9,2e9]),s,True)
    config=TunerConfig()
    r=TuningManager(config,Motion(),GammaSearchEngine(data()),VNA()).tune(1e9,75,verify=True)
    assert r['mechanically_positioned'] and r['verification_passed']
    assert r['verification_kind']=='simulated_rf' and not r['rf_verified']


def test_tuning_rejects_calibration_geometry_and_provenance():
    from tuner.motion import MockMotionController
    config=TunerConfig(); motion=MockMotionController(config)
    altered=data(); altered['metadata']['config']['fixed_offsets']=[0.,3.]
    with pytest.raises(ValueError,match='fixed_offsets'):
        TuningManager(config,motion,GammaSearchEngine(altered))
    altered=data(); altered['metadata']['simulated']=False
    with pytest.raises(ValueError,match='provenance'):
        TuningManager(config,motion,GammaSearchEngine(altered))
    altered=data(); altered.pop('metadata')
    with pytest.raises(ValueError,match='metadata'):
        TuningManager(config,motion,GammaSearchEngine(altered))

def test_tuning_wraps_raw_controller_with_legacy_staged_plan():
    from tuner.motion import MockMotionController
    config=TunerConfig(independent_axes=False)
    motion=MockMotionController(config); motion.initialize(); motion.home()
    motion.move_to([0,.5,.5])
    result=TuningManager(config,motion,GammaSearchEngine(data())).tune(1e9,75)
    assert result['mechanically_positioned']
    np.testing.assert_allclose(motion.history[-2],[1,0,0])
    np.testing.assert_allclose(motion.history[-3],[0,0,0])


def test_tuning_issues_one_direct_move_with_independent_axes():
    """Default policy: tuning does not retract probes on X's behalf."""
    from tuner.motion import MockMotionController
    config=TunerConfig(); motion=MockMotionController(config); motion.initialize(); motion.home()
    motion.move_to([0,.5,.5])
    before=len(motion.history)
    result=TuningManager(config,motion,GammaSearchEngine(data())).tune(1e9,75)
    assert result['mechanically_positioned']
    assert len(motion.history) == before + 1
    assert np.allclose(motion.history[-1], result['candidate'].position)

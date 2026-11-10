import numpy as np
import pytest
from tuner.models import TunerConfig
from tuner.vna import MockVNAController, PyVISAVNAController, VISAProfile

class Position:
    def __init__(self,p): self.p=p
    def read_position(self): return self.p

@pytest.mark.parametrize('n',[2,3])
def test_mock_passive_reciprocal_complete(n):
    c=TunerConfig(probes=n,fixed_offsets=tuple(range(n)))
    m=Position([2]+[.7]*n)
    v=MockVNAController(c,m,np.linspace(1e9,30e9,101))
    with pytest.raises(RuntimeError): v.measure()
    v.initialize(); result=v.measure()
    assert result.simulated and result.s.shape==(101,2,2)
    assert np.allclose(result.s[:,0,1],result.s[:,1,0])
    assert np.max(np.linalg.svd(result.s,compute_uv=False)) <= 1+1e-10
    original=result.s.copy(); m.p[0]=4
    assert not np.allclose(v.measure().s,original)
    m.p[-1]=.3
    assert not np.allclose(v.measure().s,original)
    v.close()
    with pytest.raises(RuntimeError): v.measure()

class Instrument:
    def __init__(self): self.commands=[]; self.bad=False
    def write(self,s): self.commands.append(s)
    def query(self,s): return '1'
    def query_ascii_values(self,s):
        if s=='freq': return [1e9,2e9]
        if self.bad: return [0]
        return [int(s[-1]),.1,int(s[-1]),.2]
    def close(self): pass
class Manager:
    def __init__(self): self.inst=Instrument(); self.opened=False
    def open_resource(self,s): self.opened=True; return self.inst

def profile():
    return VISAProfile('USER_RESOURCE',1000,'freq',('trigger',),'done',dict(S11='t1',S21='t2',S12='t3',S22='t4'))

def test_visa_explicit_opt_in_mapping_and_length():
    rm=Manager(); v=PyVISAVNAController(profile(),resource_manager=rm)
    with pytest.raises(PermissionError): v.initialize()
    assert not rm.opened
    v.enabled=True; v.initialize(); r=v.measure()
    assert not r.simulated and r.s[0,1,0]==2+.1j and r.s[0,0,1]==3+.1j
    rm.inst.bad=True
    with pytest.raises(ValueError): v.measure()
    v.close()

@pytest.mark.parametrize('f',[[2,1],[0,1],[float('nan')],[]])
def test_bad_frequency(f):
    with pytest.raises(ValueError): MockVNAController(TunerConfig(),Position([0,0,0]),f)

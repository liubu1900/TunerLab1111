import json
import numpy as np
import pytest
from tuner.validation import vector_statistics


def test_vector_complex_statistics():
    a=np.array([[1+1j,0],[1-1j,2]])
    r=vector_statistics(a)
    assert r['rms_vector']==[1.,1.]
    assert np.allclose(r['sample_std_vector'],np.sqrt(2))
    with pytest.raises(ValueError): vector_statistics(a[:1])

@pytest.mark.parametrize('probes',[2,3])
def test_mock_end_to_end(tmp_path,probes):
    from tuner.demo import run_demo
    report=run_demo(tmp_path,probes)
    assert report['simulated'] is True
    assert report['rf_hardware_verified'] is False
    assert report['points']==5*3**probes
    assert report['valid_points']==report['points']
    assert len(report['repeatability']['positions']['A']['S21']['rms_vector'])==19
    assert json.loads((tmp_path/f'mock_{probes}probe_report.json').read_text())['probes']==probes
    assert report['candidates'][0]['error']<1e-8
    with pytest.raises(FileExistsError): run_demo(tmp_path,probes)

def test_real_repeatability_does_not_claim_impedance_accuracy():
    from tuner.models import Measurement
    from tuner.validation import repeatability_report
    class Motion:
        def move_to(self,p): self.p=np.asarray(p)
        def read_position(self): return self.p
    class VNA:
        def measure(self): return Measurement(np.array([1e9]),np.zeros((1,2,2),complex),False)
    report=repeatability_report(Motion(),VNA(),[0,0,0],[1,0,0],2)
    assert report['real_measurements']
    assert not report['rf_hardware_verified']

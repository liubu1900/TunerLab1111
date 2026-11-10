import numpy as np
import pytest
from tuner.rf import RFNetworkEngine as RF

def test_z_gamma_roundtrip():
    z=np.array([0,50,100+20j,5-300j]); np.testing.assert_allclose(RF.gamma_to_z(RF.z_to_gamma(z)),z,atol=1e-10)
    assert RF.z_to_gamma(np.inf)==1
    assert np.isinf(RF.gamma_to_z(1))
    with pytest.raises(ValueError): RF.z_to_gamma(-50)

def test_terminated_orientation():
    s=np.array([[.1,.3],[.4,.2]],complex); termination=.5j
    expected=.1+.3*.4*termination/(1-.2*termination)
    assert RF.terminated_gamma(s,termination)==expected
    assert RF.terminated_gamma(s)==.1

def test_fixture_and_cascade():
    s=np.array([[.1,.3],[.4,.2]],complex)
    t=np.array([[0,.9j],[.9j,0]],complex)
    np.testing.assert_allclose(RF.dut_gamma(s,.2,t),RF.terminated_gamma(RF.cascade(t,s),.2))
    thru=np.array([[0,1],[1,0]],complex)
    np.testing.assert_allclose(RF.cascade(thru,s),s)
    np.testing.assert_allclose(RF.cascade(s,thru),s)

def test_singular_termination():
    with pytest.raises(ValueError): RF.terminated_gamma(np.ones((2,2)),1)

def test_skrf_cascade_crosscheck():
    import skrf
    rng=np.random.default_rng(7)
    a=(rng.normal(size=(5,2,2))+1j*rng.normal(size=(5,2,2)))*.1
    b=(rng.normal(size=(5,2,2))+1j*rng.normal(size=(5,2,2)))*.1
    frequency=skrf.Frequency.from_f(np.arange(1,6)*1e9,unit='hz')
    expected=(skrf.Network(frequency=frequency,s=a,z0=50)**skrf.Network(frequency=frequency,s=b,z0=50)).s
    np.testing.assert_allclose(RF.cascade(a,b),expected,atol=1e-14)

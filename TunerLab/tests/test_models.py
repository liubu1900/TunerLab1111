import numpy as np
import pytest
from tuner.models import TunerConfig

@pytest.mark.parametrize('probes,total',[(2,12),(3,24)])
def test_grid(probes,total):
    c=TunerConfig(probes=probes,fixed_offsets=tuple(range(probes)))
    assert c.grid([0,1,2],[0,1]).shape==(total,probes+1)

@pytest.mark.parametrize('kwargs',[
    {'x_limits':(2,1)}, {'z_limits':(0,float('nan'))},
    {'z0':0}, {'motion_timeout':-1}, {'fixed_offsets':(0,float('inf'))}])
def test_bad_configuration(kwargs):
    with pytest.raises(ValueError): TunerConfig(**kwargs)

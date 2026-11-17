"""Derive a measured-truth library following an equal-|Γ| / Smith plan."""
import numpy as np
import pytest

from tuner.database import CalibrationDatabase
from tuner.library_plan_derive import derive_by_gamma_plan, plan_target_gamma
from tuner.models import TunerConfig
from tuner.pointplan import DENSITY_PRESETS
from tuner.virtual_data import generate_calibration


@pytest.fixture(scope='module')
def source_lib(tmp_path_factory):
    path = tmp_path_factory.mktemp('lib') / 'src.h5'
    generate_calibration(str(path), TunerConfig(), x_count=6, z_count=5)
    return str(path)


def test_target_generation_both_plans():
    for plan in ('gamma_rings', 'smith'):
        t = plan_target_gamma(plan, count=95, r_min=0.0, r_max=0.9)
        assert t.shape == (95,)
        assert np.all(np.abs(t) <= 0.9 + 1e-9)
    with pytest.raises(ValueError):
        plan_target_gamma('nope', count=10)


@pytest.mark.parametrize('plan', ['gamma_rings', 'smith'])
def test_derive_reuses_measured_truth(source_lib, tmp_path, plan):
    src = CalibrationDatabase(source_lib)
    out = str(tmp_path / f'{plan}.h5')
    res = derive_by_gamma_plan(src, out, plan=plan, density='low',
                               frequency_hz=None)
    assert res['plan'] == plan
    assert res['points'] == DENSITY_PRESETS['low']
    assert res['parent_version']  # lineage recorded

    db = CalibrationDatabase(out)
    data = db.load()
    assert bool(np.all(data['valid'])), 'derived points must all be measured truth'
    assert len(data['positions']) == DENSITY_PRESETS['low']
    assert data['s'].shape[0] == DENSITY_PRESETS['low']
    meta = db.read_metadata()
    assert meta['operation'] == 'derive_gamma_plan'
    assert meta['point_plan'] == plan


def test_dedupe_reduces_or_keeps_point_count(source_lib, tmp_path):
    src = CalibrationDatabase(source_lib)
    out = str(tmp_path / 'dedupe.h5')
    res = derive_by_gamma_plan(src, out, plan='smith', density='low', dedupe=True)
    assert 1 <= res['points'] <= DENSITY_PRESETS['low']
    assert res['deduped'] is True


def test_frequency_outside_grid_rejected(source_lib, tmp_path):
    src = CalibrationDatabase(source_lib)
    with pytest.raises(ValueError):
        derive_by_gamma_plan(src, str(tmp_path / 'x.h5'), plan='smith',
                             density='low', frequency_hz=999e9)

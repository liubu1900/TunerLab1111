"""Vector S11 repeatability vs library memory (20log|S11^m - S11^c|)."""
import json

import numpy as np
import pytest

from tuner.repeatability import (
    vector_repeatability_memory, vector_repeatability_report,
)


def test_formula_scalar():
    # |0.51+0.1j - 0.5+0.1j| = 0.01 -> 20log10(0.01) = -40 dB
    r = vector_repeatability_memory(complex(0.51, 0.1), complex(0.5, 0.1))
    assert r['scalar_db'] == pytest.approx(-40.0, abs=1e-6)
    assert r['delta'] == pytest.approx(0.01, abs=1e-9)


def test_formula_uses_complex_vector_difference():
    # Same magnitude, different phase: the vector difference is non-zero.
    m = 0.5 * np.exp(1j * np.deg2rad(2))
    c = 0.5 + 0j
    r = vector_repeatability_memory(m, c)
    assert r['delta'] > 0
    assert np.isfinite(r['scalar_db'])


def test_zero_difference_is_clamped_finite():
    r = vector_repeatability_memory(complex(0.3, -0.2), complex(0.3, -0.2))
    assert np.isfinite(r['worst_db'])
    assert r['worst_db'] < -500  # effectively perfect repetition


def test_per_frequency_arrays_and_summary():
    measured = np.array([complex(0.51, 0.1), complex(0.55, 0.1)])
    memory = np.array([complex(0.5, 0.1), complex(0.5, 0.1)])
    r = vector_repeatability_memory(measured, memory)
    assert r['count'] == 2
    assert r['repeatability_db'].shape == (2,)
    assert r['worst_db'] == pytest.approx(max(r['repeatability_db']))
    assert r['best_db'] == pytest.approx(min(r['repeatability_db']))


def test_shape_mismatch_rejected():
    with pytest.raises(ValueError):
        vector_repeatability_memory(np.array([1 + 0j, 2 + 0j]), np.array([1 + 0j]))


def test_report_serialisable_and_tolerance():
    row = vector_repeatability_report(
        [5e9, 10e9], [complex(0.51, 0.1), complex(0.52, 0.1)],
        [complex(0.5, 0.1), complex(0.5, 0.1)],
        point_index=7, position=[1.0, 0.5, 0.25], tolerance_db=-30.0)
    assert row['point_index'] == 7
    assert len(row['repeatability_db']) == 2
    assert row['passed'] is True
    json.dumps(row)  # must be JSON-serialisable

    fail = vector_repeatability_report(
        [5e9], [complex(0.8, 0.0)], [complex(0.5, 0.0)], tolerance_db=-30.0)
    assert fail['passed'] is False

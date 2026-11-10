"""P2 reference-plane shifting.

The physics has to be right before any UI is built, so these tests check the
transformation against cases with a known answer:

* a lossless line of length *l* rotates Γ by ``e^(−2jβl)`` when the device is
  seen *through* the line;
* shifting and shifting back returns the original network;
* a matched line does not change the S-parameters' magnitudes;
* an S2P fixture is **not** interpolated between its samples.
"""
import numpy as np
import pytest

from tuner.reference_shift import (
    abcd_from_line, line_s_parameters, read_s2p, s2p_at_frequencies,
    shift_gamma, shift_s_parameters,
)

VF = 0.66


def test_a_lossless_line_rotates_gamma_by_the_round_trip_factor():
    """Seen through a line of length l, Γ is rotated by e^(−2jβl)."""
    frequency = np.array([5e9])
    length = 0.001                       # 1 mm
    beta = float(2 * np.pi * frequency[0] / (299_792_458.0 * VF))
    network = line_s_parameters(length, frequency, velocity_factor=VF)
    gamma = 0.3 + 0.4j
    seen_through = shift_gamma(gamma, network[0], direction='embed')
    assert seen_through == pytest.approx(gamma * np.exp(-2j * beta * length))


def test_deembedding_undoes_the_rotation():
    """Taking the line out rotates Γ back by e^(+2jβl)."""
    frequency = np.array([5e9])
    beta = float(2 * np.pi * frequency[0] / (299_792_458.0 * VF))
    network = line_s_parameters(0.001, frequency, velocity_factor=VF)
    gamma = 0.3 + 0.4j
    measured = shift_gamma(gamma, network[0], direction='embed')
    restored = shift_gamma(measured, network[0], direction='deembed')
    assert restored == pytest.approx(gamma)


def test_a_zero_length_line_changes_nothing():
    network = line_s_parameters(0.0, np.array([5e9]))
    gamma = 0.42 - 0.11j
    assert shift_gamma(gamma, network[0]) == pytest.approx(gamma)


def test_the_abcd_matrix_of_a_line_matches_the_known_form():
    """A = D = cos βl, B = j·Z₀·sin βl, C = j·sin βl / Z₀."""
    frequency = np.array([5e9])
    length = 0.001
    beta = float(2 * np.pi * frequency[0] / (299_792_458.0 * VF)) * length
    matrix = abcd_from_line(length, frequency, z0=50.0, velocity_factor=VF)[0]
    assert matrix[0, 0] == pytest.approx(np.cos(beta))
    assert matrix[1, 1] == pytest.approx(np.cos(beta))
    assert matrix[0, 1] == pytest.approx(1j * 50.0 * np.sin(beta))
    assert matrix[1, 0] == pytest.approx(1j * np.sin(beta) / 50.0)


def test_shift_s_parameters_round_trips():
    s = np.array([[0.2 + 0.1j, 0.7 - 0.2j], [0.6 + 0.05j, -0.1 + 0.3j]])
    network = line_s_parameters(0.001, np.array([5e9]), velocity_factor=VF)
    there = shift_s_parameters(s, network[0], direction='embed')
    back = shift_s_parameters(there, network[0], direction='deembed')
    assert back == pytest.approx(s)


def test_a_matched_line_keeps_the_magnitudes():
    """A lossless matched line changes the phase, not the magnitude."""
    s = np.array([[0.2 + 0.1j, 0.7 - 0.2j], [0.6 + 0.05j, -0.1 + 0.3j]])
    network = line_s_parameters(0.001, np.array([5e9]), velocity_factor=VF)
    shifted = shift_s_parameters(s, network[0], direction='embed')
    assert abs(shifted[0, 0]) == pytest.approx(abs(s[0, 0]))
    assert abs(shifted[0, 1]) == pytest.approx(abs(s[0, 1]))
    assert abs(shifted[1, 0]) == pytest.approx(abs(s[1, 0]))


def test_s2p_is_read_with_its_unit():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as folder:
        s2p = Path(folder) / 'fix.s2p'
        s2p.write_text('# GHz S RI R 50\n'
                       '5 0.1 0.2 0.3 0.4 0.5 0.6 0.7 0.8\n'
                       '10 0.11 0.21 0.31 0.41 0.51 0.61 0.71 0.81\n',
                       encoding='utf-8')
        frequencies, matrices = read_s2p(s2p)
    assert frequencies == pytest.approx([5e9, 10e9])
    assert matrices.shape == (2, 2, 2)
    assert matrices[0][0, 0] == pytest.approx(0.1 + 0.2j)
    assert matrices[0][0, 1] == pytest.approx(0.5 + 0.6j)
    assert matrices[0][1, 0] == pytest.approx(0.3 + 0.4j)


def test_a_missing_fixture_frequency_aborts():
    """The fixture is not interpolated between its samples."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as folder:
        s2p = Path(folder) / 'fix.s2p'
        s2p.write_text('# GHz S RI R 50\n5 0.1 0.2 0.3 0.4 0.5 0.6 0.7 0.8\n',
                       encoding='utf-8')
        with pytest.raises(ValueError) as info:
            s2p_at_frequencies(s2p, np.array([5e9, 7e9]), tol_hz=1.0)
    assert 'not interpolated' in str(info.value)


def test_a_fixture_sample_is_used_at_the_right_frequency():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as folder:
        s2p = Path(folder) / 'fix.s2p'
        s2p.write_text('# GHz S RI R 50\n'
                       '5 0.1 0.2 0.3 0.4 0.5 0.6 0.7 0.8\n',
                       encoding='utf-8')
        matrices = s2p_at_frequencies(s2p, np.array([5e9]), tol_hz=1.0)
    assert matrices.shape == (1, 2, 2)
    assert matrices[0][0, 0] == pytest.approx(0.1 + 0.2j)


def test_invalid_line_parameters_are_refused():
    with pytest.raises(ValueError):
        line_s_parameters(-0.001, np.array([5e9]))
    with pytest.raises(ValueError):
        line_s_parameters(0.001, np.array([5e9]), velocity_factor=1.5)
    with pytest.raises(ValueError):
        abcd_from_line(0.001, np.array([5e9]), z0=0)


def test_an_unknown_direction_is_refused():
    network = line_s_parameters(0.001, np.array([5e9]))
    with pytest.raises(ValueError):
        shift_gamma(0.1 + 0j, network[0], direction='sideways')

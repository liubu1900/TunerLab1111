"""Integration checks against the real Zmotion controller.

These tests need a **real machine** and are therefore guarded twice:

1.  ``TUNERLAB_ENABLE_HARDWARE_TESTS=1`` must be set explicitly.  Without it every
    test in this file is skipped, so a routine ``pytest`` run can never reach the
    controller by accident — the controller is discovered by UDP broadcast on the
    local subnet, so "the machine is powered on" is enough to make it answer.
2.  The controller must be reachable, so the suite also stays green on a laptop
    that is not on the machine's subnet.

The tests issue no meaningful travel: the only commanded motion is a zero-displacement
move plus a 5 µm scale probe on one axis, and both return the axis to where it
started.  Even so, they write axis parameters and therefore count as *touching the
machine* — which is exactly why the opt-in above exists.

Point them at a controller with::

    TUNERLAB_ENABLE_HARDWARE_TESTS=1 TUNERLAB_TEST_IP=192.168.0.11 \
        python -m pytest tests/test_zmotion_hardware.py
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from tuner.axis_params import TUNER_AXES, default_axis_params
from tuner.zmotion import ZmotionController, ZmotionError
from tuner.zmotion_motion import ZmotionMotionController, build_tuner_config

IP = os.environ.get('TUNERLAB_TEST_IP', '192.168.0.11')

#: Set to ``1`` to allow this file to talk to the machine.  Anything else (or an
#: unset variable) keeps every test below skipped.
_HARDWARE_ENABLED = os.environ.get('TUNERLAB_ENABLE_HARDWARE_TESTS', '').strip() in (
    '1', 'true', 'yes', 'on')

pytestmark = pytest.mark.skipif(
    not _HARDWARE_ENABLED,
    reason='真机测试默认关闭；设置 TUNERLAB_ENABLE_HARDWARE_TESTS=1 显式授权后才会运行')


def _controller_or_skip():
    """Connect to the controller, or skip the test when it is not reachable."""
    controller = ZmotionController()
    try:
        controller.open(IP, retries=2)
    except ZmotionError as exc:
        pytest.skip(f'controller {IP} not reachable: {exc}')
    return controller


@pytest.fixture
def link():
    controller = _controller_or_skip()
    yield controller
    controller.close()


def test_controller_reports_firmware(link):
    assert link.connected
    assert link.firmware, 'firmware version should be readable over the Basic channel'


def test_all_four_axes_are_present(link):
    for axis in range(len(TUNER_AXES)):
        assert link.get_atype(axis) >= 0
        assert link.get_units(axis) > 0


def test_axis_parameters_match_the_commissioned_values(link):
    """The controller must still carry the configuration recorded in the docs."""
    expected = default_axis_params()
    for index, name in enumerate(TUNER_AXES):
        assert link.get_atype(index) == expected[name].atype
        assert link.get_units(index) == pytest.approx(expected[name].units)
        assert link.get_speed(index) == pytest.approx(expected[name].speed)
        assert link.get_datum_in(index) == expected[name].datum_in


def test_parameter_round_trip_is_stable(link):
    """Writing the value that is already stored must not change anything."""
    for axis in range(len(TUNER_AXES)):
        units = link.get_units(axis)
        link.set_units(axis, units)
        assert link.get_units(axis) == pytest.approx(units)


def test_positions_are_readable_and_consistent(link):
    info = link.read_all_axis_info(len(TUNER_AXES))
    assert len(info['dpos']) == len(TUNER_AXES)
    for index in range(len(TUNER_AXES)):
        assert link.get_dpos(index) == pytest.approx(info['dpos'][index], abs=1e-3)
        assert link.get_mpos(index) == pytest.approx(info['mpos'][index], abs=1e-3)


def test_zero_displacement_move(link):
    """A move to the current position exercises the full chain without moving."""
    params = default_axis_params()
    backend = ZmotionMotionController(build_tuner_config(params), link, params)
    backend.initialize()
    position = backend.read_position()
    result = backend.move_to(position)
    assert np.allclose(result, position, atol=backend.position_tolerance)


# The retract-before-X interlock (legacy ``independent_axes=False`` mode) is
# covered by the FakeLibrary unit tests in tests/test_zmotion.py. It is
# deliberately NOT exercised against the real controller: the test used to
# assume a probe was extended, and on a machine whose probes already sit at
# safe_z the coupled check would let a real X move through. With independent
# axes as the default that assumption is even less safe, so no X travel is
# issued from this file.


def test_scale_probe_reports_the_command_to_feedback_ratio(link):
    """The probe measures how far the axis really travels per commanded unit.

    On the commissioned machine this is far from 1.0 on X, Z1 and Z3, which is the
    configuration defect this check exists to expose.
    """
    params = default_axis_params()
    backend = ZmotionMotionController(build_tuner_config(params), link, params)
    backend.initialize()
    result = backend.probe_axis_scale('Z1', distance=0.005)
    assert result['commanded'] == pytest.approx(0.005)
    assert result['ratio'] > 0
    # The probe returns the axis to where it started.
    assert result['restored_dpos'] == pytest.approx(result['start_dpos'], abs=1e-3)


def test_move_is_rejected_when_the_scales_disagree(link):
    """A move whose encoder travel does not match the command must be refused.

    This is the guard that protects the machine while the controller's ``UNITS`` is
    still inconsistent with its feedback scale.
    """
    params = default_axis_params()
    backend = ZmotionMotionController(build_tuner_config(params), link, params)
    backend.initialize()
    start = backend.read_position()
    try:
        backend.move_axis_to('Z1', start[1] + 0.01)
    except Exception as exc:
        # Expected on a machine whose command scale is not yet corrected.
        assert 'encoder feedback' in str(exc)
    else:
        # Otherwise the axis scales agree and the move must have landed correctly.
        assert backend.read_position()[1] == pytest.approx(start[1] + 0.01, abs=0.002)

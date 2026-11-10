"""Decoding of the Zmotion controller's own diagnostics.

The reference values are the ones observed on the real controller:
``AXISSTATUS = 4194304`` is bit 22, which the vendor documents as "告警信号输入"
(alarm signal input) — the condition that was reported on Z2 before the
machine was repaired.
"""
import pytest

from tuner.zmotion_errors import (
    AXIS_STATUS_BITS, CONTROLLER_ERRORS, DLL_ERRORS,
    decode_axis_status, describe_axis_status, describe_controller_error,
    describe_dll_error, describe_error_code, describe_stop_reason, has_fault,
)


def test_observed_alarm_signal_is_decoded():
    # Z2 reported AXISSTATUS = 4194304 -> bit22, "告警信号输入".
    flags = decode_axis_status(4194304)
    assert flags == ['告警信号输入 (bit22)']
    assert '告警信号输入' in describe_axis_status(4194304)
    assert has_fault(4194304)


def test_observed_stop_reason_combination():
    # STOP_REASON = 4196352 = bit22 (alarm input) + bit11 (CANCEL running).
    flags = decode_axis_status(4196352)
    assert len(flags) == 2
    assert any('告警信号输入' in f for f in flags)
    assert any('CANCEL' in f for f in flags)
    assert '告警信号输入' in describe_stop_reason(4196352)


def test_busy_bits_are_not_faults():
    # bit6 = 找原点中 (homing in progress) is activity, not a fault.
    assert decode_axis_status(64) == ['找原点中 (bit6)']
    assert not has_fault(64)
    # bit11 = CANCEL 执行中 is also activity.
    assert not has_fault(2048)


def test_normal_status_has_no_flags():
    assert decode_axis_status(0) == []
    assert describe_axis_status(0) == '正常'
    assert not has_fault(0)
    assert describe_stop_reason(0) == '无锁存停止原因'


def test_undocumented_bits_are_reported_not_swallowed():
    # bit13 is not used by the vendor; it must still show up.
    flags = decode_axis_status(1 << 13)
    assert any('未定义状态位 bit13' in f for f in flags)
    assert has_fault(1 << 13)


def test_documented_bit_table_matches_vendor_values():
    # Spot-check the print values the vendor publishes next to each bit.
    expected = {1: 2, 2: 4, 3: 8, 4: 16, 5: 32, 6: 64, 7: 128, 8: 256,
                9: 512, 10: 1024, 11: 2048, 12: 4096, 14: 16384, 18: 262144,
                19: 524288, 21: 2097152, 22: 4194304, 23: 8388608}
    for bit, _text in AXIS_STATUS_BITS:
        assert expected[bit] == 1 << bit


def test_error_code_descriptions():
    assert '找不到控制器' in describe_controller_error(20008)
    assert '参数错误' in describe_dll_error(30002)
    assert '成功' in describe_dll_error(0)
    assert '未知' in describe_dll_error(123456)
    assert '未知' in describe_controller_error(987654)
    assert '无错误码' in describe_error_code(None)


def test_error_code_namespaces_do_not_collide():
    # A controller code wins over the DLL table when both could match.
    assert describe_error_code(1009) == describe_controller_error(1009)
    assert describe_error_code(30002) == describe_dll_error(30002)
    assert 30002 in DLL_ERRORS
    assert 1009 in CONTROLLER_ERRORS


def test_negative_status_is_rejected():
    with pytest.raises(ValueError):
        decode_axis_status(-1)

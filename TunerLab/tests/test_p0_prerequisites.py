"""F-03 统一前提条件检查单（硬门禁）——单元测试。

只依赖 tuner.prerequisites 纯函数引擎；不连真机、不运行软件、不做 UI/API 接线。
"""
import json
import os
import sys
import time

import numpy as np
import pytest

# 让本测试文件在任意工作目录下都能 import 到 TunerLab/tuner 包。
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from tuner.prerequisites import (  # noqa: E402
    CALIBRATION_MAX_AGE_DAYS,
    CHECK_IDS,
    CHECK_TITLES,
    format_report_text,
    run_prerequisite_checks,
    snapshot_for_metadata,
)

EXPECTED_KEYS = {'passed', 'checks', 'failed_hard', 'warnings', 'timestamp', 'summary'}
CHECK_KEYS = {'id', 'title', 'status', 'detail', 'hard'}


def good_context():
    """一个完全合规的真实建库前提上下文。"""
    return {
        'vna_source': 'real',
        'motion_source': 'real',
        'vna_calibration': {
            'verified': True,
            'method': 'TRL',
            'cal_kit': 'TRL-kit-01',
            'reference_plane': 'probe-tip',
            'timestamp': time.time() - 60.0,
        },
        'frequencies_hz': [1e9, 2e9, 3e9, 4e9, 5e9],
        's2p_frequencies_hz': [1e9, 2e9, 3e9, 4e9, 5e9],
        's2p_path': 'deembed.s2p',
        'axes': {
            'x1': {'homed': True, 'feedback_scale': 1.0},
            'x2': {'homed': True, 'feedback_scale': 1.0},
            'z1': {'homed': True, 'feedback_scale': 1.0},
            'z2': {'homed': True, 'feedback_scale': 1.0},
        },
        # 总轴数 = 探针数 + 1（X 轴由所有探针共用，见 TunerConfig.axes）
        'axis_count': 4,
        'probes': 3,
        'x_limits': (0.0, 50.0),
        'z_limits': (0.0, 25.0),
        'reciprocity_confirmed': True,
        's21': 0.9 + 0.01j,
        's12': 0.9 + 0.01j,
    }


def by_id(report, check_id):
    return next(c for c in report['checks'] if c['id'] == check_id)


def assert_report_shape(report):
    """报告结构、字段类型、hard/failed_hard/warnings 三者自洽。"""
    assert isinstance(report, dict)
    assert set(report.keys()) == EXPECTED_KEYS
    assert isinstance(report['passed'], bool)
    assert isinstance(report['timestamp'], float)
    assert isinstance(report['summary'], str) and report['summary']
    assert isinstance(report['failed_hard'], list)
    assert isinstance(report['warnings'], list)
    assert len(report['checks']) == 6
    assert [c['id'] for c in report['checks']] == list(CHECK_IDS)
    for check in report['checks']:
        assert set(check.keys()) == CHECK_KEYS
        assert check['status'] in ('pass', 'warn', 'fail')
        assert check['title'] == CHECK_TITLES[check['id']]
        assert isinstance(check['detail'], str) and check['detail']
        assert check['hard'] is (check['status'] == 'fail')
    assert report['passed'] is (len(report['failed_hard']) == 0)
    assert report['failed_hard'] == [
        c['id'] for c in report['checks'] if c['status'] == 'fail'
    ]
    assert report['warnings'] == [
        c['id'] for c in report['checks'] if c['status'] == 'warn'
    ]


# ------------------------------------------------------------------ 常量
def test_public_constants():
    assert CHECK_IDS == (
        'vna_calibration', 's2p_coverage', 'frequency_grid',
        'instrument_axes', 'reciprocity', 'mechanical_travel',
    )
    assert set(CHECK_TITLES) == set(CHECK_IDS)
    assert all(isinstance(title, str) and title for title in CHECK_TITLES.values())
    assert CALIBRATION_MAX_AGE_DAYS == 7.0


# ------------------------------------------------------- 场景 1：全部通过
def test_all_pass_with_real_sources():
    report = run_prerequisite_checks(good_context())
    assert_report_shape(report)
    assert report['passed'] is True
    assert report['failed_hard'] == []
    assert report['warnings'] == []
    assert all(c['status'] == 'pass' for c in report['checks'])
    assert all(c['hard'] is False for c in report['checks'])
    assert '6/6' in report['summary']


def test_literal_minimal_all_pass_context_is_fully_clean():
    """按需求字面列出的「全部通过」要素构造、不含可选键时也应 6 项全 pass。"""
    ctx = {
        'vna_source': 'real',
        'motion_source': 'real',
        'vna_calibration': {
            'verified': True,
            'method': 'TRL',
            'timestamp': time.time() - 60.0,
        },
        'frequencies_hz': [1e9, 2e9, 3e9, 4e9, 5e9],
        's2p_frequencies_hz': [1e9, 2e9, 3e9, 4e9, 5e9],
        'axes': {
            'x1': {'homed': True, 'feedback_scale': 1.0},
            'x2': {'homed': True, 'feedback_scale': 1.0},
            'z1': {'homed': True, 'feedback_scale': 1.0},
            'z2': {'homed': True, 'feedback_scale': 1.0},
        },
        'reciprocity_confirmed': True,
    }
    report = run_prerequisite_checks(ctx)
    assert_report_shape(report)
    assert report['passed'] is True
    assert report['failed_hard'] == []
    assert report['warnings'] == []
    assert all(c['status'] == 'pass' for c in report['checks'])


# ------------------------------------------------- 场景 2：无校准记录硬失败
def test_missing_calibration_record_fails_hard():
    ctx = good_context()
    del ctx['vna_calibration']
    report = run_prerequisite_checks(ctx)
    assert_report_shape(report)
    assert report['passed'] is False
    assert by_id(report, 'vna_calibration')['status'] == 'fail'
    assert by_id(report, 'vna_calibration')['hard'] is True
    assert 'vna_calibration' in report['failed_hard']
    assert '校准' in by_id(report, 'vna_calibration')['detail']


def test_unverified_calibration_record_fails_hard():
    ctx = good_context()
    ctx['vna_calibration']['verified'] = False
    report = run_prerequisite_checks(ctx)
    assert report['passed'] is False
    assert 'vna_calibration' in report['failed_hard']


# --------------------------------------------- 场景 3：S2P 缺频点硬失败
def test_s2p_missing_frequency_fails_hard():
    ctx = good_context()
    ctx['s2p_frequencies_hz'] = [1e9, 2e9, 4e9, 5e9]  # 缺 3 GHz
    report = run_prerequisite_checks(ctx)
    assert_report_shape(report)
    assert report['passed'] is False
    check = by_id(report, 's2p_coverage')
    assert check['status'] == 'fail'
    assert check['hard'] is True
    assert 's2p_coverage' in report['failed_hard']
    assert '3' in check['detail'] and '绝不插值' in check['detail']


def test_s2p_none_means_no_coverage_check():
    ctx = good_context()
    ctx['s2p_frequencies_hz'] = None
    report = run_prerequisite_checks(ctx)
    assert by_id(report, 's2p_coverage')['status'] == 'pass'
    assert report['passed'] is True


# ---------------------------------------------- 场景 4：校准 8 天仅 warn
def test_calibration_8_days_old_only_warns():
    ctx = good_context()
    ctx['vna_calibration']['timestamp'] = time.time() - 8.0 * 86400.0
    report = run_prerequisite_checks(ctx)
    assert_report_shape(report)
    assert report['passed'] is True
    assert report['failed_hard'] == []
    check = by_id(report, 'vna_calibration')
    assert check['status'] == 'warn'
    assert check['hard'] is False
    assert 'vna_calibration' in report['warnings']
    assert '7' in check['detail'] and '天' in check['detail']


# ------------------------------------------- 场景 5：SOLT@5GHz 仅 warn
def test_solt_at_5ghz_only_warns():
    ctx = good_context()
    ctx['vna_calibration']['method'] = 'SOLT'
    report = run_prerequisite_checks(ctx)
    assert_report_shape(report)
    assert report['passed'] is True
    assert report['failed_hard'] == []
    check = by_id(report, 'vna_calibration')
    assert check['status'] == 'warn'
    assert 'vna_calibration' in report['warnings']
    assert 'TRL' in check['detail'] and 'GHz' in check['detail']


# ------------------------------------------------ 场景 6：未回零硬失败
def test_not_homed_fails_hard():
    ctx = good_context()
    ctx['axes']['x1']['homed'] = False
    report = run_prerequisite_checks(ctx)
    assert_report_shape(report)
    assert report['passed'] is False
    check = by_id(report, 'mechanical_travel')
    assert check['status'] == 'fail'
    assert check['hard'] is True
    assert 'mechanical_travel' in report['failed_hard']
    assert '回零' in check['detail']


def test_inconsistent_feedback_scale_fails_hard():
    ctx = good_context()
    ctx['axes']['z2']['feedback_scale'] = 2.0
    report = run_prerequisite_checks(ctx)
    assert report['passed'] is False
    assert 'mechanical_travel' in report['failed_hard']
    assert '标度' in by_id(report, 'mechanical_travel')['detail']


# ------------------------------------------- 场景 7：不等间隔频率硬失败
def test_uneven_frequency_grid_fails_hard():
    ctx = good_context()
    ctx['frequencies_hz'] = [1e9, 2e9, 4e9]  # 步长 1GHz / 2GHz
    report = run_prerequisite_checks(ctx)
    assert_report_shape(report)
    assert report['passed'] is False
    check = by_id(report, 'frequency_grid')
    assert check['status'] == 'fail'
    assert check['hard'] is True
    assert 'frequency_grid' in report['failed_hard']
    assert '等间隔' in check['detail']


@pytest.mark.parametrize('bad', [
    [1e9, float('nan'), 3e9],
    [float('nan'), 1e9, 2e9],
    [1e9, float('inf'), 3e9],
    [1e9, -2e9, 3e9],
    [3e9, 2e9, 1e9],
    [1e9, 1e9, 2e9],
])
def test_illegal_frequency_grids_fail_hard(bad):
    ctx = good_context()
    ctx['frequencies_hz'] = bad
    report = run_prerequisite_checks(ctx)
    assert by_id(report, 'frequency_grid')['status'] == 'fail'
    assert report['passed'] is False


def test_real_motion_axis_count_mismatch_fails_hard():
    ctx = good_context()
    ctx['axis_count'] = 5  # 与 probes=3 (期望 3+1=4) 不符
    report = run_prerequisite_checks(ctx)
    assert report['passed'] is False
    assert 'instrument_axes' in report['failed_hard']
    assert by_id(report, 'instrument_axes')['status'] == 'fail'


def test_real_motion_axis_count_accepts_the_shared_x_axis():
    """总轴数 = 探针数 + 1 是正确的（X 轴由所有探针共用）。

    回归测试：曾经把 ``axis_count``（总轴数）直接与 ``probes`` 比较，于是
    每台真机都会被判成「轴定义数量与探针数不符」而硬阻止建库。
    """
    ctx = good_context()
    ctx['axis_count'] = 4
    ctx['probes'] = 3
    report = run_prerequisite_checks(ctx)
    assert by_id(report, 'instrument_axes')['status'] == 'pass'
    assert 'instrument_axes' not in report['failed_hard']


# -------------------------------------- 场景 8：模拟来源只 warn 不 fail
def test_mock_sources_only_warn_never_fail():
    ctx = good_context()
    ctx['vna_source'] = 'mock'
    ctx['motion_source'] = 'mock'
    report = run_prerequisite_checks(ctx)
    assert_report_shape(report)
    assert report['passed'] is True
    assert report['failed_hard'] == []
    assert all(c['status'] != 'fail' for c in report['checks'])
    assert report['warnings']  # 至少一条提醒
    assert any(_MOCK_TEXT in c['detail'] for c in report['checks'])


_MOCK_TEXT = '模拟数据不得用于实测'


def test_mock_source_without_any_prerequisites_never_fails():
    """最关键的豁免：模拟流程即便缺校准记录 / 未回零也绝不产生 fail。"""
    report = run_prerequisite_checks({
        'vna_source': 'mock',
        'motion_source': 'mock',
        'axes': {'x1': {'homed': False, 'feedback_scale': None}},
        'frequencies_hz': [1e9, 2e9, 4e9],
        's2p_frequencies_hz': [1e9],
    })
    assert_report_shape(report)
    assert report['passed'] is True
    assert report['failed_hard'] == []
    assert all(c['status'] != 'fail' for c in report['checks'])
    assert any(_MOCK_TEXT in c['detail'] for c in report['checks'])


def test_warn_only_reports_still_pass():
    """互易性未确认、机械行程可疑等仅提醒项不阻断。"""
    ctx = good_context()
    ctx['reciprocity_confirmed'] = False
    ctx['s21'] = None
    ctx['s12'] = None
    ctx['x_limits'] = (50.0, 0.0)  # 申报值可疑但可用
    report = run_prerequisite_checks(ctx)
    assert report['passed'] is True
    assert 'reciprocity' in report['warnings']
    assert by_id(report, 'reciprocity')['status'] == 'warn'


# --------------------------------------- 场景 9：缺省 context 不抛异常
def test_default_context_does_not_raise():
    report = run_prerequisite_checks({})
    assert_report_shape(report)
    assert isinstance(format_report_text(report), str)


@pytest.mark.parametrize('context', [
    None,
    [],
    'not-a-dict',
    {'vna_calibration': 'bad-type', 'frequencies_hz': 'oops', 'axes': 3},
    {'s2p_frequencies_hz': [], 'reciprocity_confirmed': 'yes'},
])
def test_defensive_parsing_never_raises(context):
    report = run_prerequisite_checks(context)
    assert_report_shape(report)


def test_deterministic_except_timestamp():
    ctx = good_context()
    first = run_prerequisite_checks(ctx)
    second = run_prerequisite_checks(ctx)
    first.pop('timestamp')
    second.pop('timestamp')
    assert first == second


# ------------------------------- 场景 10：snapshot 可 JSON 序列化
def test_snapshot_is_json_serializable():
    report = run_prerequisite_checks(good_context())
    snapshot = snapshot_for_metadata(report)
    assert set(snapshot.keys()) == {
        'passed', 'timestamp', 'checks', 'failed_hard', 'summary',
    }
    assert isinstance(snapshot['passed'], bool)
    assert isinstance(snapshot['timestamp'], float)
    for check in snapshot['checks']:
        assert set(check.keys()) == {'id', 'status', 'detail'}
    # allow_nan=False + 不用 default=：能成功即证明无 numpy / 非标准类型。
    text = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
    assert json.loads(text)['passed'] is True


def assert_plain(value):
    """递归确认不含 numpy 标量 / ndarray。"""
    assert not isinstance(value, (np.generic, np.ndarray))
    if isinstance(value, dict):
        for key, item in value.items():
            assert_plain(key)
            assert_plain(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_plain(item)


def test_snapshot_strips_numpy_types():
    ctx = good_context()
    ctx['frequencies_hz'] = np.array([1e9, 2e9, 3e9, 4e9, 5e9])
    ctx['s2p_frequencies_hz'] = np.array([1e9, 2e9, 3e9, 4e9, 5e9])
    ctx['axis_count'] = np.int64(4)
    ctx['probes'] = np.int64(3)
    ctx['vna_calibration']['timestamp'] = np.float64(time.time() - 60.0)
    ctx['s21'] = np.complex128(0.9 + 0.01j)
    ctx['s12'] = np.complex128(0.9 + 0.01j)
    snapshot = snapshot_for_metadata(run_prerequisite_checks(ctx))
    assert_plain(snapshot)
    json.dumps(snapshot, ensure_ascii=False, allow_nan=False)


def test_snapshot_reflects_failed_hard():
    ctx = good_context()
    del ctx['vna_calibration']
    snapshot = snapshot_for_metadata(run_prerequisite_checks(ctx))
    assert snapshot['passed'] is False
    assert 'vna_calibration' in snapshot['failed_hard']
    assert by_id(snapshot, 'vna_calibration')['status'] == 'fail'


# ------------------------------------------- format_report_text 可直接显示
def test_format_report_text_is_multiline_with_symbols_and_reasons():
    report = run_prerequisite_checks(good_context())
    text = format_report_text(report)
    assert isinstance(text, str)
    lines = text.splitlines()
    assert len(lines) >= 7  # 标题 + 6 项 + 汇总
    assert '[√]' in text
    for title in CHECK_TITLES.values():
        assert title in text
    for check in report['checks']:
        assert check['detail'] in text


def test_format_report_text_marks_failures():
    ctx = good_context()
    del ctx['vna_calibration']
    text = format_report_text(run_prerequisite_checks(ctx))
    assert '[×]' in text
    assert '禁止建库' in text

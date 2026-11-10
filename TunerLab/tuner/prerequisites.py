"""F-03 统一前提条件检查单（硬门禁）——纯函数判定引擎。

依据商用调谐器软件的建库前提检查（见同目录设计文档
《调谐器校准库建库方法详解》§7），在建库动作前对 6 类前提做统一判定。

设计约定（供调用方 / 主代理接线时遵循）
--------------------------------------
1. 本模块是**纯函数**：无文件 IO、无网络、无 UI、无全局可变状态。除
   ``timestamp``（``time.time()``）外，相同 context 的判定结果可复现。
2. 返回报告字段：
   ``passed``        —— 无任何 status=='fail' 时为 True；warn 不阻断。
   ``checks``        —— 按 CHECK_IDS 顺序的 6 个检查项。
   ``failed_hard``  —— 本次 status=='fail' 的检查 **id 列表**（按 CHECK_IDS 顺序）。
   ``warnings``      —— 本次 status=='warn' 的检查 **id 列表**（按 CHECK_IDS 顺序）。
   ``timestamp`` / ``summary`` —— 时间戳与中文汇总。
   每个 check = {'id', 'title', 'status', 'detail', 'hard'}，其中
   ``hard`` 表示**本次该检查是否构成硬阻止**，等价于 ``status=='fail'``。
   具体中文原因在 check['detail'] 中，UI 可直接用 format_report_text()。
3. 硬阻止（fail）与仅提醒（warn）的划分严格按需求：
   - 硬阻止：无有效 VNA 校准记录、S2P 频点未覆盖校准频率、频率网格非法、
     轴未回零 / 反馈标度不一致、真实运动来源下轴定义数量与探针数不符。
   - 仅提醒：校准超过 7 天、SOLT 且 >3–4 GHz、互易性未确认、机械行程
     申报值可疑、模拟来源（模拟数据不得用于实测）。
4. 模拟来源豁免：当 ``vna_source=='mock'`` 或 ``motion_source=='mock'`` 时，
   全报告**不产生任何 fail**（本应 fail 的降级为 warn），以免阻断既有的
   模拟 / 自动化流程。真实建库请显式传 ``'real'``；来源未声明时按真实
   建库的「最保守但可解释」原则判定（缺关键前提 -> fail 并给出可操作说明）。
5. 所有 context 键均可选；键缺失 / 类型异常一律防御式解析，不抛异常。
   信息缺失但并非「明确非法数据」时给出 warn（而非静默 pass，也非 fail）。
"""
from __future__ import annotations

import math
import time

# ---------------------------------------------------------------- 公开常量
CHECK_IDS = (
    'vna_calibration',
    's2p_coverage',
    'frequency_grid',
    'instrument_axes',
    'reciprocity',
    'mechanical_travel',
)

CHECK_TITLES = {
    'vna_calibration': 'VNA 校准质量',
    's2p_coverage': 'S2P / 去嵌入文件全频点覆盖',
    'frequency_grid': '频率网格等间隔与合法性',
    'instrument_axes': '仪器与四轴定义、端口映射',
    'reciprocity': '网络互易性',
    'mechanical_travel': '机械行程与回零标度',
}

CALIBRATION_MAX_AGE_DAYS = 7.0

#: 每项检查的**判定方法与通过标准**。UI 直接显示这张表，操作员据此判断
#: 「凭什么说我没过」。每条包含：
#: ``method``  —— 怎么判（看哪个量、怎么比）；
#: ``hard``    —— 什么情况构成**硬阻止**（未通过即禁止建库）；
#: ``warn``    —— 什么情况只是**提醒**（不阻断建库）。
CHECK_CRITERIA = {
    'vna_calibration': {
        'method': '读取 VNA 校准记录的 verified / timestamp / method 三项',
        'hard': 'verified ≠ True（没有已验证的校准记录）',
        'warn': f'校准记录超过 {CALIBRATION_MAX_AGE_DAYS:.0f} 天；'
                '缺 timestamp；采用 SOLT 且最高频率超过 3–4 GHz（建议改用 TRL）',
    },
    's2p_coverage': {
        'method': '把 S2P 文件的频点列表与校准频率逐点比对（容差 1 Hz + 1e-9 相对）',
        'hard': 'S2P 频点表含非法数值；S2P 缺少任何一个校准频点（缺频点直接中止，'
                '绝不插值补偿）',
        'warn': '未提供校准频率列表，无法比对覆盖',
    },
    'frequency_grid': {
        'method': '检查频率列表是否有限、为正、严格递增，且相邻步长一致',
        'hard': '含 NaN/Inf 或非正频率；频点未严格递增；多点时步长不一致（非等间隔）',
        'warn': '未提供或为空的频率列表',
    },
    'instrument_axes': {
        'method': '比较仪器总轴数与探针数（总轴数 = 探针数 + 1，X 轴由所有探针共用）',
        'hard': '总轴数 ≠ 探针数 + 1（仪器与四轴定义不一致）',
        'warn': '模拟运动来源，不做硬性校验',
    },
    'reciprocity': {
        'method': '看 reciprocity_confirmed 标志；否则比较实测 S21 与 S12 的差',
        'hard': '无（此项从不硬阻止）',
        'warn': '|S21 − S12| > 1e-3（链路可能含隔离器，TRL / 去嵌入数学不成立）；'
                '未确认互易性',
    },
    'mechanical_travel': {
        'method': '检查每个轴的 homed 标志与 feedback_scale，并核对申报的 X/Z 行程',
        'hard': '存在未回零的轴；某轴缺少有效 feedback_scale；各轴反馈标度不一致',
        'warn': '行程申报值可疑（low ≥ high 或 low < 0）；未提供行程限值或轴状态',
    },
}

# 内部阈值
_DAY_SECONDS = 86400.0
_SOLT_WARN_HZ = 3.0e9          # SOLT 在 3–4 GHz 以上只警告，取区间下限最保守
_MOCK_NOTE = '模拟数据不得用于实测'


# ------------------------------------------------------------ 防御式小工具
def _to_float(value):
    """转成有限 float；无法转换 / 非有限 / 布尔一律返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _to_int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _to_complex(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = complex(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(number.real) and math.isfinite(number.imag)):
        return None
    return number


def _to_bool(value, default=False):
    """兼容 bool / numpy.bool_ / 0-1 / 常见真假字符串。"""
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ('false', '0', 'no', 'n', 'off', ''):
            return False
        if text in ('true', '1', 'yes', 'y', 'on'):
            return True
        return default
    try:
        return bool(value)
    except Exception:  # pragma: no cover - 极端自定义对象兜底
        return default


def _coerce_freq_list(raw):
    """把频率序列转成 list[float]。

    返回 (values, error)。error ∈ {None, 'missing', 'empty', 'invalid',
    'nonfinite'}。None 表示成功。
    """
    if raw is None:
        return None, 'missing'
    if isinstance(raw, (str, bytes)):
        return None, 'invalid'
    try:
        seq = list(raw)
    except TypeError:
        return None, 'invalid'
    if len(seq) == 0:
        return [], 'empty'
    values = []
    for item in seq:
        number = _to_float(item)
        if number is None:
            return values, 'nonfinite'
        values.append(number)
    return values, None


def _close(a, b, rel=1e-9, abs_=1.0):
    return math.isclose(a, b, rel_tol=rel, abs_tol=abs_)


# ---------------------------------------------------------------- 规范化
def _normalize(context):
    """把任意 context 收敛成引擎内部使用的只读字段集合，绝不抛异常。"""
    if not isinstance(context, dict):
        context = {}

    cal = context.get('vna_calibration')
    if not isinstance(cal, dict):
        cal = {}

    def source(key):
        value = context.get(key)
        if isinstance(value, str):
            return value.strip().lower()
        return None

    freqs, freq_err = _coerce_freq_list(context.get('frequencies_hz'))
    s2p_raw = context.get('s2p_frequencies_hz')
    s2p_freqs, s2p_err = _coerce_freq_list(s2p_raw)

    axes_raw = context.get('axes')
    axes = {}
    axes_present = isinstance(axes_raw, dict) and len(axes_raw) > 0
    if isinstance(axes_raw, dict):
        for name, info in axes_raw.items():
            if isinstance(info, dict):
                axes[str(name)] = {
                    'homed': _to_bool(info.get('homed'), False),
                    'feedback_scale': _to_float(info.get('feedback_scale')),
                }
            else:
                axes[str(name)] = {'homed': False, 'feedback_scale': None}

    def limits(key):
        raw = context.get(key)
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            return (_to_float(raw[0]), _to_float(raw[1]))
        return None

    return {
        'cal': cal,
        'vna_source': source('vna_source'),
        'motion_source': source('motion_source'),
        'freqs': freqs,
        'freq_err': freq_err,
        's2p_freqs': s2p_freqs,
        's2p_err': s2p_err,
        's2p_is_none': s2p_raw is None,
        's2p_path': str(context.get('s2p_path') or ''),
        'axes': axes,
        'axes_present': axes_present,
        'axis_count': _to_int(context.get('axis_count')),
        'probes': _to_int(context.get('probes')),
        'x_limits': limits('x_limits'),
        'z_limits': limits('z_limits'),
        'reciprocity_confirmed': _to_bool(context.get('reciprocity_confirmed'), False),
        's21': _to_complex(context.get('s21')),
        's12': _to_complex(context.get('s12')),
    }


# ------------------------------------------------------------- 六类检查
def _check_vna_calibration(c, now):
    cal = c['cal']
    if not _to_bool(cal.get('verified'), False):
        return 'fail', (
            '未提供有效的 VNA 校准记录（vna_calibration 缺省或 verified≠True）。'
            '建库质量主要取决于 VNA 校准，务必 meticulously verify；无校准记录会产生'
            '整库系统性偏差，且无法靠增加点数补救。请先完成并验证校准。'
            '若为模拟/自动化流程，请显式设置 vna_source="mock"。'
        )

    method = str(cal.get('method') or '').strip().upper()
    pass_notes = []
    warn_notes = []
    cal_freqs = c['freqs']
    max_freq = max(cal_freqs) if cal_freqs else None

    stamp = _to_float(cal.get('timestamp'))
    if stamp is None:
        warn_notes.append('校准记录缺少有效 timestamp，无法判断是否超过 7 天，建议补全时间戳')
    else:
        age_days = (now - stamp) / _DAY_SECONDS
        if age_days > CALIBRATION_MAX_AGE_DAYS:
            warn_notes.append(
                f'校准记录已过 {age_days:.1f} 天（阈值 {CALIBRATION_MAX_AGE_DAYS:.0f} 天），'
                'VNA 因漂移需频繁重做校准，建议重新校准后再建库'
            )
        elif age_days < -0.01:
            warn_notes.append('校准记录时间戳晚于当前系统时间，请核对时钟')
        else:
            pass_notes.append(f'校准记录新鲜（{age_days:.2f} 天内）')

    if method == 'TRL':
        pass_notes.append('采用 TRL 校准（官方推荐，质量最佳）')
    elif method == 'SOLT':
        if max_freq is not None and max_freq > _SOLT_WARN_HZ:
            warn_notes.append(
                f'最高频率 {max_freq / 1e9:.2f} GHz 已超过 3–4 GHz，'
                'SOLT 在此频段得不到可接受的 tuner 数据，强烈建议改用 TRL'
            )
        elif max_freq is None:
            warn_notes.append(
                '采用 SOLT 校准但未提供频率列表，无法确认是否处于 >3–4 GHz 风险频段，建议改用 TRL'
            )
        else:
            pass_notes.append('采用 SOLT 校准，频率低于 3 GHz，未触发高频警告')
    elif method:
        pass_notes.append(f'校准方法为 {method}（推荐优先使用 TRL）')
    else:
        warn_notes.append('校准记录未声明 method，无法确认 TRL 优先策略，建议补全')

    if warn_notes:
        return 'warn', '；'.join(pass_notes + warn_notes)
    return 'pass', '；'.join(pass_notes) or '校准记录有效且新鲜'


def _check_s2p_coverage(c):
    path = c['s2p_path'] or '（未填写路径）'
    if c['s2p_is_none']:
        return 'pass', (
            '未挂载 S2P / 去嵌入文件（s2p_frequencies_hz=None），按约定不视为缺频点；'
            '若实际链路使用去嵌入，请提供其频点列表以校验覆盖。'
        )

    s2p_freqs = c['s2p_freqs']
    if s2p_freqs is None or c['s2p_err'] in ('invalid', 'nonfinite'):
        return 'fail', (
            f'S2P 频点列表含非数值或非有限值，无法校验覆盖。请修正 {path} 的频率列：'
            '所有 S2P 必须含全部校准频率，缺频点直接中止、绝不插值补偿。'
        )

    cal_freqs = c['freqs']
    if not cal_freqs:
        return 'warn', (
            '未提供校准频率列表（frequencies_hz），无法校验 S2P 频点覆盖；'
            '建库前必须提供等间隔频率网格并确认 S2P 含全部频点。'
        )

    missing = [
        f for f in cal_freqs
        if not any(_close(f, s) for s in s2p_freqs)
    ]
    if missing:
        preview = '、'.join(f'{m / 1e9:.4g}GHz' for m in missing[:5])
        more = f' 等共 {len(missing)} 个' if len(missing) > 5 else ''
        return 'fail', (
            f'S2P / 去嵌入文件（{path}）缺少校准频点 {preview}{more}。'
            '按建库硬约束「缺频点直接中止标定，绝不插值补偿」，'
            '必须补齐含全部校准频率的 S2P 文件后重新检查。'
        )
    return 'pass', (
        f'S2P / 去嵌入文件（{path}）已覆盖全部 {len(cal_freqs)} 个校准频点，'
        '无缺频点，未做任何插值补偿。'
    )


def _check_frequency_grid(c):
    err = c['freq_err']
    freqs = c['freqs']
    if err in ('invalid', 'nonfinite'):
        return 'fail', (
            '频率列表含非数值或非有限值（NaN/Inf），频率网格非法，禁止建库。'
            '请提供有限的正频率值。'
        )
    if err in ('missing', 'empty') or not freqs:
        return 'warn', (
            '未提供或为空的频率列表，无法校验等间隔与合法性；'
            '建库前必须提供有限、为正、严格递增且同表内等间隔的频率网格。'
        )
    for value in freqs:
        if value <= 0:
            return 'fail', (
                f'频率网格非法：存在非正频率 {value:g} Hz，所有频点必须 > 0。'
            )
    for a, b in zip(freqs, freqs[1:]):
        if not (b > a):
            return 'fail', (
                f'频率网格非法：频点未严格递增（{a:g} → {b:g} Hz），'
                '必须按升序排列且互不相等。'
            )
    if len(freqs) >= 2:
        diffs = [b - a for a, b in zip(freqs, freqs[1:])]
        step = diffs[0]
        for diff in diffs[1:]:
            if not _close(diff, step):
                return 'fail', (
                    '频率网格非法：多频点频率必须等间隔，但相邻步长不一致'
                    f'（{step:g} Hz 与 {diff:g} Hz）。同表内频率列表必须等间隔，否则无法驱动建库。'
                )
        return 'pass', (
            f'频率网格合法：{len(freqs)} 个频点，有限、为正、严格递增、'
            f'等间隔（步长 {step:g} Hz）。'
        )
    return 'pass', '频率网格合法：单频点，无需校验等间隔，且有限、为正。'


def _check_instrument_axes(c):
    port_note = (
        '端口映射按默认约定：VNA Port1→tuner 输入、Port2→tuner 输出（1-2）；'
        '若实际接反需事后用 Reverse 修正。'
    )
    motion = c['motion_source']
    if motion == 'mock':
        return 'warn', (
            '当前为模拟运动来源（motion_source=mock），四轴定义 / 端口映射不做硬性校验；'
            f'{_MOCK_NOTE}。{port_note}'
        )

    axis_count, probes = c['axis_count'], c['probes']
    if axis_count is not None and probes is not None:
        # ``probes`` counts the *probe* (Z) axes; the X axis is shared by every
        # probe, so the total axis count is always ``probes + 1``
        # (``TunerConfig.axes`` == ('X', 'Z1', …, 'Z{probes}') and
        # ``TunerSettings.probe_count`` == ``axis_count - 1``).  Comparing the
        # two numbers directly would report a mismatch on every real machine.
        expected = probes + 1
        if axis_count != expected:
            return 'fail', (
                f'轴定义数量（{axis_count}）与探针数（{probes}）不符：'
                f'共用一个 X 轴时总轴数应为 探针数+1 = {expected}。'
                '仪器与四轴定义不一致，无法启动校准。请核对 tuner 定义'
                '（S/N、Type、Mode、Step Size、Xmax/Ymax、Controller）与探针配置。'
                f'{port_note}'
            )
        return 'pass', (
            f'四轴定义与探针数一致（总轴数={axis_count} = 探针数 {probes} + 1 个共用 X 轴），'
            f'仪器已定义。{port_note}'
        )

    missing = []
    if axis_count is None:
        missing.append('axis_count')
    if probes is None:
        missing.append('probes')
    # 可选键缺失属「信息不足」，不是 warn 清单里的提醒项，也不是硬阻止；
    # 给 pass 并显式说明，保证按最小合规 context 调用时 6 项全通过。
    return 'pass', (
        f'未申报 {"、".join(missing)}，本次按默认四轴定义与端口映射处理，'
        f'未发现「轴定义数量≠探针数」冲突；真实建库建议补全 tuner 定义以显式核对。{port_note}'
    )


def _check_reciprocity(c):
    if c['reciprocity_confirmed']:
        return 'pass', (
            '已确认被校准 / 去嵌入链路不含隔离器，网络互易（S21≈S12）成立，'
            'In-Situ 去嵌入数学有效。'
        )
    s21, s12 = c['s21'], c['s12']
    if s21 is not None and s12 is not None:
        delta = abs(s21 - s12)
        if delta <= 1e-3:
            return 'pass', (
                f'实测 S21≈S12（|Δ|={delta:.3g}），链路互易，未发现隔离器迹象。'
            )
        return 'warn', (
            f'S21 与 S12 偏差较大（|Δ|={delta:.3g}），链路可能含隔离器等非互易器件，'
            'TRL / 去嵌入数学不成立。请确认无隔离器或显式设置 '
            'reciprocity_confirmed=True；此项仅提醒、不阻断。'
        )
    return 'warn', (
        '网络互易性未确认（reciprocity_confirmed≠True 且未提供 S21/S12）。'
        '请确认被校准链路不含隔离器；此项仅提醒、不阻断。'
    )


def _check_mechanical_travel(c):
    motion = c['motion_source']
    axes = c['axes']
    pass_notes = []
    warn_notes = []

    if not c['axes_present']:
        warn_notes.append(
            '未提供轴状态（axes），无法确认各轴已回零及 UNITS / 反馈标度一致；'
            '真实建库前必须补全并回零'
        )
    else:
        not_homed = [name for name, info in axes.items() if not info['homed']]
        if not_homed:
            return 'fail', (
                '存在未回零的轴：' + '、'.join(not_homed) + '。'
                '机械未回零时位置标度无效，属硬阻止；必须先执行回零（Homing）再建库。'
            )
        pass_notes.append(f'全部 {len(axes)} 个轴均已回零')

        scales = [info['feedback_scale'] for info in axes.values()]
        if any(scale is None for scale in scales):
            return 'fail', (
                '部分轴缺少有效 feedback_scale（UNITS / 反馈标度），'
                '标度不一致属硬阻止；请为所有轴填写一致且有限的反馈标度。'
            )
        base = scales[0]
        if any(not _close(scale, base, rel=1e-9, abs_=1e-12) for scale in scales[1:]):
            preview = '、'.join(f'{scale:g}' for scale in scales[:4])
            return 'fail', (
                f'各轴 UNITS / 反馈标度不一致（{preview}），回零标度不统一属硬阻止；'
                '请统一为相同标度。'
            )
        pass_notes.append(f'反馈标度一致（{base:g}）')

    for label, limit in (('X', c['x_limits']), ('Z', c['z_limits'])):
        if limit is None:
            # 未提供行程限值属信息缺失，不是「申报值可疑」，不产生提醒。
            pass_notes.append(f'未提供 {label} 行程限值，本次不核对 Xmax/Ymax')
            continue
        low, high = limit
        if low is None or high is None or not (high > low) or low < 0:
            warn_notes.append(
                f'{label} 行程申报值可疑（{low}, {high}），请核对 Xmax/Ymax；'
                '此项仅提醒、但需确认可用'
            )
        else:
            pass_notes.append(f'{label} 行程 [{low:g}, {high:g}] 合法')

    if motion == 'mock':
        warn_notes.append(
            f'当前为模拟运动来源（motion_source=mock），{_MOCK_NOTE}'
        )

    if warn_notes:
        return 'warn', '；'.join(pass_notes + warn_notes)
    return 'pass', '；'.join(pass_notes) or '机械行程与回零标度正常'


# ------------------------------------------------------------- 来源后处理
def _apply_source_policy(checks, c):
    """模拟来源：明确提示 + 全报告不产生 fail（硬阻止降级为 warn）。"""
    by_id = {check['id']: check for check in checks}
    vna_mock = c['vna_source'] == 'mock'
    motion_mock = c['motion_source'] == 'mock'

    if vna_mock:
        check = by_id['vna_calibration']
        if check['status'] == 'pass':
            check['status'] = 'warn'
        check['detail'] += (
            f'；检测到模拟矢网（vna_source=mock），{_MOCK_NOTE}，'
            '即便该项显示通过也不代表真实校准'
        )
    if motion_mock:
        check = by_id['mechanical_travel']
        if check['status'] == 'pass':
            check['status'] = 'warn'
        check['detail'] += (
            f'；检测到模拟运动（motion_source=mock），{_MOCK_NOTE}'
        )

    if vna_mock or motion_mock:
        for check in checks:
            if check['status'] == 'fail':
                check['status'] = 'warn'
                check['detail'] += (
                    f'；【模拟来源】本项硬阻止已降级为提醒，{_MOCK_NOTE}'
                )


# ------------------------------------------------------------------ 汇总
def _make_summary(checks):
    failed = [c['id'] for c in checks if c['status'] == 'fail']
    warned = [c['id'] for c in checks if c['status'] == 'warn']
    if failed:
        titles = '、'.join(CHECK_TITLES[item] for item in failed)
        return (
            f'前提检查未通过：{len(failed)} 项硬阻止（{titles}），'
            f'{len(warned)} 项提醒。存在硬阻止，禁止建库；'
            '请先按各项说明整改后重新检查。'
        )
    if warned:
        titles = '、'.join(CHECK_TITLES[item] for item in warned)
        return (
            f'前提检查通过（有提醒）：0 项硬阻止，{len(warned)} 项提醒（{titles}）。'
            '提醒不阻断建库，但请确认后再继续。'
        )
    return '前提检查全部通过（6/6），无硬阻止、无提醒，允许建库。'


# -------------------------------------------------------------- 公开接口
def run_prerequisite_checks(context: dict) -> dict:
    """对 6 类前提做统一判定，返回硬门禁报告。所有 context 键可选。"""
    c = _normalize(context)
    now = time.time()

    results = (
        _check_vna_calibration(c, now),
        _check_s2p_coverage(c),
        _check_frequency_grid(c),
        _check_instrument_axes(c),
        _check_reciprocity(c),
        _check_mechanical_travel(c),
    )
    checks = [
        {
            'id': check_id,
            'title': CHECK_TITLES[check_id],
            'status': status,
            'detail': detail,
            'hard': False,  # 在来源后处理之后统一重算
        }
        for check_id, (status, detail) in zip(CHECK_IDS, results)
    ]

    _apply_source_policy(checks, c)

    for check in checks:
        check['hard'] = check['status'] == 'fail'

    failed_hard = [check['id'] for check in checks if check['status'] == 'fail']
    warnings = [check['id'] for check in checks if check['status'] == 'warn']
    return {
        'passed': not failed_hard,
        'checks': checks,
        'failed_hard': failed_hard,
        'warnings': warnings,
        'timestamp': float(now),
        'summary': _make_summary(checks),
    }


def criteria_report() -> dict:
    """返回 6 项检查的判定方法与通过标准。

    供 UI 显示「凭什么说我没过」：每项给出怎么判、什么算硬阻止、什么只是
    提醒。纯静态数据，不依赖任何 context。
    """
    return {
        'items': [
            {
                'id': check_id,
                'title': CHECK_TITLES[check_id],
                'method': CHECK_CRITERIA[check_id]['method'],
                'hard': CHECK_CRITERIA[check_id]['hard'],
                'warn': CHECK_CRITERIA[check_id]['warn'],
            }
            for check_id in CHECK_IDS
        ],
    }


def format_criteria_text() -> str:
    """把判定标准渲染成可直接显示的中文多行文本。"""
    lines = ['【前提条件检查 · 判定方法与标准】']
    for check_id in CHECK_IDS:
        item = CHECK_CRITERIA[check_id]
        lines.append(f'■ {CHECK_TITLES[check_id]}')
        lines.append(f'  判定方法：{item["method"]}')
        lines.append(f'  硬阻止（不过则禁止建库）：{item["hard"]}')
        lines.append(f'  仅提醒（不阻断）：{item["warn"]}')
    lines.append('—— 判定依据商用调谐器软件的建库前提检查；'
                 '模拟来源（mock）时硬阻止一律降级为提醒。')
    return '\n'.join(lines)


def snapshot_for_metadata(report: dict) -> dict:
    """生成适合写入 HDF5 库头的精简快照，保证纯 Python 类型、JSON 可序列化。"""
    return {
        'passed': bool(report.get('passed', False)),
        'timestamp': float(report.get('timestamp', 0.0)),
        'checks': [
            {
                'id': str(check.get('id', '')),
                'status': str(check.get('status', '')),
                'detail': str(check.get('detail', '')),
            }
            for check in report.get('checks', [])
        ],
        'failed_hard': [str(item) for item in report.get('failed_hard', [])],
        'summary': str(report.get('summary', '')),
    }


def format_report_text(report: dict) -> str:
    """给 UI 直接显示的多行中文文本：每行一项，含状态符号与原因。"""
    symbols = {'pass': '√', 'warn': '!', 'fail': '×'}
    checks = report.get('checks', [])
    n_fail = sum(1 for c in checks if c.get('status') == 'fail')
    n_warn = sum(1 for c in checks if c.get('status') == 'warn')
    passed = bool(report.get('passed', False))
    verdict = '通过 · 允许建库' if passed else '未通过 · 禁止建库'

    lines = [f'【P0 前提检查单】{verdict}（硬阻止 {n_fail} / 提醒 {n_warn}）']
    for check in checks:
        symbol = symbols.get(check.get('status'), '?')
        title = check.get('title') or check.get('id', '')
        detail = check.get('detail', '')
        lines.append(f'[{symbol}] {title}：{detail}')
    lines.append('—— ' + str(report.get('summary', '')))
    return '\n'.join(lines)

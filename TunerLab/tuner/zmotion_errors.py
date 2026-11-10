"""Human-readable decoding of Zmotion controller diagnostics.

Three independent error sources exist and all of them are surfaced here:

* **Vendor API layer** — every ``ZAux_*`` call returns a numeric code.  The
  DLL-side codes are declared in ``zauxdll2.h`` (``ERR_AUX_OFFSET`` = 30000).
* **Controller layer** — the controller reports its own error number through
  the Basic command channel, and per-axis state through the ``AXISSTATUS`` and
  ``AXIS_STOPREASON`` axis parameters.
* **Per-axis bit fields** — ``AXISSTATUS`` is a bit field whose bits are
  documented by the vendor.  ``AXIS_STOPREASON`` latches the same bit
  information about the last stop, so both are decoded with one table.

The tables below are transcribed from the vendor documentation:

* ``AXISSTATUS`` bit meanings: 正运动 "运动控制器轴回零的配置与实现" (官方技术文),
* controller error numbers: 正运动 "错误码整理列表",
* DLL codes: ``vendor/zmotion/zauxdll2.h``.

Nothing here guesses.  An unknown code is reported as unknown rather than being
mapped onto a plausible-sounding message.
"""
from __future__ import annotations

__all__ = [
    'AXIS_STATUS_BITS',
    'DLL_ERRORS',
    'CONTROLLER_ERRORS',
    'decode_axis_status',
    'describe_axis_status',
    'describe_stop_reason',
    'describe_dll_error',
    'describe_controller_error',
    'describe_error_code',
    'has_fault',
]

# --------------------------------------------------------------------------- #
# AXISSTATUS / AXIS_STOPREASON bit table
# --------------------------------------------------------------------------- #
#: ``AXISSTATUS`` bit index -> description.  Bit 0 is unused by the vendor.
#: ``AXIS_STOPREASON`` latches the same information, so one table serves both.
AXIS_STATUS_BITS: tuple[tuple[int, str], ...] = (
    (1, '随动误差超限告警'),
    (2, '与远程轴通讯出错'),
    (3, '远程驱动器报错'),
    (4, '正向硬限位触发'),
    (5, '反向硬限位触发'),
    (6, '找原点中'),
    (7, 'HOLD 速度保持信号输入'),
    (8, '随动误差超限出错'),
    (9, '超过正向软限位'),
    (10, '超过负向软限位'),
    (11, 'CANCEL 执行中'),
    (12, '脉冲频率超过 MAX_SPEED 限制'),
    (14, '机械手指令坐标错误'),
    (18, '电源异常'),
    (19, '精准输出缓冲溢出'),
    (21, '运动中触发特殊运动指令失败'),
    (22, '告警信号输入'),
    (23, '轴进入了暂停状态'),
)

#: Bits that mean "something is wrong" rather than "the axis is busy".
_FAULT_BITS = frozenset({
    1, 2, 3, 4, 5, 8, 9, 10, 12, 14, 18, 19, 21, 22, 23,
})

#: Bits that only describe the current activity, not a fault.
_BUSY_BITS = frozenset({6, 7, 11})


# --------------------------------------------------------------------------- #
# Vendor API (DLL) error codes — see vendor/zmotion/zauxdll2.h
# --------------------------------------------------------------------------- #
DLL_ERRORS: dict[int, str] = {
    0: '成功',
    1: '控制器无此命令或参数错误',
    2: '控制器内部错误',
    3: '参数错误',
    4: '控制器状态不允许该操作',
    5: '通道号或轴号错误',
    6: '找不到控制器（地址错误或广播未应答）',
    30000: '控制器无应答',
    30001: '应答错误',
    30002: '参数错误',
    30003: '功能不支持',
    30004: '文件错误',
    30005: '操作系统错误',
}


# --------------------------------------------------------------------------- #
# Controller-side error numbers (正运动 错误码整理列表)
# --------------------------------------------------------------------------- #
#: The full table runs to several hundred entries; the ones below are the
#: codes that a motion-control workflow can realistically hit.  Anything else is
#: reported as "未知错误码" with the raw number preserved.
CONTROLLER_ERRORS: dict[int, str] = {
    222: '文件不存在',
    223: '密码保护限制',
    224: '密码保护限制 2',
    260: '硬件错误',
    261: '磁盘没有格式化',
    262: 'RTC 错误',
    263: 'NORFLASH 错误',
    264: 'RAM 错误',
    265: 'NANDFLASH 错误',
    266: 'U 盘错误',
    267: 'FPGA 错误',
    268: '以太网硬件错误',
    271: '备份电源错误',
    272: '子卡不存在',
    273: '文件丢失',
    274: '系统文件错误',
    275: '无主控，子卡上产生',
    276: '程序文件校验错误',
    277: '程序文件错误导致不启动',
    278: 'ZAR 校验 APPPASS 出错',
    279: 'ZAR 校验 ID 出错',
    280: 'BAS 文件超过最大数量',
    281: '子卡 ID 冲突，或多主冲突',
    282: '不支持的功能',
    284: 'ZAR 与控制器不匹配',
    285: '图片文件错误',
    286: '字体文件错误',
    1000: '运动模块返回错误偏移',
    1002: '无运动缓冲',
    1004: '从轴运动中',
    1005: '不支持的运动功能',
    1006: '圆弧位置错误',
    1007: '椭圆 AB 参数错误',
    1008: '运动模块输入参数错误',
    1009: '运动中，无法操作',
    1010: '暂停等重复操作',
    1011: 'IDLE 无法做暂停等操作',
    1012: '当前运动不支持暂停',
    1013: '找不到暂停点',
    1014: 'ATYPE 不支持',
    1015: 'ZCAN 的 ATYPE 冲突',
    1016: '轴不支持的功能',
    2000: 'ZBASIC 模块错误偏移',
    2021: '手动停止',
    2022: '因其他任务错误导致本任务停止',
    2023: '试图修改只读状态参数',
    2024: '数组越界',
    2025: '变量数超过控制器规格',
    2026: '数组数超过控制器规格',
    2027: '数组空间超过控制器规格',
    2028: 'SUB 数超过控制器规格',
    2029: '标识符命名错误',
    2030: '标识符命名过长',
    2031: '没有右括号',
    2032: '不认识的字符',
    2033: '表达式中碰到不认识的名称',
    2043: '不认识的命令标识符',
    2044: '堆栈溢出',
    2045: '数学表达式太复杂',
    2046: '没有找到结束引用标号',
    2047: '指令没有返回值，不能用于表达式计算',
    2048: '函数必须返回值',
    2049: '特殊指令必须单独一行',
    2050: '参数或数组需要索引',
    2051: '变量不能使用索引',
    2052: '数组重定义且长度不一致',
    2053: '数组定义长度参数错误',
    2054: '标识符已经定义为 SUB 过程',
    2055: '标识符已经定义为参数',
    20008: '找不到控制器（地址错误或网络未应答）',
}


# --------------------------------------------------------------------------- #
# Decoding helpers
# --------------------------------------------------------------------------- #
def decode_axis_status(value: int) -> list[str]:
    """Return the descriptions of every set bit of an ``AXISSTATUS`` value.

    An empty list means the axis reports no status bit at all, which the
    controller uses to mean "everything is normal".
    """
    number = int(value)
    if number == 0:
        return []
    if number < 0:
        raise ValueError('axis status must not be negative')
    flags: list[str] = []
    for bit, text in AXIS_STATUS_BITS:
        if number & (1 << bit):
            flags.append(f'{text} (bit{bit})')
    # Bits the vendor does not document are still reported, so an unknown
    # condition is never silently swallowed.
    known = 0
    for bit, _ in AXIS_STATUS_BITS:
        known |= 1 << bit
    unknown = number & ~known
    if unknown:
        for bit in range(0, 32):
            if unknown & (1 << bit):
                flags.append(f'未定义状态位 bit{bit}')
    return flags


def has_fault(value: int) -> bool:
    """``True`` when the status word contains a fault bit (not just "busy")."""
    number = int(value)
    for bit in _FAULT_BITS:
        if number & (1 << bit):
            return True
    known = 0
    for bit, _ in AXIS_STATUS_BITS:
        known |= 1 << bit
    return bool(number & ~known)


def describe_axis_status(value: int) -> str:
    """One-line summary of an ``AXISSTATUS`` value."""
    flags = decode_axis_status(value)
    if not flags:
        return '正常'
    return '、'.join(flags)


def describe_stop_reason(value: int) -> str:
    """One-line summary of an ``AXIS_STOPREASON`` value.

    The vendor latches the ``AXISSTATUS`` information into the stop reason, so
    the same bit table applies.
    """
    flags = decode_axis_status(value)
    if not flags:
        return '无锁存停止原因'
    return '、'.join(flags)


def describe_dll_error(code: int | None) -> str:
    """Describe a vendor API error code."""
    if code is None:
        return '无错误码'
    number = int(code)
    text = DLL_ERRORS.get(number)
    if text is not None:
        return f'{text} (code {number})'
    return f'未知 DLL 错误码 {number}'


def describe_controller_error(code: int | None) -> str:
    """Describe a controller-reported error number."""
    if code is None:
        return '未读取到控制器错误码'
    number = int(code)
    if number == 0:
        return '控制器无错误 (code 0)'
    text = CONTROLLER_ERRORS.get(number)
    if text is not None:
        return f'{text} (code {number})'
    return f'未知控制器错误码 {number}（请查阅 ZMotion 错误码表）'


def describe_error_code(code: int | None) -> str:
    """Describe an error number from whichever layer produced it.

    DLL codes (>= 30000) and controller codes live in different namespaces, so
    the more specific table is consulted first.
    """
    if code is None:
        return '无错误码'
    number = int(code)
    if number in CONTROLLER_ERRORS:
        return describe_controller_error(number)
    if number in DLL_ERRORS:
        return describe_dll_error(number)
    if number >= 30000:
        return describe_dll_error(number)
    return describe_controller_error(number)

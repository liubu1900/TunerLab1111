#!/usr/bin/env python
"""真机 SCPI 方言探测器（不启动 TunerLab 界面，直接连矢网）。

用途
----
当「连接并配置」报 ``failed to define measurement S11``、仪器面板回
``-113 Undefined header`` 时，用本脚本一次性查清：

1. ``*IDN?`` 的真实型号与固件版本（回答「是不是版本太旧」）；
2. 这台仪器**认哪些**测量定义 / 选择 / 取数命令（回答「该用哪套方言」）；
3. 基础扫频、功率、IFBW、触发命令是否可用。

脚本只发查询和无害的配置命令，**不发 ``*RST``**，但探测「测量定义」
时会在仪器上创建名为 ``TL_probe_*`` 的测量 / 改动迹线 1 的参数，
跑完请按需在前面板恢复。每条探测前都发 ``*CLS`` 清错误队列，避免
上一条的 -113 残留影响判定。

用法（PowerShell）
------------------
.. code-block:: powershell

    cd D:\\Tuner
    .\\.venv\\Scripts\\python verification\\probe_vna_scpi.py --host 192.168.2.111
    .\\.venv\\Scripts\\python verification\\probe_vna_scpi.py --host 192.168.2.111 --port 5025 --json
    .\\.venv\\Scripts\\python verification\\probe_vna_scpi.py --interface gpib --resource GPIB0::17::INSTR

把输出的 ``*IDN?`` 一行和「测量结果」表发回来，即可据此新增专用 profile。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许从源码树直接运行（venv 里已 editable-install 时可省略）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'TunerLab'))

from tuner.vna_drivers import SocketSCPI, VISASCPI  # noqa: E402

NO_ERROR = ('+0', '0,')

#: 各厂商创建「命名测量」的写法，按软件当前回退顺序排列。
DEFINE_CANDIDATES = [
    (":CALC1:PAR:DEF 'TL_probe_S11','S11'", '当前驱动 / ENA 两参数写法'),
    (":CALC1:PAR:SDEF 'TL_probe_S11','S11'", 'R&S ZNB/ZVA/ZND (SDEFine)'),
    (":CALC1:PAR:DEF:EXT 'TL_probe_S11','S11'", 'Keysight PNA / PNA-X (DEFine:EXTended)'),
    (":CALC1:PAR:EXT 'TL_probe_S11','S11'", 'Keysight PNA 简写 (PAR:EXT)'),
    (":CALC1:MEAS:DEF 'S11'", '新版 PNA (MEASure:DEFine，无命名)'),
    (":CALC1:PAR1:DEF S11", '老式 ENA：按迹线号设置参数'),
]

#: 其余关键命令（查询类会额外打印返回值）。
OTHER_COMMANDS = [
    (':FORMat:DATA ASCii', 'ASCII 数据格式'),
    (':SENS1:SWE:TYPE LIN', '线性扫频'),
    (':INIT1:CONT OFF', '关闭连续触发'),
    (':SENS1:FREQ:STAR 1e9', '起始频率'),
    (':SENS1:FREQ:STOP 2e9', '终止频率'),
    (':SENS1:SWE:POIN 11', '扫频点数'),
    (':SOUR1:POW:LEV -10', '功率（ENA 风格）'),
    (':SOUR1:POW:LEV:IMM:AMPL -10', '功率（PNA-X 风格）'),
    (':SENS1:BAND 1000', '中频带宽'),
    (":CALC1:PAR:SEL 'TL_probe_S11'", '按名字选择测量（PNA/ENA 新）'),
    (':CALC1:PAR1:SEL', '按迹线号选择（老 ENA，可能无此命令）'),
    (':CALC1:DATA? SDATA', '取数 SDATA（ENA/PNA）'),
    (':CALC1:DATA? SDAT', '取数 SDAT（R&S）'),
    (':CALC1:TRAC1:DATA? SDATA', '取数 TRACe1（老 ENA）'),
    (':CALC1:MEAS1:DATA:SDATA?', '取数 MEASure（新版 PNA）'),
    (':INIT1:IMM', '单次触发'),
    (':INIT:IMM', '单次触发（无通道号）'),
]


def make_transport(args):
    if args.interface == 'gpib':
        return VISASCPI(args.resource, args.timeout)
    return SocketSCPI(args.host, args.port, args.timeout)


def error_of(tr) -> str:
    """读一条错误队列内容；无响应时明确标注，不误判为成功。"""
    try:
        resp = tr.query(':SYST:ERR?').strip()
    except Exception as exc:  # noqa: BLE001
        return f'<查询异常: {exc}>'
    return resp or '<无响应>'


def is_ok(resp: str) -> bool:
    return resp.startswith(NO_ERROR)


def probe_write(tr, cmd: str) -> tuple[str, str]:
    """发一条设置命令，返回 (错误队列内容, 判定)。"""
    tr.write('*CLS')
    try:
        tr.write(cmd)
    except Exception as exc:  # noqa: BLE001
        return f'<发送异常: {exc}>', 'ERROR'
    err = error_of(tr)
    tr.write('*CLS')
    return err, 'OK' if is_ok(err) else 'REJECT'


def probe_query(tr, cmd: str) -> tuple[str, str, str]:
    """发一条查询命令，返回 (返回值摘要, 错误队列内容, 判定)。

    判定要区分「返回了数据」「返回空」「错误队列报错」，否则取数助记符
    是否可用会看不出来。注意查询命令必须真的读回响应：只写不读会把响应
    留在仪器输出缓冲里，下一条 ``:SYST:ERR?`` 读到的就是残留数据。
    """
    tr.write('*CLS')
    try:
        value = tr.query(cmd).strip()
    except Exception as exc:  # noqa: BLE001
        value = f'<查询异常: {exc}>'
    err = error_of(tr)
    tr.write('*CLS')
    if value.startswith('<查询异常'):
        verdict = 'ERROR'
    elif not value:
        verdict = 'NO DATA'
    elif not is_ok(err):
        verdict = 'REJECT'
    else:
        verdict = 'OK'
    summary = value if len(value) <= 80 else value[:77] + '...'
    return summary, err, verdict


def main() -> int:
    ap = argparse.ArgumentParser(description='探测矢网真实 SCPI 命令集')
    ap.add_argument('--host', default='192.168.2.111', help='矢网 IP（TCP/IP）')
    ap.add_argument('--port', type=int, default=5025, help='SCPI socket 端口')
    ap.add_argument('--timeout', type=float, default=3.0, help='单条命令超时（秒）')
    ap.add_argument('--interface', choices=('tcpip', 'gpib'), default='tcpip')
    ap.add_argument('--resource', default='GPIB0::17::INSTR', help='VISA 资源串（GPIB）')
    ap.add_argument('--json', action='store_true', help='额外输出机器可读 JSON')
    args = ap.parse_args()

    target = (f'{args.host}:{args.port}' if args.interface == 'tcpip'
              else args.resource)
    print(f'连接 {args.interface.upper()} {target} （超时 {args.timeout}s）...')
    tr = make_transport(args)
    try:
        tr.open()
    except Exception as exc:  # noqa: BLE001
        print(f'连接失败：{exc}')
        print('先确认 IP/端口、SCPI 服务器已开启、防火墙放行，'
              '或 GPIB 地址与 IO Libraries 状态。')
        return 2
    print('连接成功。注意：探测会临时创建 TL_probe_* 测量 / 改动迹线 1 参数。\n')

    results: dict = {'target': target, 'interface': args.interface}

    # ---- 1. 身份 -------------------------------------------------------
    print('=' * 72)
    print('1) 仪器身份（判断「版本旧不旧」看这里）')
    print('=' * 72)
    tr.write('*CLS')
    idn = tr.query('*IDN?').strip()
    opt = ''
    try:
        opt = tr.query('*OPT?').strip()
    except Exception:  # noqa: BLE001
        pass
    print(f'  *IDN? = {idn or "<无响应>"}')
    print(f'  *OPT? = {opt or "<无响应/不支持>"}')
    fields = [f.strip() for f in idn.split(',')]
    if len(fields) >= 4:
        print(f'  解析：厂商={fields[0]}  型号={fields[1]}  '
              f'序列号={fields[2]}  固件={fields[3]}')
    else:
        print('  解析：IDN 不是标准四段格式，请把原文发回。')
    results['idn'] = idn
    results['opt'] = opt

    # ---- 2. 测量定义方言 ----------------------------------------------
    print()
    print('=' * 72)
    print('2) 测量定义命令（报错的那一步）')
    print('=' * 72)
    print(f'  {"判定":<8} {"错误队列":<40} 命令 / 说明')
    print('  ' + '-' * 68)
    define_results = []
    for cmd, note in DEFINE_CANDIDATES:
        err, verdict = probe_write(tr, cmd)
        print(f'  {verdict:<8} {err[:38]:<40} {cmd}  [{note}]')
        define_results.append({'cmd': cmd, 'note': note,
                               'error': err, 'verdict': verdict})
    results['define'] = define_results
    accepted = [r['cmd'] for r in define_results if r['verdict'] == 'OK']
    print(f'\n  => 被接受的定义命令：{accepted or "（无）"}')
    if not accepted:
        print('     若全部被拒，说明这台仪器的命令树与 6 个内置 profile 都不匹配，'
              '需要按型号新增 profile。')

    # ---- 3. 其余命令 --------------------------------------------------
    print()
    print('=' * 72)
    print('3) 其余 SCPI 命令能力')
    print('=' * 72)
    print(f'  {"判定":<8} {"错误队列":<34} 命令 / 返回值')
    print('  ' + '-' * 68)
    other_results = []
    for cmd, note in OTHER_COMMANDS:
        # '?' can sit in the middle, e.g. ':CALC1:DATA? SDATA'.
        is_query = '?' in cmd
        if is_query:
            value, err, verdict = probe_query(tr, cmd)
            detail = f'{cmd}  -> {value}'
        else:
            value = ''
            err, verdict = probe_write(tr, cmd)
            detail = f'{cmd}  [{note}]'
        print(f'  {verdict:<8} {err[:32]:<34} {detail}')
        other_results.append({'cmd': cmd, 'note': note, 'error': err,
                              'verdict': verdict, 'value': value})
    results['other'] = other_results

    # ---- 4. 建议 ------------------------------------------------------
    print()
    print('=' * 72)
    print('4) 结论建议')
    print('=' * 72)
    if accepted:
        print(f'  测量定义请用：{accepted[0]}')
        print('  把上面整段输出（尤其 *IDN? 与第 2、3 节表格）发回，'
              '即可把该写法固化进对应厂商 profile。')
    else:
        print('  未找到可用的测量定义写法；请把 *IDN? 原文与仪器编程手册的'
              '「Measurement / Trace 定义」章节发回。')
    print('  -113 = Undefined header（命令头本设备未定义），'
          '不是参数错，也不是固件版本校验失败。')

    tr.write('*CLS')
    tr.close()
    print('\n连接已关闭。')

    if args.json:
        print('\n--- JSON ---')
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

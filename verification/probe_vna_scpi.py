#!/usr/bin/env python
"""真机 SCPI 方言探测器（命令行版）。

应用内已内置同样的功能：桌面端「系统 → SCPI 调试工具」页，或 Web 控制台
的「SCPI 探测」按钮。这个脚本用于不开界面、或需要导出 JSON 的场合。

用法（PowerShell）
------------------
.. code-block:: powershell

    cd D:\\Tuner
    .\\.venv\\Scripts\\python verification\\probe_vna_scpi.py --host 192.168.2.111
    .\\.venv\\Scripts\\python verification\\probe_vna_scpi.py --host 192.168.2.111 --json
    .\\.venv\\Scripts\\python verification\\probe_vna_scpi.py --interface gpib --resource GPIB0::17::INSTR

把输出的「1) 仪器身份」和「2) 测量定义命令」两节发回来，即可据此新增专用
profile。探测只发查询和无害配置，不发 ``*RST``；会临时创建 ``TL_probe_*``
测量 / 改动迹线 1，跑完请按需在前面板恢复。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许从源码树直接运行（venv 里已 editable-install 时可省略）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'TunerLab'))

from tuner.vna_probe import format_report, probe  # noqa: E402


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
    try:
        result = probe(interface=args.interface, host=args.host, port=args.port,
                       resource=args.resource, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001 - CLI shows a readable failure
        print(f'探测失败：{exc}')
        return 2
    print('连接成功。注意：探测会临时创建 TL_probe_* 测量 / 改动迹线 1 参数。\n')
    print(format_report(result))
    print('\n连接已关闭。')
    if args.json:
        print('\n--- JSON ---')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

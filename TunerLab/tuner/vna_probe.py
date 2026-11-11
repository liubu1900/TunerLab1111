"""Real-instrument SCPI capability probe, shared by the CLI, Web API and app.

The first Agilent/Keysight hardware session failed with ``-113 "Undefined
header"`` because the driver sent a measurement-definition command the
instrument does not implement.  This module answers, for any VNA reachable
over TCP/IP or VISA GPIB:

* who the instrument really is (``*IDN?`` -> model + firmware revision);
* which measurement-definition dialect it accepts;
* which data / power / IFBW / trigger commands are usable.

It is deliberately transport-agnostic: pass any object implementing the
``SocketSCPI`` contract (``open/close/connected/write/read/query``).  That
makes it usable from a background thread in the desktop app, from the Web
handler and from a standalone script, and easy to test with a scripted fake.

Only queries and harmless setup commands are sent -- never ``*RST``.  The
probe creates ``TL_probe_*`` measurements and changes trace 1, so the operator
should restore the front panel afterwards.

Older GPIB analyzers may not implement ``*CLS`` or ``:SYST:ERR?``.  Those
capabilities are detected up front; when the error queue is unavailable the
verdict becomes ``UNKNOWN`` instead of a misleading ``REJECT``, and the probe
falls back to reading the measurement catalogue back.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from . import vna_drivers as V

__all__ = [
    'DEFINE_CANDIDATES', 'OTHER_COMMANDS', 'make_transport',
    'probe_transport', 'probe', 'format_report', 'suggested_dialect',
]

NO_ERROR_PREFIXES = ('+0', '0,')

#: Measurement-definition dialects, in the same order the driver negotiates.
#: ``legacy`` commands only make sense on older GPIB analyzers.
DEFINE_CANDIDATES: list[tuple[str, str]] = [
    (":CALC1:PAR:DEF 'TL_probe_S11','S11'", '当前驱动 / ENA 两参数写法'),
    (":CALC1:PAR:SDEF 'TL_probe_S11','S11'", 'R&S ZNB/ZVA/ZND (SDEFine)'),
    (":CALC1:PAR:DEF:EXT 'TL_probe_S11','S11'", 'Keysight PNA / PNA-X (DEFine:EXTended)'),
    (":CALC1:PAR:EXT 'TL_probe_S11','S11'", 'Keysight PNA 简写 (PAR:EXT)'),
    (":CALC1:MEAS:DEF 'S11'", '早期 PNA（无命名测量）'),
    (':CALC1:PAR1:DEF S11', '老式 ENA / HP 87xx：按迹线号设置参数'),
]

#: Remaining commands.  A '?' anywhere in the string means "read a response";
#: ``:CALC1:DATA? SDATA`` is a query even though it does not end with '?'.
OTHER_COMMANDS: list[tuple[str, str]] = [
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


def make_transport(*, interface: str = 'tcpip',
                   host: str = '192.168.2.111', port: int = 5025,
                   resource: str = V.DEFAULT_GPIB_RESOURCE,
                   timeout: float = 3.0, resource_manager=None):
    """Build (but do not open) the transport described by a connection spec."""
    if str(interface).strip().lower() == V.INTERFACE_GPIB:
        return V.VISASCPI(resource, timeout, resource_manager=resource_manager)
    return V.SocketSCPI(host, port, timeout)


def _is_ok(resp: str) -> bool:
    text = (resp or '').strip().lower()
    return (text.startswith(NO_ERROR_PREFIXES) or text in ('0', '+0')
            or 'no error' in text)


class _ProbeSession:
    """One probe run: holds the transport plus its detected capabilities."""

    def __init__(self, tr, on_step=None, should_stop=None):
        self.tr = tr
        self.on_step = on_step
        self.should_stop = should_stop
        self.cls = True
        self.error_queue = True
        self.readback = True
        self.notes: list[str] = []

    def step(self, section: str, index: int, total: int, message: str) -> None:
        if self.on_step:
            try:
                self.on_step(section, index, total, message)
            except Exception:  # noqa: BLE001 - progress reporting is optional
                pass

    @property
    def aborted(self) -> bool:
        return bool(self.should_stop and self.should_stop())

    def clear(self) -> None:
        if not self.cls:
            return
        try:
            self.tr.write('*CLS')
        except Exception as exc:  # noqa: BLE001
            self.cls = False
            self.notes.append(f'*CLS 不被支持或超时：{exc}；后续清队列已跳过')

    def error_of(self) -> str:
        try:
            resp = self.tr.query(':SYST:ERR?').strip()
        except Exception as exc:  # noqa: BLE001
            self.error_queue = False
            return f'<查询异常: {exc}>'
        return resp or '<无响应>'

    def capabilities(self) -> dict:
        """Probe *CLS / :SYST:ERR? / read-back support once, before any test."""
        self.clear()
        resp = self.error_of()
        # Any real reply (including a pending -113) proves the queue exists;
        # a timeout, an empty read or a placeholder means it does not.
        self.error_queue = bool(resp) and not resp.startswith('<')
        if not self.error_queue:
            self.notes.append(
                '仪器没有可用的 :SYST:ERR? 错误队列；命令判定改用回读，'
                '无法回读时标记为 UNKNOWN（不代表被拒绝）')
        self.clear()
        try:
            self.tr.query(':CALC1:PAR:CAT?').strip()
        except Exception:  # noqa: BLE001
            # An empty catalogue is a valid reply; only a failed query means
            # the instrument cannot answer the read-back at all.
            self.readback = False
            self.notes.append('测量目录回读 :CALC1:PAR:CAT? 不可用')
        return {
            'cls': self.cls,
            'error_queue': self.error_queue,
            'readback': self.readback,
            'notes': list(self.notes),
        }

    def _catalog(self) -> Optional[str]:
        """Current measurement catalogue, or None when read-back is unavailable."""
        if not self.readback:
            return None
        try:
            return self.tr.query(':CALC1:PAR:CAT?').strip()
        except Exception:  # noqa: BLE001
            return None

    def write(self, cmd: str) -> tuple[str, str, str]:
        """Return (error-queue text, verdict, extra detail).

        Without an error queue a definition command is judged by whether it
        *changed* the measurement catalogue.  Comparing before/after (rather
        than just looking for the name) matters because a successful earlier
        candidate leaves its measurement in the catalogue, and every later
        candidate would otherwise look accepted too.
        """
        self.clear()
        definable = any(token in cmd for token in (
            ':PAR:DEF', ':PAR:SDEF', ':PAR:EXT', ':MEAS:DEF', 'PAR1:DEF'))
        before = self._catalog() if (definable and not self.error_queue) else None
        try:
            self.tr.write(cmd)
        except Exception as exc:  # noqa: BLE001
            return f'<发送异常: {exc}>', 'ERROR', ''
        if self.error_queue:
            err = self.error_of()
            verdict = 'OK' if _is_ok(err) else 'REJECT'
        else:
            err = '<无错误队列>'
            if not definable or before is None:
                verdict = 'UNKNOWN'
            else:
                after = self._catalog()
                if after is None:
                    verdict = 'UNKNOWN'
                elif after != before and not after.lstrip().startswith(('-', '!')):
                    verdict = 'OK'
                else:
                    verdict = 'REJECT'
        self.clear()
        return err, verdict, ''

    def query(self, cmd: str) -> tuple[str, str, str]:
        """Return (value summary, error-queue text, verdict).

        A query MUST read its response even when '?' is in the middle: leaving
        it in the instrument output buffer makes the next :SYST:ERR? read stale
        trace data instead of an error queue entry.
        """
        self.clear()
        try:
            value = self.tr.query(cmd).strip()
        except Exception as exc:  # noqa: BLE001
            value = f'<查询异常: {exc}>'
        err = self.error_of() if self.error_queue else '<无错误队列>'
        self.clear()
        if value.startswith('<查询异常'):
            verdict = 'ERROR'
        elif not value:
            verdict = 'NO DATA'
        elif value.lstrip().startswith(('-', '!')):
            verdict = 'REJECT'
        elif self.error_queue and not _is_ok(err):
            verdict = 'REJECT'
        else:
            # Without an error queue, a real response is the only evidence
            # available: treat returned data as usable, not as "unknown".
            verdict = 'OK'
        summary = value if len(value) <= 120 else value[:117] + '...'
        return summary, err, verdict


def _idn_fields(idn: str) -> dict:
    fields = [f.strip() for f in (idn or '').split(',')]
    if len(fields) >= 4:
        return {'manufacturer': fields[0], 'model': fields[1],
                'serial': fields[2], 'firmware': fields[3], 'raw': idn}
    return {'raw': idn}


def suggested_dialect(result: dict) -> dict:
    """Turn accepted probe commands into a driver ``dialect`` override.

    Only commands the current driver can actually consume are emitted, so the
    result can be passed straight to ``create_vna(dialect=...)`` without
    letting a probe report inject arbitrary settings.
    """
    out: dict[str, str] = {}
    for row in result.get('define', []):
        if row.get('verdict') != 'OK':
            continue
        header = str(row['cmd']).split(' ', 1)[0]
        if header in V.STANDARD_DEFINE_HEADERS:
            out['define_cmd'] = header
            break
    for row in result.get('other', []):
        if row.get('verdict') != 'OK':
            continue
        cmd = str(row['cmd'])
        head = cmd.split(' ', 1)[0]
        if cmd.startswith(':CALC1:DATA? ') and 'fetch_mnemonic' not in out:
            out['fetch_mnemonic'] = cmd.split(' ', 1)[1].strip()
        elif head in (':SOUR1:POW:LEV', ':SOUR1:POW:LEV:IMM:AMPL') \
                and 'power_cmd' not in out:
            out['power_cmd'] = head
        elif head == ':SENS1:BAND' and 'ifbw_cmd' not in out:
            out['ifbw_cmd'] = head
        elif head in (':INIT1:IMM', ':INIT:IMM') and 'trigger_cmd' not in out:
            out['trigger_cmd'] = head
    return out


def _summarise(result: dict) -> list[str]:
    lines: list[str] = []
    fields = result.get('idn_fields') or {}
    if fields.get('model'):
        lines.append(f"仪器：{fields['manufacturer']} {fields['model']}，"
                     f"固件 {fields['firmware']}")
    else:
        lines.append(f"*IDN? 原文：{result.get('idn') or '<无响应>'}")
    caps = result.get('capabilities') or {}
    if not caps.get('error_queue', True):
        lines.append('注意：仪器没有 :SYST:ERR? 错误队列，UNKNOWN 判定需用回读或手册确认。')
    accepted = [r['cmd'] for r in result.get('define', [])
                if r.get('verdict') == 'OK']
    if accepted:
        lines.append(f'可用的测量定义命令：{accepted[0]}')
    else:
        lines.append('未找到可用的测量定义写法；请把 *IDN? 与编程手册的'
                     '「Measurement / Trace 定义」章节发回以新增 profile。')
    dialect = result.get('suggested_dialect') or {}
    if dialect:
        lines.append('可直接应用的方言：' + '，'.join(
            f'{k}={v}' for k, v in dialect.items()))
    else:
        lines.append('没有可直接应用的完整方言（取数/功率命令可能不同），'
                     '需要按型号新增 profile。')
    lines.append('-113 = Undefined header（命令头本设备未定义），'
                 '不是参数错，也不是固件版本校验失败。')
    if result.get('aborted'):
        lines.append('探测已被手动中止，结果不完整。')
    return lines


def probe_transport(tr, *, target: str = '', interface: str = 'tcpip',
                    on_step: Optional[Callable] = None,
                    should_stop: Optional[Callable[[], bool]] = None) -> dict:
    """Probe an already-open transport.  The caller owns its lifetime."""
    session = _ProbeSession(tr, on_step, should_stop)
    result: dict[str, Any] = {
        'target': target,
        'interface': interface,
        'started_at': datetime.now().isoformat(timespec='seconds'),
        'idn': '', 'opt': '', 'idn_fields': {},
        'capabilities': {}, 'define': [], 'other': [],
        'accepted_define': [], 'suggested_dialect': {},
        'summary': [], 'aborted': False,
    }

    # ---- 1. identity ---------------------------------------------------
    session.step('身份', 0, 1, '读取 *IDN? / *OPT?')
    try:
        result['idn'] = tr.query('*IDN?').strip()
    except Exception as exc:  # noqa: BLE001
        result['idn'] = f'<查询异常: {exc}>'
    try:
        result['opt'] = tr.query('*OPT?').strip()
    except Exception:  # noqa: BLE001 - optional on most analyzers
        result['opt'] = ''
    result['idn_fields'] = _idn_fields(result['idn'])
    result['capabilities'] = session.capabilities()

    # ---- 2. measurement definition ------------------------------------
    total = len(DEFINE_CANDIDATES) + len(OTHER_COMMANDS)
    define_results: list[dict] = []
    for index, (cmd, note) in enumerate(DEFINE_CANDIDATES):
        if session.aborted:
            result['aborted'] = True
            break
        session.step('测量定义', index, total, cmd)
        err, verdict, _ = session.write(cmd)
        define_results.append({'cmd': cmd, 'note': note, 'error': err,
                               'verdict': verdict, 'value': ''})
    result['define'] = define_results
    result['accepted_define'] = [r['cmd'] for r in define_results
                                 if r['verdict'] == 'OK']

    # ---- 3. remaining commands ----------------------------------------
    other_results: list[dict] = []
    for offset, (cmd, note) in enumerate(OTHER_COMMANDS):
        if session.aborted:
            result['aborted'] = True
            break
        session.step('命令能力', len(DEFINE_CANDIDATES) + offset, total, cmd)
        if '?' in cmd:
            value, err, verdict = session.query(cmd)
        else:
            value = ''
            err, verdict, _ = session.write(cmd)
        other_results.append({'cmd': cmd, 'note': note, 'error': err,
                              'verdict': verdict, 'value': value})
    result['other'] = other_results

    try:
        session.clear()
    except Exception:  # noqa: BLE001
        pass
    result['suggested_dialect'] = suggested_dialect(result)
    result['summary'] = _summarise(result)
    result['finished_at'] = datetime.now().isoformat(timespec='seconds')
    return result


def probe(*, interface: str = 'tcpip', host: str = '192.168.2.111',
          port: int = 5025, resource: str = V.DEFAULT_GPIB_RESOURCE,
          timeout: float = 3.0, on_step=None, should_stop=None,
          resource_manager=None) -> dict:
    """Open a dedicated transport, probe it, and always close it again."""
    tr = make_transport(interface=interface, host=host, port=port,
                        resource=resource, timeout=timeout,
                        resource_manager=resource_manager)
    target = (f'{host}:{port}' if str(interface).lower() != V.INTERFACE_GPIB
              else str(getattr(tr, 'resource', resource)))
    try:
        tr.open()
    except Exception as exc:  # noqa: BLE001
        if str(interface).lower() == V.INTERFACE_GPIB:
            raise RuntimeError(
                f'无法打开 GPIB 资源 {target}：{exc}。请确认已安装 Keysight IO '
                'Libraries Suite、82357B 已识别、仪器上电且地址正确；'
                '可先用「检测 VISA」核对资源名。') from exc
        raise RuntimeError(
            f'无法连接 {target}：{exc}。请确认 IP/端口、仪器已开启 SCPI 服务、'
            '防火墙放行。') from exc
    try:
        return probe_transport(tr, target=target, interface=interface,
                               on_step=on_step, should_stop=should_stop)
    finally:
        try:
            tr.close()
        except Exception:  # noqa: BLE001
            pass


def format_report(result: dict) -> str:
    """Render a probe result as the plain-text report used by the CLI/app."""
    lines: list[str] = []
    lines.append(f"目标：{result.get('interface', '').upper()} "
                 f"{result.get('target', '')}  "
                 f"时间：{result.get('started_at', '')}")
    lines.append('=' * 72)
    lines.append('1) 仪器身份（判断「版本旧不旧」看这里）')
    lines.append('=' * 72)
    lines.append(f"  *IDN? = {result.get('idn') or '<无响应>'}")
    lines.append(f"  *OPT? = {result.get('opt') or '<无响应/不支持>'}")
    fields = result.get('idn_fields') or {}
    if fields.get('model'):
        lines.append(f"  解析：厂商={fields['manufacturer']}  型号={fields['model']}  "
                     f"序列号={fields['serial']}  固件={fields['firmware']}")
    else:
        lines.append('  解析：IDN 不是标准四段格式，请把原文发回。')
    caps = result.get('capabilities') or {}
    lines.append(f"  能力：*CLS={'有' if caps.get('cls') else '无'}  "
                 f":SYST:ERR?={'有' if caps.get('error_queue') else '无'}  "
                 f"测量目录回读={'有' if caps.get('readback') else '无'}")
    for note in caps.get('notes', []):
        lines.append(f'  提示：{note}')

    lines.append('')
    lines.append('=' * 72)
    lines.append('2) 测量定义命令（-113 报错的那一步）')
    lines.append('=' * 72)
    lines.append(f"  {'判定':<9} {'错误队列':<36} 命令 / 说明")
    lines.append('  ' + '-' * 68)
    for row in result.get('define', []):
        lines.append(f"  {row['verdict']:<9} {str(row['error'])[:34]:<36} "
                     f"{row['cmd']}  [{row['note']}]")
    accepted = result.get('accepted_define') or []
    lines.append(f"\n  => 被接受的定义命令：{accepted or '（无）'}")

    lines.append('')
    lines.append('=' * 72)
    lines.append('3) 其余 SCPI 命令能力')
    lines.append('=' * 72)
    lines.append(f"  {'判定':<9} {'错误队列':<32} 命令 / 返回值")
    lines.append('  ' + '-' * 68)
    for row in result.get('other', []):
        detail = row['cmd'] + (f"  -> {row['value']}" if row.get('value') else
                               f"  [{row['note']}]")
        lines.append(f"  {row['verdict']:<9} {str(row['error'])[:30]:<32} {detail}")

    lines.append('')
    lines.append('=' * 72)
    lines.append('4) 结论建议')
    lines.append('=' * 72)
    for line in result.get('summary', []):
        lines.append(f'  {line}')
    if result.get('aborted'):
        lines.append('  ⚠ 探测被中止，表格不完整。')
    lines.append('  说明：探测会临时创建 TL_probe_* 测量 / 改动迹线 1，'
                 '跑完请在前面板恢复。')
    return '\n'.join(lines)

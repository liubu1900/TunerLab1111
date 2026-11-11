"""Multi-vendor vector network analyzer drivers over TCP/IP or GPIB (SCPI).

Two transports share the same driver and the same vendor profiles:

* ``tcpip`` -- raw SCPI on a TCP socket (default port 5025, stdlib only).
* ``gpib``  -- SCPI over VISA, e.g. ``GPIB0::17::INSTR`` through an
  Agilent/Keysight 82357B USB-GPIB adapter. Requires an IVI-VISA runtime
  (Keysight IO Libraries Suite) plus PyVISA, both imported lazily so the
  TCP/IP path stays dependency-free.

Supported vendor families (the interface only changes *how* SCPI travels):

| key                | vendor / family                         | example models            |
|--------------------|-----------------------------------------|---------------------------|
| ``rsp``            | Rohde & Schwarz                         | ZNB, ZVA, ZND             |
| ``keysight_ena``   | Keysight ENA                            | E5071C, E5080, E5063      |
| ``keysight_pna``   | Keysight PNA / PNA-X                    | N5247A, N522x, N523x      |
| ``ceyear``         | Ceyear / 思仪 (CETC 41st Inst.)         | AV3672 series             |
| ``anritsu``        | Anritsu                                 | VectorStar MS46xxx, ShockLine |
| ``generic``        | ENA-like fallback (auto after unknown)  | —                         |

All drivers share one class, :class:`TCPVNA`; the vendor key only switches the
small set of commands that differ between families (measurement definition,
fetch mnemonic, power / IF-BW headers, trigger). A command-trial helper makes
each divergent operation fall back to the next candidate so a slightly different
firmware still works on first contact.
"""
from __future__ import annotations

import os
import re
import socket
import threading
import time
from typing import Optional

import numpy as np

from .models import Measurement
from . import vna_calibration


# --------------------------------------------------------------------------- #
# Interfaces and VISA resource names
# --------------------------------------------------------------------------- #
INTERFACE_TCPIP = 'tcpip'
INTERFACE_GPIB = 'gpib'

INTERFACE_LABELS = {
    INTERFACE_TCPIP: 'TCP/IP（SCPI socket，端口 5025）',
    INTERFACE_GPIB: 'GPIB（VISA / Agilent 82357B USB-GPIB）',
}

DEFAULT_GPIB_BOARD = 0
DEFAULT_GPIB_ADDRESS = 17
DEFAULT_GPIB_RESOURCE = 'GPIB0::17::INSTR'

#: ``GPIB0::17::INSTR`` or ``GPIB0::17::0::INSTR`` (secondary address).
_GPIB_RESOURCE_RE = re.compile(
    r'^GPIB(\d+)::(\d+)(?:::(\d+))?::INSTR$', re.IGNORECASE)


def gpib_resource(board: int = DEFAULT_GPIB_BOARD,
                  address: int = DEFAULT_GPIB_ADDRESS,
                  secondary: Optional[int] = None) -> str:
    """Build a VISA GPIB resource name, validating the IEEE-488 address range."""
    b, a = int(board), int(address)
    if not 0 <= b <= 15:
        raise ValueError('GPIB 板号必须在 0–15 之间')
    if not 0 <= a <= 30:
        raise ValueError('GPIB 主地址必须在 0–30 之间（31 为广播地址，不可用于仪器）')
    if secondary is None:
        return f'GPIB{b}::{a}::INSTR'
    s = int(secondary)
    if not 0 <= s <= 30:
        raise ValueError('GPIB 副地址必须在 0–30 之间')
    return f'GPIB{b}::{a}::{s}::INSTR'


def normalise_gpib_resource(value: str | int,
                            board: int = DEFAULT_GPIB_BOARD) -> str:
    """Accept either a bare primary address or a full VISA resource string."""
    text = str(value if value is not None else '').strip()
    if not text:
        raise ValueError('请填写 GPIB 地址（如 17）或 VISA 资源串（如 GPIB0::17::INSTR）')
    if text.isdigit():
        return gpib_resource(board, int(text))
    m = _GPIB_RESOURCE_RE.match(text)
    if m:
        return gpib_resource(m.group(1), m.group(2), m.group(3))
    raise ValueError(
        f'无法识别的 GPIB 资源串：{text!r}。'
        '请填写主地址（如 17）或完整 VISA 资源名（如 GPIB0::17::INSTR）。'
        '注意 GPIB 不是 IP 地址，也不需要填端口。')


#: ``os.add_dll_directory`` handles must stay referenced or the search path
#: entry is released by the garbage collector.
_VISA_DLL_HANDLES: list = []


def _prepare_visa_dll_paths() -> None:
    """Make the Keysight/Agilent IO Libraries DLLs loadable for PyVISA.

    The installer adds its ``bin`` directory to the system PATH, but processes
    started before the installation (a long-running desktop app, an IDE, a
    service) keep the old PATH.  Since Python 3.8, dependent DLLs are also no
    longer searched via PATH at all, so ``os.add_dll_directory`` is needed even
    in a fresh shell.  Without this, ``ResourceManager()`` fails with
    ``VI_ERROR_LIBRARY_NFOUND`` although ``visa32.dll`` is present.
    """
    if os.name != 'nt' or not hasattr(os, 'add_dll_directory'):
        return
    drive = os.environ.get('SystemDrive', 'C:')
    candidates = [
        rf'{drive}\Program Files\Keysight\IO Libraries Suite\bin',
        rf'{drive}\Program Files (x86)\Keysight\IO Libraries Suite\bin',
        rf'{drive}\Program Files (x86)\Agilent\IO Libraries Suite\bin',
        rf'{drive}\Program Files\IVI Foundation\VISA\Win64\bin',
        rf'{drive}\Program Files (x86)\IVI Foundation\VISA\WinNT\bin',
    ]
    paths = os.environ.get('PATH', '').split(os.pathsep)
    for directory in candidates:
        if not os.path.isdir(directory):
            continue
        try:
            _VISA_DLL_HANDLES.append(os.add_dll_directory(directory))
        except (OSError, ValueError):  # pragma: no cover - Windows-only quirks
            pass
        if directory not in paths:
            paths.insert(0, directory)
    os.environ['PATH'] = os.pathsep.join(paths)


def visa_resources() -> dict:
    """Probe the installed VISA runtime: backend path + discoverable resources.

    Used by the UI's "检测 VISA 资源" button so the operator can read the real
    GPIB address instead of guessing it.  Raises a readable RuntimeError when
    the Agilent/Keysight IO Libraries runtime is missing.
    """
    try:
        import pyvisa
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError('未安装 PyVISA，请在项目虚拟环境中执行 pip install pyvisa') from exc
    _prepare_visa_dll_paths()
    try:
        manager = pyvisa.ResourceManager()
    except Exception as exc:  # noqa: BLE001 - VISA backends raise varied errors
        raise RuntimeError(
            '未找到 VISA 运行时。Agilent/Keysight 82357B USB-GPIB 卡在 Windows 上'
            '需要安装 Keysight IO Libraries Suite（提供 visa32.dll 驱动）；'
            f'安装后重启应用再试。原始错误：{exc}') from exc
    try:
        lib = getattr(getattr(manager, 'visalib', None), 'library_path', None)
        return {
            'backend': str(lib or type(manager).__name__),
            'resources': [str(r) for r in manager.list_resources()],
        }
    finally:
        try:
            manager.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Transport 1: a tiny SCPI-over-TCP client
# --------------------------------------------------------------------------- #
class SocketSCPI:
    """SCPI client speaking over a raw TCP socket (VNA default port 5025)."""

    DEFAULT_PORT = 5025

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 10.0):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.sock: Optional[socket.socket] = None

    def open(self) -> None:
        if self.sock is not None:
            self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            finally:
                self.sock = None

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def write(self, cmd: str) -> None:
        if self.sock is None:
            raise RuntimeError('VNA not connected')
        self.sock.sendall(cmd.encode('ascii') + b'\n')

    def read(self) -> str:
        """Read until a line terminator or until the timeout fires."""
        if self.sock is None:
            raise RuntimeError('VNA not connected')
        buf = bytearray()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            last = buf[-1:]
            if last in (b'\n', b'\r'):
                break
            if time.monotonic() > deadline:
                break
        return buf.decode('ascii', errors='replace').strip()

    def query(self, cmd: str) -> str:
        self.write(cmd)
        return self.read()


# --------------------------------------------------------------------------- #
# Transport 2: SCPI over VISA GPIB (Agilent/Keysight 82357B, NI GPIB, ...)
# --------------------------------------------------------------------------- #
class VISASCPI:
    """SCPI client over a VISA GPIB session.

    Implements exactly the contract :class:`SocketSCPI` exposes -- ``open``,
    ``close``, ``connected``, ``write``, ``read``, ``query`` -- so :class:`TCPVNA`
    and all vendor profiles work unchanged over either interface.

    GPIB messages are terminated by the EOI line, not a newline, so both
    ``read_termination`` and ``write_termination`` default to ``''`` (VISA
    asserts EOI on the last byte of a write).  Instruments that only end their
    responses with LF can be handled by passing ``read_termination='\\n'``.

    ``resource_manager`` may be injected in tests; a manager created here is
    closed together with the session.
    """

    def __init__(self, resource: str = DEFAULT_GPIB_RESOURCE,
                 timeout: float = 10.0,
                 read_termination: str = '', write_termination: str = '',
                 resource_manager=None):
        self.resource = normalise_gpib_resource(resource)
        self.timeout = float(timeout)
        self.read_termination = read_termination
        self.write_termination = write_termination
        self._manager = resource_manager
        self._owns_manager = resource_manager is None
        self.instrument = None

    def open(self) -> None:
        if self.instrument is not None:
            self.close()
        try:
            if self._manager is None:
                import pyvisa
                _prepare_visa_dll_paths()
                self._manager = pyvisa.ResourceManager()
            inst = self._manager.open_resource(self.resource)
        except ImportError as exc:
            raise RuntimeError('未安装 PyVISA，请执行 pip install pyvisa') from exc
        except Exception as exc:  # noqa: BLE001 - backends raise varied errors
            raise RuntimeError(
                f'无法打开 GPIB 资源 {self.resource}：{exc}。'
                '请确认已安装 Keysight IO Libraries Suite（Agilent 82357B 驱动）、'
                '仪器已上电、GPIB 地址正确；可点「检测 VISA」核对实际资源名。') from exc
        try:
            inst.timeout = max(500, int(self.timeout * 1000))
            inst.read_termination = self.read_termination
            inst.write_termination = self.write_termination
        except Exception:  # noqa: BLE001
            try:
                inst.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        self.instrument = inst

    def close(self) -> None:
        inst, self.instrument = self.instrument, None
        if inst is not None:
            try:
                inst.close()
            except Exception:  # noqa: BLE001
                pass
        if self._owns_manager:
            manager, self._manager = self._manager, None
            if manager is not None:
                try:
                    manager.close()
                except Exception:  # noqa: BLE001
                    pass

    @property
    def connected(self) -> bool:
        return self.instrument is not None

    def _require(self):
        if self.instrument is None:
            raise RuntimeError('VNA not connected')
        return self.instrument

    def write(self, cmd: str) -> None:
        self._require().write(cmd)

    def read(self) -> str:
        return self._require().read().strip()

    def query(self, cmd: str) -> str:
        return self._require().query(cmd).strip()


# --------------------------------------------------------------------------- #
# Vendor detection + command profiles
# --------------------------------------------------------------------------- #
def detect_vendor(idn: str) -> str:
    """Guess the vendor family from an ``*IDN?`` string."""
    u = (idn or '').upper()
    if 'ROHDE' in u or 'R&S' in u or '&S' in u:
        return 'rsp'
    if 'E507' in u or 'E508' in u or 'E506' in u or 'ENA' in u:
        return 'keysight_ena'
    if 'N524' in u or 'N523' in u or 'N522' in u or 'PNA' in u:
        return 'keysight_pna'
    if 'CEYEAR' in u or 'AV367' in u or 'CETC' in u or '41ST' in u:
        return 'ceyear'
    if 'ANRITSU' in u or 'MS46' in u or 'MS4' in u or 'SHOCKLINE' in u:
        return 'anritsu'
    return 'generic'


# Per-family command settings. Only the fields that actually differ between
# vendors are listed; everything else uses the shared defaults in TCPVNA.
_VENDOR_PROFILES = {
    'rsp': {            # Rohde & Schwarz ZNB / ZVA / ZND
        'define_cmd': ':CALC1:PAR:SDEF',     # SDEFine (R&S specific)
        'fetch_mnemonic': 'SDAT',            # R&S uses SDAT
        'power_cmd': ':SOUR1:POW1:AMPL',     # POWer1:AMPLitude
        'ifbw_cmd': ':SENS1:BAND',
        'trigger_cmd': ':INIT1:IMM',
    },
    'keysight_ena': {   # ENA family (E5071C, E5080, ...)
        'define_cmd': ':CALC1:PAR:DEF',
        'fetch_mnemonic': 'SDATA',
        'power_cmd': ':SOUR1:POW:LEV',
        'ifbw_cmd': ':SENS1:BAND',
        'trigger_cmd': ':INIT1:IMM',
    },
    'keysight_pna': {   # PNA / PNA-X (N5247A, ...)
        # PNA creates *named* measurements with DEFine:EXTended; the plain
        # CALC1:PAR:DEF header is not defined on that command tree (-113).
        'define_cmd': ':CALC1:PAR:DEF:EXT',
        'fetch_mnemonic': 'SDATA',
        'power_cmd': ':SOUR1:POW:LEV:IMM:AMPL',
        'ifbw_cmd': ':SENS1:BAND',
        'trigger_cmd': ':INIT1:IMM',
    },
    'ceyear': {         # Ceyear 思仪 AV3672 (ENA-like SCPI)
        'define_cmd': ':CALC1:PAR:DEF',
        'fetch_mnemonic': 'SDATA',
        'power_cmd': ':SOUR1:POW:LEV',
        'ifbw_cmd': ':SENS1:BAND',
        'trigger_cmd': ':INIT1:IMM',
    },
    'anritsu': {        # Anritsu VectorStar / ShockLine
        'define_cmd': ':CALC1:PAR:DEF',
        'fetch_mnemonic': 'SDATA',
        'power_cmd': ':SOUR1:POW:LEV',
        'ifbw_cmd': ':SENS1:BAND',
        'trigger_cmd': ':INIT1:IMM',
    },
    'generic': {        # ENA-like fallback
        'define_cmd': ':CALC1:PAR:DEF',
        'fetch_mnemonic': 'SDATA',
        'power_cmd': ':SOUR1:POW:LEV',
        'ifbw_cmd': ':SENS1:BAND',
        'trigger_cmd': ':INIT1:IMM',
    },
}


VENDOR_LABELS = {
    'rsp': 'Rohde & Schwarz (ZNB/ZVA/ZND)',
    'keysight_ena': 'Keysight ENA (E5071C/E5080)',
    'keysight_pna': 'Keysight PNA-X (N5247A 等)',
    'ceyear': '思仪 Ceyear (AV3672)',
    'anritsu': 'Anritsu (VectorStar/ShockLine)',
    'generic': '通用 ENA 风格',
    'auto': '自动识别',
}


# --------------------------------------------------------------------------- #
# The driver
# --------------------------------------------------------------------------- #
class TCPVNA:
    """A SCPI session to any supported VNA, over TCP/IP or VISA GPIB.

    Usage::

        vna = create_vna('auto', '192.168.2.111', 5025, timeout=10)
        vna = create_vna('auto', '', 5025, timeout=10, interface='gpib',
                         resource='GPIB0::17::INSTR')
        vna.connect()                 # opens transport, reads IDN, builds S-params
        vna.set_sweep(1e9, 20e9, 401)
        vna.set_power(-10)
        m = vna.measure()             # -> Measurement(f, 2x2 S complex)
        vna.close()
    """

    S_PARAMS = ('S11', 'S21', 'S12', 'S22')

    def __init__(self, host: str, port: int = SocketSCPI.DEFAULT_PORT,
                 timeout: float = 10.0, vendor_key: str = 'auto', *,
                 transport=None, resource: Optional[str] = None,
                 interface: str = INTERFACE_TCPIP):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.vendor_key = vendor_key if vendor_key else 'auto'
        self.interface = interface
        self.transport = (transport if transport is not None
                          else SocketSCPI(host, port, timeout))
        #: Provenance string stored in every Measurement (host:port or VISA name).
        self.resource = resource or f'{self.host}:{self.port}'
        self.idn: Optional[str] = None
        self.last_error: Optional[str] = None

        self.frequencies = np.array([], dtype=float)
        self._sweep = {'start': 1e9, 'stop': 20e9, 'points': 41}
        self._power = -10.0
        self._ifbw = 1000.0

        self.lock = threading.RLock()
        # command fields (overwritten by _apply_profile)
        self._define_cmd = ':CALC1:PAR:DEF'
        self._fetch_mnemonic = 'SDATA'
        self._power_cmd = ':SOUR1:POW:LEV'
        self._ifbw_cmd = ':SENS1:BAND'
        self._trigger_cmd = ':INIT1:IMM'

    # -- connection ------------------------------------------------------
    def connect(self) -> str:
        with self.lock:
            self.transport.open()
            self.transport.write('*CLS')
            self.idn = self.transport.query('*IDN?')
            if self.vendor_key in ('', 'auto', None):
                self.vendor_key = detect_vendor(self.idn)
            self._apply_profile()
            self._preset()
            self.set_sweep(self._sweep['start'], self._sweep['stop'], self._sweep['points'])
            self.set_power(self._power)
            self.set_ifbw(self._ifbw)
            self.last_error = None
            errs = self.read_errors()
            if errs:
                self.last_error = '; '.join(errs)
            return self.idn

    def close(self) -> None:
        with self.lock:
            self.transport.close()

    @property
    def connected(self) -> bool:
        return self.transport.connected

    def query(self, cmd: str) -> str:
        """Interface-neutral SCPI query (used by the IDN button)."""
        with self.lock:
            return self.transport.query(cmd)

    def _apply_profile(self) -> None:
        prof = _VENDOR_PROFILES.get(self.vendor_key, _VENDOR_PROFILES['generic'])
        self._define_cmd = prof['define_cmd']
        self._fetch_mnemonic = prof['fetch_mnemonic']
        self._power_cmd = prof['power_cmd']
        self._ifbw_cmd = prof['ifbw_cmd']
        self._trigger_cmd = prof['trigger_cmd']

    # -- error handling --------------------------------------------------
    def _check_error(self) -> Optional[str]:
        """Return the next error-string from the queue, or None if empty."""
        try:
            resp = self.transport.query(':SYST:ERR?').strip()
        except Exception:  # noqa: BLE001 - never let a probe crash connect
            return None
        if resp.startswith('+0') or resp.startswith('0,'):
            return None
        return resp

    def _send_optional(self, cmd: str) -> None:
        """Send a best-effort command and swallow any error it produces."""
        self.transport.write(cmd)
        err = self._check_error()
        if err is not None:
            self.transport.write('*CLS')

    def read_errors(self) -> list[str]:
        errs: list[str] = []
        for _ in range(12):
            try:
                r = self.transport.query(':SYST:ERR?').strip()
            except Exception:  # noqa: BLE001 - older GPIB gear may lack a queue
                break
            if r.startswith('+0') or r.startswith('0,') or not r:
                break
            errs.append(r)
            if len(errs) >= 12:
                break
        return errs

    # -- setup -----------------------------------------------------------
    def _preset(self) -> None:
        # ASCII complex data so parsing is identical across vendors.
        self._send_optional(':FORMat:DATA ASCii')
        self._send_optional(':SENS1:SWE:TYPE LIN')
        # Single-sweep mode (best effort; ignored where unsupported).
        self._send_optional(':INIT1:CONT OFF')
        for name in self.S_PARAMS:
            self._define_measurement(name, name)
        self._select('S11')

    def _define_measurement(self, name: str, param: str) -> None:
        """Create a named measurement, trying every SCPI dialect we know.

        ``CALC1:PAR:DEF 'name','param'`` is NOT universal.  R&S uses
        ``CALC1:PAR:SDEF``; Keysight PNA / PNA-X use
        ``CALC1:PAR:DEF:EXT`` (or the short form ``CALC1:PAR:EXT``), and older
        ENA firmware expects a trace suffix (``PAR1:DEF``), which is a
        different model handled by a dedicated profile.  An unsupported header
        makes the instrument queue ``-113,"Undefined header"``.

        Every attempt clears the error queue *before and after*: a real
        analyzer queues one error per rejected command, so a leftover -113
        from the previous candidate would otherwise poison the verdict of the
        next (possibly valid) one.
        """
        headers = [self._define_cmd]
        for extra in (':CALC1:PAR:DEF', ':CALC1:PAR:SDEF',
                      ':CALC1:PAR:DEF:EXT', ':CALC1:PAR:EXT'):
            if extra not in headers:
                headers.append(extra)
        tried: list[str] = []
        for header in headers:
            cmd = f"{header} '{name}','{param}'"
            tried.append(cmd)
            self.transport.write('*CLS')
            self.transport.write(cmd)
            accepted = self._check_error() is None
            self.transport.write('*CLS')
            if accepted:
                # Reuse the working dialect for S21/S12/S22 in this session.
                self._define_cmd = header
                return
        raise RuntimeError(f'failed to define measurement {name} (tried {tried})')

    def _select(self, name: str) -> None:
        self._send_optional(f":CALC1:PAR:SEL '{name}'")

    # -- setters ---------------------------------------------------------
    def set_sweep(self, start_hz: float, stop_hz: float, points: int) -> None:
        """Configure the instrument sweep and the frequency grid stored in the library.

        ``points == 1`` is the single-frequency calibration case (load-pull at one
        spot frequency): START is the calibration frequency, STOP is pinned to the
        same value, and the local grid holds exactly that one point.  Several
        analyzers still return two or more trace samples when START == STOP;
        :meth:`_fetch_trace` keeps the first sample in that mode.

        Rejecting ``start >= stop`` here (rather than in ``database.create``)
        matters on real hardware: an invalid sweep must never be pushed to the
        instrument before the operator is told what to fix.
        """
        start = float(start_hz)
        stop = float(stop_hz)
        n = int(points)
        if not np.isfinite(start) or not np.isfinite(stop) or start <= 0 or stop <= 0:
            raise ValueError('sweep frequencies must be finite and positive')
        if n < 1:
            raise ValueError('sweep needs at least one point')
        if n > 1 and start >= stop:
            raise ValueError(
                'start frequency must be below stop frequency for a multi-point '
                'sweep; set points to 1 for single-frequency calibration')
        sweep_stop = start if n == 1 else stop
        with self.lock:
            self.transport.write(f":SENS1:FREQ:STAR {start:.10g}")
            self.transport.write(f":SENS1:FREQ:STOP {sweep_stop:.10g}")
            self.transport.write(f":SENS1:SWE:POIN {n}")
            self._sweep = {'start': start, 'stop': sweep_stop, 'points': n}
            self.frequencies = (np.asarray([start], dtype=float) if n == 1
                                else np.linspace(start, stop, n))

    def set_power(self, dbm: float) -> None:
        with self.lock:
            self.transport.write(f"{self._power_cmd} {float(dbm):.6g}")
            self._power = float(dbm)

    def set_ifbw(self, hz: float) -> None:
        with self.lock:
            self.transport.write(f"{self._ifbw_cmd} {float(hz):.6g}")
            self._ifbw = float(hz)

    def get_config(self) -> dict:
        return {
            'start_hz': self._sweep['start'],
            'stop_hz': self._sweep['stop'],
            'points': self._sweep['points'],
            'power_dbm': self._power,
            'ifbw_hz': self._ifbw,
            'fetch_mnemonic': self._fetch_mnemonic,
            'power_cmd': self._power_cmd,
            'ifbw_cmd': self._ifbw_cmd,
        }

    # -- acquisition -----------------------------------------------------
    def trigger_single(self) -> bool:
        with self.lock:
            candidates = [
                f"{self._trigger_cmd};*OPC?",
                ':INIT:IMM;*OPC?',
                ':INITiate:IMMediate;*OPC?',
            ]
            for cmd in candidates:
                try:
                    resp = self.transport.query(cmd).strip().lstrip('+').lstrip()
                except Exception:  # noqa: BLE001 - try the next trigger form
                    continue
                if resp.startswith('1'):
                    return True
            # Some GPIB instruments (notably older HP/Agilent boxes) reject a
            # compound message that mixes a trigger with a query.  Split it into
            # a write plus a separate *OPC? before giving up.
            try:
                self.transport.write(self._trigger_cmd)
                resp = self.transport.query('*OPC?').strip().lstrip('+').lstrip()
                if resp.startswith('1'):
                    return True
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError('trigger did not confirm completion (*OPC? != 1)')

    def _fetch_trace(self) -> np.ndarray:
        resp = self.transport.query(f":CALC1:DATA? {self._fetch_mnemonic}")
        parts = [x for x in resp.replace(' ', '').split(',') if x != '']
        vals = [float(x) for x in parts]
        if len(vals) % 2 != 0:
            raise ValueError(f'expected even number of real/imag values, got {len(vals)}')
        n = len(vals) // 2
        expected = self.frequencies.size
        if expected:
            if expected == 1 and n >= 1:
                # Single-frequency mode: some analyzers keep a minimum of two
                # sweep samples even with START == STOP. Every sample is at the
                # same frequency, so the first one is the measurement.
                vals = vals[:2]
            elif n != expected:
                raise ValueError(
                    f'expected {2 * expected} values for '
                    f'{expected} points, got {len(vals)}')
        n = len(vals) // 2
        arr = np.asarray(vals, dtype=float).reshape(n, 2)
        return arr[:, 0] + 1j * arr[:, 1]

    def fetch_all(self) -> dict[str, np.ndarray]:
        with self.lock:
            out: dict[str, np.ndarray] = {}
            for name in self.S_PARAMS:
                self._select(name)
                out[name] = self._fetch_trace()
            return out

    def measure(self) -> Measurement:
        if not self.connected:
            raise RuntimeError('VNA not connected')
        self.trigger_single()
        traces = self.fetch_all()
        f = self.frequencies
        s = np.empty((f.size, 2, 2), dtype=complex)
        for key, (op, ip) in {'S11': (0, 0), 'S21': (1, 0),
                              'S12': (0, 1), 'S22': (1, 1)}.items():
            s[:, op, ip] = traces[key]
        cal = vna_calibration.get_state()
        return Measurement(
            f.copy(), s, False,
            {'source': 'physical_vna', 'idn': self.idn,
             'resource': self.resource, 'interface': self.interface,
             'vendor': self.vendor_key,
             'calibration_validated': bool(cal.verified),
             'calibration_method': cal.method,
             'calibration_timestamp': cal.timestamp,
             'calibration_valid': bool(vna_calibration.is_valid())})

    # -- convenience -----------------------------------------------------
    def to_db(self, s: np.ndarray) -> np.ndarray:
        with np.errstate(divide='ignore'):
            return 20.0 * np.log10(np.abs(s) + 1e-30)


def create_vna(vendor_key: str, host: str, port: int = SocketSCPI.DEFAULT_PORT,
               timeout: float = 10.0, *, interface: str = INTERFACE_TCPIP,
               resource: Optional[str] = None,
               gpib_board: int = DEFAULT_GPIB_BOARD,
               gpib_address: int = DEFAULT_GPIB_ADDRESS,
               resource_manager=None) -> TCPVNA:
    """Factory: build a :class:`TCPVNA` on the requested interface.

    ``interface='tcpip'`` keeps the original socket behaviour (and the original
    positional signature, so every existing caller stays valid).
    ``interface='gpib'`` opens ``resource`` (default ``GPIB0::17::INSTR``) via
    PyVISA; a bare ``gpib_address`` is accepted too.
    """
    iface = str(interface or INTERFACE_TCPIP).strip().lower()
    if iface in (INTERFACE_GPIB, 'visa', 'gpib-visa', 'gpib_visa'):
        target = normalise_gpib_resource(
            resource if resource not in (None, '') else gpib_address, gpib_board)
        transport = VISASCPI(target, timeout, resource_manager=resource_manager)
        return TCPVNA(host, port, timeout, vendor_key, transport=transport,
                      resource=target, interface=INTERFACE_GPIB)
    if iface != INTERFACE_TCPIP:
        raise ValueError(f'unknown VNA interface: {interface!r}')
    return TCPVNA(host, port, timeout, vendor_key)


def available_interfaces() -> dict[str, str]:
    """Interface keys + Chinese labels for the connection panels."""
    return dict(INTERFACE_LABELS)


def available_vendors() -> dict[str, str]:
    return {k: VENDOR_LABELS.get(k, k) for k in VENDOR_LABELS
            if k not in ('auto',)}

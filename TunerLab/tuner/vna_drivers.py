"""Multi-vendor vector network analyzer drivers over TCP/IP (SCPI on socket).

Supported instruments (all controlled via raw TCP socket SCPI, default port 5025,
no VISA runtime / vendor driver required):

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

The module is intentionally dependency-free (standard library ``socket`` only)
so it runs on the same Windows host that carries the Zmotion link, and so the
calibration studio can use a real VNA instead of the synthetic one.
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Optional

import numpy as np

from .models import Measurement
from . import vna_calibration


# --------------------------------------------------------------------------- #
# Transport: a tiny SCPI-over-TCP client
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
        'define_cmd': ':CALC1:PAR:DEF',
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
    """A TCP/IP SCPI session to any supported VNA.

    Usage::

        vna = create_vna('auto', '192.168.2.111', 5025, timeout=10)
        vna.connect()                 # opens socket, reads IDN, builds S-params
        vna.set_sweep(1e9, 20e9, 401)
        vna.set_power(-10)
        m = vna.measure()             # -> Measurement(f, 2x2 S complex)
        vna.close()
    """

    S_PARAMS = ('S11', 'S21', 'S12', 'S22')

    def __init__(self, host: str, port: int = SocketSCPI.DEFAULT_PORT,
                 timeout: float = 10.0, vendor_key: str = 'auto'):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.vendor_key = vendor_key if vendor_key else 'auto'
        self.transport = SocketSCPI(host, port, timeout)
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
            r = self.transport.query(':SYST:ERR?').strip()
            if r.startswith('+0') or r.startswith('0,'):
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
        # Both the trace/measurement name and the S-parameter are quoted; every
        # supported family accepts quoted strings (R&S is the strict one).
        candidates = [f"{self._define_cmd} '{name}','{param}'"]
        # Add the sibling form so R&S (SDEF) vs others (DEF) both work.
        if self._define_cmd.endswith('SDEF'):
            candidates.append(f":CALC1:PAR:DEF '{name}','{param}'")
        else:
            candidates.append(f":CALC1:PAR:SDEF '{name}','{param}'")
        for cmd in candidates:
            self.transport.write(cmd)
            if self._check_error() is None:
                return
        raise RuntimeError(f'failed to define measurement {name} (tried {candidates})')

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
             'resource': f'{self.host}:{self.port}', 'vendor': self.vendor_key,
             'calibration_validated': bool(cal.verified),
             'calibration_method': cal.method,
             'calibration_timestamp': cal.timestamp,
             'calibration_valid': bool(vna_calibration.is_valid())})

    # -- convenience -----------------------------------------------------
    def to_db(self, s: np.ndarray) -> np.ndarray:
        with np.errstate(divide='ignore'):
            return 20.0 * np.log10(np.abs(s) + 1e-30)


def create_vna(vendor_key: str, host: str, port: int = SocketSCPI.DEFAULT_PORT,
               timeout: float = 10.0) -> TCPVNA:
    """Factory: build a :class:`TCPVNA` for the given vendor key / host."""
    return TCPVNA(host, port, timeout, vendor_key)


def available_vendors() -> dict[str, str]:
    return {k: VENDOR_LABELS.get(k, k) for k in VENDOR_LABELS
            if k not in ('auto',)}

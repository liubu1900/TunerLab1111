"""Zmotion (正运动) ZMC controller driver over the official ``zauxdll`` shared library.

The vendor ships ``zauxdllPython.py`` which hard-codes ``ctypes.WinDLL('./zauxdll.dll')``.
That resolves the library against the *current working directory*, so the package only
imports when the process happens to run inside the vendor folder, and the DLL cannot be
swapped for a test double. This module binds the documented C entry points directly so
that:

* the library path is resolved from the package location (no CWD dependency),
* a fake library can be injected for automated tests,
* every vendor error code is surfaced as a typed :class:`ZmotionError`.

Units
-----
``UNITS`` is the pulse equivalent of the controller (pulses per user unit). Distances
passed to ``MOVE``/``MOVEABS`` and positions reported by ``DPOS``/``MPOS`` are expressed
in **user units** (millimetres on this machine), not raw pulses. The controller converts
internally: ``pulses = distance * UNITS``. This driver therefore speaks user units
end-to-end and never scales by ``UNITS`` itself.

Only functions verified against ``zauxdll2.h`` and confirmed by reading real values back
from the controller are bound here.
"""
from __future__ import annotations

import atexit
import ctypes
import os
import time
import weakref
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

__all__ = [
    'ZmotionError',
    'ZmotionLibrary',
    'ZmotionController',
    'load_library',
    'probe_controller',
    'vendor_dir',
]

# Vendor error code returned for a successful call.
ERR_OK = 0

#: Vendor ``ZAux_OpenEth`` fails with this code when no controller answers.
ERR_NOT_FOUND = 6

#: Size of the reply buffer handed to the Basic command channel.
BASIC_RESPONSE_BUFFER = 2048

#: Buffer for ``ZAux_SearchEthlist``; the DLL writes a space-separated address
#: list into it.
SEARCH_LIST_BUFFER = 2048


# --------------------------------------------------------------------------- #
# Clean shutdown of live controller handles
# --------------------------------------------------------------------------- #
# ``zauxdll.dll`` spawns an internal receive thread for every ``ZAux_OpenEth``
# link.  If the process exits while that thread is still alive, the DLL is
# unloaded out from under it and the main thread raises an *access violation*
# ("<freed thread state>") at interpreter shutdown — which, under Windows Error
# Reporting, can also make the process hang instead of returning.  We therefore
# track every open controller and release the handles in an ``atexit`` hook so
# the vendor thread is stopped *before* the DLL goes away.  The set holds weak
# references so it never keeps a controller alive on its own.
_LIVE_CONTROLLERS: weakref.WeakSet = weakref.WeakSet()


def _close_live_controllers() -> None:
    """Release any controller handle still open when the process exits.

    Best-effort only: a caller that forgot to close is the abnormal case, and
    the vendor call may itself be racing shutdown, so swallow any failure.
    """
    for controller in list(_LIVE_CONTROLLERS):
        try:
            controller.close()
        except Exception:  # noqa: BLE001 - nothing useful can be done at exit
            pass


atexit.register(_close_live_controllers)


class ZmotionError(RuntimeError):
    """A vendor API call failed or the controller is in an unusable state."""

    def __init__(self, message: str, code: int | None = None):
        self.code = code
        super().__init__(message if code is None else f'{message} (error code {code})')


def vendor_dir() -> Path:
    """Directory holding the vendor library and header."""
    return Path(__file__).resolve().parent.parent / 'vendor' / 'zmotion'


def _candidate_library_paths() -> list[Path]:
    """Search order for ``zauxdll.dll``: package vendor dir, then environment override."""
    candidates: list[Path] = []
    override = os.environ.get('TUNERLAB_ZAUXDLL')
    if override:
        candidates.append(Path(override))
    base = vendor_dir()
    candidates.append(base / 'zauxdll.dll')
    candidates.append(base / 'zauxdll64.dll')
    return candidates


def load_library(path: str | os.PathLike | None = None) -> ctypes.CDLL:
    """Load the vendor shared library.

    Raises :class:`ZmotionError` with an actionable message when the library is absent,
    naming every path that was probed.
    """
    probed: list[Path] = []
    targets = [Path(path)] if path is not None else _candidate_library_paths()
    for candidate in targets:
        probed.append(candidate)
        if candidate.is_file():
            if not hasattr(ctypes, 'WinDLL'):
                raise ZmotionError(
                    'Zmotion zauxdll.dll requires Windows (64-bit); '
                    'use the virtual controller on this platform.')
            try:
                return ctypes.WinDLL(str(candidate))
            except OSError as exc:  # pragma: no cover - depends on host DLL loader
                raise ZmotionError(f'cannot load Zmotion library {candidate}: {exc}') from exc
    listing = '\n  '.join(str(p) for p in probed)
    raise ZmotionError(
        'Zmotion shared library not found. Install zauxdll.dll (64-bit) from the\n'
        f'  vendor "Windows Python (64位)" package into one of:\n  {listing}\n'
        'or set the TUNERLAB_ZAUXDLL environment variable to its full path.'
    )


def probe_controller(ip: str, timeout: float = 3.0) -> dict[str, object]:
    """Check whether a controller answers at ``ip``, and which firmware it runs.

    ``ZAux_OpenEth`` addresses one controller directly and answered on every
    attempt on the bench (2026-11-08), whereas the UDP search calls are
    rate-limited by the controller and only answer occasionally.  This probe
    therefore opens a short-lived link, reads the firmware string and closes it
    again — the operator gets a definite "yes, firmware 2.9600" instead of a
    broadcast that may or may not be answered.

    The link is closed in a ``finally`` block, so a failed probe never leaves a
    handle behind.
    """
    target = (ip or '').strip()
    result: dict[str, object] = {
        'ip': target, 'reachable': False, 'firmware': '', 'error': '',
    }
    if not target:
        result['error'] = 'IP 地址为空'
        return result
    controller = ZmotionController(ip=target)
    try:
        controller.open(target, timeout=timeout, retries=1)
    except ZmotionError as exc:
        result['error'] = str(exc)
        result['code'] = exc.code
        return result
    except Exception as exc:  # noqa: BLE001 - a probe must not raise
        result['error'] = f'{type(exc).__name__}: {exc}'
        return result
    try:
        result['reachable'] = True
        result['firmware'] = controller.firmware
    finally:
        controller.close()
    return result


def _bind(dll: ctypes.CDLL, name: str, argtypes: Sequence[object], restype=ctypes.c_int32):
    """Bind one exported function, reporting the exact symbol when it is missing."""
    try:
        func = getattr(dll, name)
    except AttributeError as exc:
        raise ZmotionError(f'Zmotion library does not export {name}') from exc
    func.argtypes = list(argtypes)
    func.restype = restype
    return func


class ZmotionLibrary:
    """Thin typed wrapper around the ``zauxdll`` C API.

    Instances hold the loaded DLL plus bound function pointers. Constructing this class
    does not talk to any hardware, which keeps it safe to build during import.
    """

    def __init__(self, dll: ctypes.CDLL):
        self.dll = dll
        handle = ctypes.c_void_p
        i32p = ctypes.POINTER(ctypes.c_int32)
        f32p = ctypes.POINTER(ctypes.c_float)

        self.open_eth = _bind(dll, 'ZAux_OpenEth', [ctypes.c_char_p, handle])
        self.close = _bind(dll, 'ZAux_Close', [handle])
        # ``ZAux_SearchEth`` takes three arguments: the target address, the response
        # window in milliseconds and a handle pointer.  The vendor header
        # (zauxdll2.h) still documents the old two-argument form, but the shipped
        # DLL and the vendor's own Python wrapper both use three; with two the
        # third register arrives as garbage and every call fails with 20010.
        self.search_eth = _bind(dll, 'ZAux_SearchEth',
                                [ctypes.c_char_p, ctypes.c_uint32,
                                 ctypes.POINTER(ctypes.c_void_p)])
        # ``ZAux_SearchEthlist`` fills a caller-provided buffer with the addresses
        # of every controller that answers on the subnet, separated by spaces.
        self.search_eth_list = _bind(dll, 'ZAux_SearchEthlist',
                                     [ctypes.c_char_p, ctypes.c_uint32,
                                      ctypes.c_uint32])
        # psResponse is a caller-provided buffer (char*), and the length is passed by
        # value (uint32), not by pointer.
        self.execute = _bind(dll, 'ZAux_Execute',
                              [handle, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char),
                               ctypes.c_uint32])
        self.direct_command = _bind(dll, 'ZAux_DirectCommand',
                                    [handle, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char),
                                     ctypes.c_uint32])

        self.set_atype = _bind(dll, 'ZAux_Direct_SetAtype', [handle, ctypes.c_int32, ctypes.c_int32])
        self.get_atype = _bind(dll, 'ZAux_Direct_GetAtype', [handle, ctypes.c_int32, i32p])
        self.set_units = _bind(dll, 'ZAux_Direct_SetUnits', [handle, ctypes.c_int32, ctypes.c_float])
        self.get_units = _bind(dll, 'ZAux_Direct_GetUnits', [handle, ctypes.c_int32, f32p])
        self.set_speed = _bind(dll, 'ZAux_Direct_SetSpeed', [handle, ctypes.c_int32, ctypes.c_float])
        self.get_speed = _bind(dll, 'ZAux_Direct_GetSpeed', [handle, ctypes.c_int32, f32p])
        self.set_accel = _bind(dll, 'ZAux_Direct_SetAccel', [handle, ctypes.c_int32, ctypes.c_float])
        self.get_accel = _bind(dll, 'ZAux_Direct_GetAccel', [handle, ctypes.c_int32, f32p])
        self.set_decel = _bind(dll, 'ZAux_Direct_SetDecel', [handle, ctypes.c_int32, ctypes.c_float])
        self.get_decel = _bind(dll, 'ZAux_Direct_GetDecel', [handle, ctypes.c_int32, f32p])
        self.set_creep = _bind(dll, 'ZAux_Direct_SetCreep', [handle, ctypes.c_int32, ctypes.c_float])
        self.get_creep = _bind(dll, 'ZAux_Direct_GetCreep', [handle, ctypes.c_int32, f32p])
        self.set_jog_speed = _bind(dll, 'ZAux_Direct_SetJogSpeed', [handle, ctypes.c_int32, ctypes.c_float])
        self.get_jog_speed = _bind(dll, 'ZAux_Direct_GetJogSpeed', [handle, ctypes.c_int32, f32p])
        self.set_invert_step = _bind(dll, 'ZAux_Direct_SetInvertStep', [handle, ctypes.c_int32, ctypes.c_int32])
        self.get_invert_step = _bind(dll, 'ZAux_Direct_GetInvertStep', [handle, ctypes.c_int32, i32p])
        self.set_datum_in = _bind(dll, 'ZAux_Direct_SetDatumIn', [handle, ctypes.c_int32, ctypes.c_int32])
        self.get_datum_in = _bind(dll, 'ZAux_Direct_GetDatumIn', [handle, ctypes.c_int32, i32p])
        self.set_home_wait = _bind(dll, 'ZAux_Direct_SetHomeWait', [handle, ctypes.c_int32, ctypes.c_int32])
        self.get_home_wait = _bind(dll, 'ZAux_Direct_GetHomeWait', [handle, ctypes.c_int32, i32p])
        self.set_fs_limit = _bind(dll, 'ZAux_Direct_SetFsLimit', [handle, ctypes.c_int32, ctypes.c_float])
        self.get_fs_limit = _bind(dll, 'ZAux_Direct_GetFsLimit', [handle, ctypes.c_int32, f32p])
        self.set_rs_limit = _bind(dll, 'ZAux_Direct_SetRsLimit', [handle, ctypes.c_int32, ctypes.c_float])
        self.get_rs_limit = _bind(dll, 'ZAux_Direct_GetRsLimit', [handle, ctypes.c_int32, f32p])

        self.get_dpos = _bind(dll, 'ZAux_Direct_GetDpos', [handle, ctypes.c_int32, f32p])
        self.set_dpos = _bind(dll, 'ZAux_Direct_SetDpos', [handle, ctypes.c_int32, ctypes.c_float])
        self.get_mpos = _bind(dll, 'ZAux_Direct_GetMpos', [handle, ctypes.c_int32, f32p])
        self.set_mpos = _bind(dll, 'ZAux_Direct_SetMpos', [handle, ctypes.c_int32, ctypes.c_float])
        self.get_if_idle = _bind(dll, 'ZAux_Direct_GetIfIdle', [handle, ctypes.c_int32, i32p])
        self.get_home_status = _bind(dll, 'ZAux_Direct_GetHomeStatus', [handle, ctypes.c_int32, i32p])
        self.get_axis_status = _bind(dll, 'ZAux_Direct_GetAxisStatus', [handle, ctypes.c_int32, i32p])
        self.get_axis_stop_reason = _bind(dll, 'ZAux_Direct_GetAxisStopReason', [handle, ctypes.c_int32, i32p])
        self.get_axis_enable = _bind(dll, 'ZAux_Direct_GetAxisEnable', [handle, ctypes.c_int32, i32p])
        self.set_axis_enable = _bind(dll, 'ZAux_Direct_SetAxisEnable', [handle, ctypes.c_int32, ctypes.c_int32])
        self.get_mspeed = _bind(dll, 'ZAux_Direct_GetMspeed', [handle, ctypes.c_int32, f32p])

        self.move_abs = _bind(dll, 'ZAux_Direct_Single_MoveAbs', [handle, ctypes.c_int32, ctypes.c_float])
        self.move_rel = _bind(dll, 'ZAux_Direct_Single_Move', [handle, ctypes.c_int32, ctypes.c_float])
        self.cancel = _bind(dll, 'ZAux_Direct_Single_Cancel', [handle, ctypes.c_int32, ctypes.c_int32])
        # The DLL exports this symbol with mixed case; Windows exports are case-sensitive.
        self.datum = _bind(dll, 'ZAux_Direct_Single_Datum', [handle, ctypes.c_int32, ctypes.c_int32])
        self.vmove = _bind(dll, 'ZAux_Direct_Single_Vmove', [handle, ctypes.c_int32, ctypes.c_int32])
        self.cancel_axis_list = _bind(
            dll, 'ZAux_Direct_CancelAxisList',
            [handle, ctypes.c_int32, ctypes.POINTER(ctypes.c_int32), ctypes.c_int32],
        )
        self.rapidstop = _bind(dll, 'ZAux_Direct_Rapidstop', [handle, ctypes.c_int32])
        self.get_all_axis_info = _bind(
            dll, 'ZAux_Direct_GetAllAxisInfo',
            [handle, ctypes.c_int32,
             ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_float),
             ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_int32)],
        )

    # -- helpers ---------------------------------------------------------
    def _check(self, code: int, what: str) -> int:
        if code != ERR_OK:
            raise ZmotionError(f'{what} failed', code)
        return code

    def get_f32(self, handle, func, axis: int, what: str) -> float:
        """Read one float axis parameter through a bound getter."""
        value = ctypes.c_float()
        self._check(func(handle, ctypes.c_int32(axis), ctypes.byref(value)), what)
        return float(value.value)

    def get_i32(self, handle, func, axis: int, what: str) -> int:
        """Read one integer axis parameter through a bound getter."""
        value = ctypes.c_int32()
        self._check(func(handle, ctypes.c_int32(axis), ctypes.byref(value)), what)
        return int(value.value)

    def set_f32(self, handle, func, axis: int, value: float, what: str) -> None:
        """Write one float axis parameter through a bound setter."""
        self._check(func(handle, ctypes.c_int32(axis), ctypes.c_float(value)), what)

    def set_i32(self, handle, func, axis: int, value: int, what: str) -> None:
        """Write one integer axis parameter through a bound setter."""
        self._check(func(handle, ctypes.c_int32(axis), ctypes.c_int32(value)), what)


class ZmotionController:
    """Connection to one ZMC controller plus the axis operations TunerLab needs.

    The class owns the vendor handle and guarantees it is released on ``close()``. All
    axis indices are zero-based on the controller; the Tuner maps them to ``X``, ``Z1``,
    ``Z2``, ``Z3``.
    """

    def __init__(self, library: ZmotionLibrary | None = None, ip: str = '192.168.0.11'):
        self.lib = library if library is not None else ZmotionLibrary(load_library())
        self.ip = ip
        self._handle = ctypes.c_void_p()
        self._active_handle = self._handle
        self.connected = False
        self.firmware = ''
        _LIVE_CONTROLLERS.add(self)

    # -- connection ------------------------------------------------------
    def open(self, ip: str | None = None, timeout: float = 3.0,
             retries: int = 3, backoff: float = 0.4) -> None:
        """Open an ethernet link to the controller.

        ``ZAux_OpenEth`` locates the controller by UDP broadcast on the local subnet, so
        the host must be on the same segment as the controller.

        The broadcast discovery is not guaranteed to be answered on every attempt: a
        controller that is busy or momentarily slow to reply yields error code 20008
        ("controller not found"), indistinguishable from a genuinely wrong address. The
        link is therefore established with a short bounded retry so that a single missed
        broadcast does not look like a wiring fault. The same code is still reported when
        every attempt fails.
        """
        if ip is not None:
            self.ip = ip
        if self.connected:
            self.close()
        last_code = ERR_NOT_FOUND
        for attempt in range(max(1, int(retries))):
            handle = ctypes.c_void_p()
            code = self.lib.open_eth(self.ip.encode('utf-8'), ctypes.byref(handle))
            if code == ERR_OK:
                self._handle = handle
                self._active_handle = handle
                self.connected = True
                try:
                    self.firmware = self.read_firmware()
                except ZmotionError:
                    self.firmware = ''
                return
            last_code = code
            if attempt + 1 < max(1, int(retries)):
                time.sleep(backoff * (attempt + 1))
        self._handle = ctypes.c_void_p()
        self._active_handle = self._handle
        raise ZmotionError(f'cannot open controller {self.ip}', last_code)

    def close(self) -> None:
        """Release the controller handle; safe to call when not connected."""
        if self.connected:
            try:
                self.lib.close(self._handle)
            finally:
                self.connected = False
                self._handle = ctypes.c_void_p()
                self._active_handle = self._handle
        _LIVE_CONTROLLERS.discard(self)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def require_connection(self) -> None:
        if not self.connected:
            raise ZmotionError('controller is not connected')

    def search(self, ip: str = '', timeout_ms: int = 1000) -> bool:
        """Probe whether a controller answers at ``ip``.

        ``ZAux_SearchEth`` addresses **one** controller by IP.  Measured on the
        bench (2026-11-08) an empty address is rejected with error 20010 — the
        call is not a subnet broadcast, so the address may not be left empty.
        Subnet-wide discovery is a different call, :meth:`search_list`.

        The probe is also *rate-limited by the controller*: on the bench a second
        search in the same session was answered with 20010 even though the
        controller was reachable by every other means.  A negative answer
        therefore means "did not answer", not "is not there".
        """
        target = (ip or '').strip()
        if not target:
            return bool(self.search_list(timeout_ms=timeout_ms))
        handle = ctypes.c_void_p()
        code = self.lib.search_eth(
            target.encode('utf-8'), ctypes.c_uint32(timeout_ms),
            ctypes.byref(handle))
        return code == ERR_OK

    def search_list(self, timeout_ms: int = 1500) -> list[str]:
        """Every controller that answers a subnet broadcast.

        The DLL fills the buffer with a space-separated address list.  The
        broadcast is unreliable on a multi-NIC workstation — the reply has to
        come back on the interface the request left on — so an empty list means
        "nothing answered", not "there is nothing there".
        """
        buffer = ctypes.create_string_buffer(SEARCH_LIST_BUFFER)
        code = self.lib.search_eth_list(
            buffer, ctypes.c_uint32(SEARCH_LIST_BUFFER),
            ctypes.c_uint32(timeout_ms))
        if code != ERR_OK:
            return []
        raw = buffer.value.decode('utf-8', errors='replace')
        return [part for part in raw.replace(',', ' ').split() if part]

    def read_firmware(self) -> str:
        """Controller firmware version string, read over the Basic command channel."""
        self.require_connection()
        # ?VERSION is answered by the controller's Basic interpreter.
        version = self._basic_query('?VERSION')
        return version.strip()

    def _basic_query(self, command: str) -> str:
        """Run one Basic command and return its textual reply.

        ``ZAux_DirectCommand`` is a non-blocking round trip that writes the printed
        output of the command into a caller-provided buffer; ``?VERSION`` answers with
        the firmware string.
        """
        buffer = ctypes.create_string_buffer(BASIC_RESPONSE_BUFFER)
        code = self.lib.direct_command(
            self._handle, command.encode('utf-8'), buffer, BASIC_RESPONSE_BUFFER
        )
        if code != ERR_OK:
            raise ZmotionError(f'Basic command {command!r} failed', code)
        return buffer.value.decode('utf-8', errors='replace')

    def read_error_code(self) -> dict[str, object]:
        """Read the controller's own last error number over the Basic channel.

        The controller keeps a task error number that the Basic interpreter can
        print.  Firmware that does not implement the query answers with a text
        reply instead of a number; that case is reported as ``supported=False``
        rather than being turned into a made-up error code.

        This is deliberately *not* part of the fast status poll: an unknown Basic
        command can itself raise a controller error, so the query is only issued
        when the operator explicitly asks for it.
        """
        self.require_connection()
        raw = ''
        for command in ('?ERRCODE', '?ERRMSG'):
            try:
                reply = self._basic_query(command)
            except ZmotionError:
                continue
            text = reply.strip()
            if not raw:
                raw = text
            if text.lstrip('+-').isdigit():
                return {'code': int(text), 'command': command, 'supported': True,
                        'raw': text}
        return {'code': None, 'command': None, 'supported': False, 'raw': raw}

    # -- axis parameters -------------------------------------------------
    def get_atype(self, axis: int) -> int:
        self.require_connection()
        return self.lib.get_i32(self._handle, self.lib.get_atype, axis, 'read ATYPE')

    def set_atype(self, axis: int, value: int) -> None:
        self.require_connection()
        self.lib.set_i32(self._handle, self.lib.set_atype, axis, value, 'set ATYPE')

    def get_units(self, axis: int) -> float:
        self.require_connection()
        return self.lib.get_f32(self._handle, self.lib.get_units, axis, 'read UNITS')

    def set_units(self, axis: int, value: float) -> None:
        self.require_connection()
        self.lib.set_f32(self._handle, self.lib.set_units, axis, value, 'set UNITS')

    def get_speed(self, axis: int) -> float:
        self.require_connection()
        return self.lib.get_f32(self._handle, self.lib.get_speed, axis, 'read SPEED')

    def set_speed(self, axis: int, value: float) -> None:
        self.require_connection()
        self.lib.set_f32(self._handle, self.lib.set_speed, axis, value, 'set SPEED')

    def get_accel(self, axis: int) -> float:
        self.require_connection()
        return self.lib.get_f32(self._handle, self.lib.get_accel, axis, 'read ACCEL')

    def set_accel(self, axis: int, value: float) -> None:
        self.require_connection()
        self.lib.set_f32(self._handle, self.lib.set_accel, axis, value, 'set ACCEL')

    def get_decel(self, axis: int) -> float:
        self.require_connection()
        return self.lib.get_f32(self._handle, self.lib.get_decel, axis, 'read DECEL')

    def set_decel(self, axis: int, value: float) -> None:
        self.require_connection()
        self.lib.set_f32(self._handle, self.lib.set_decel, axis, value, 'set DECEL')

    def get_creep(self, axis: int) -> float:
        self.require_connection()
        return self.lib.get_f32(self._handle, self.lib.get_creep, axis, 'read CREEP')

    def set_creep(self, axis: int, value: float) -> None:
        self.require_connection()
        self.lib.set_f32(self._handle, self.lib.set_creep, axis, value, 'set CREEP')

    def get_jog_speed(self, axis: int) -> float:
        self.require_connection()
        return self.lib.get_f32(self._handle, self.lib.get_jog_speed, axis, 'read JOG speed')

    def set_jog_speed(self, axis: int, value: float) -> None:
        self.require_connection()
        self.lib.set_f32(self._handle, self.lib.set_jog_speed, axis, value, 'set JOG speed')

    def get_invert_step(self, axis: int) -> int:
        self.require_connection()
        return self.lib.get_i32(self._handle, self.lib.get_invert_step, axis, 'read INVERT_STEP')

    def set_invert_step(self, axis: int, value: int) -> None:
        self.require_connection()
        self.lib.set_i32(self._handle, self.lib.set_invert_step, axis, value, 'set INVERT_STEP')

    def get_datum_in(self, axis: int) -> int:
        self.require_connection()
        return self.lib.get_i32(self._handle, self.lib.get_datum_in, axis, 'read DATUM_IN')

    def set_datum_in(self, axis: int, value: int) -> None:
        self.require_connection()
        self.lib.set_i32(self._handle, self.lib.set_datum_in, axis, value, 'set DATUM_IN')

    def get_home_wait(self, axis: int) -> int:
        self.require_connection()
        return self.lib.get_i32(self._handle, self.lib.get_home_wait, axis, 'read HOME_WAIT')

    def set_home_wait(self, axis: int, value: int) -> None:
        self.require_connection()
        self.lib.set_i32(self._handle, self.lib.set_home_wait, axis, value, 'set HOME_WAIT')

    def get_fs_limit(self, axis: int) -> float:
        self.require_connection()
        return self.lib.get_f32(self._handle, self.lib.get_fs_limit, axis, 'read FS_LIMIT')

    def set_fs_limit(self, axis: int, value: float) -> None:
        self.require_connection()
        self.lib.set_f32(self._handle, self.lib.set_fs_limit, axis, value, 'set FS_LIMIT')

    def get_rs_limit(self, axis: int) -> float:
        self.require_connection()
        return self.lib.get_f32(self._handle, self.lib.get_rs_limit, axis, 'read RS_LIMIT')

    def set_rs_limit(self, axis: int, value: float) -> None:
        self.require_connection()
        self.lib.set_f32(self._handle, self.lib.set_rs_limit, axis, value, 'set RS_LIMIT')

    # -- axis state ------------------------------------------------------
    def get_dpos(self, axis: int) -> float:
        """Commanded position in user units."""
        self.require_connection()
        return self.lib.get_f32(self._handle, self.lib.get_dpos, axis, 'read DPOS')

    def get_mpos(self, axis: int) -> float:
        """Feedback (encoder) position in user units."""
        self.require_connection()
        return self.lib.get_f32(self._handle, self.lib.get_mpos, axis, 'read MPOS')

    def set_dpos(self, axis: int, value: float) -> None:
        """Redefine the coordinate origin without moving the motor."""
        self.require_connection()
        self.lib.set_f32(self._handle, self.lib.set_dpos, axis, value, 'set DPOS')

    def if_idle(self, axis: int) -> bool:
        """``True`` when the axis has finished (or never started) moving."""
        self.require_connection()
        return self.lib.get_i32(self._handle, self.lib.get_if_idle, axis, 'read IDLE') != 0

    def home_status(self, axis: int) -> int:
        """``1`` when the last homing cycle succeeded, ``0`` otherwise."""
        self.require_connection()
        return self.lib.get_i32(self._handle, self.lib.get_home_status, axis, 'read HOME_STATUS')

    def axis_status(self, axis: int) -> int:
        self.require_connection()
        return self.lib.get_i32(self._handle, self.lib.get_axis_status, axis, 'read AXIS_STATUS')

    def axis_stop_reason(self, axis: int) -> int:
        self.require_connection()
        return self.lib.get_i32(self._handle, self.lib.get_axis_stop_reason, axis, 'read stop reason')

    def axis_enable(self, axis: int) -> int:
        self.require_connection()
        return self.lib.get_i32(self._handle, self.lib.get_axis_enable, axis, 'read AXIS_ENABLE')

    def set_axis_enable(self, axis: int, value: int) -> None:
        self.require_connection()
        self.lib.set_i32(self._handle, self.lib.set_axis_enable, axis, value, 'set AXIS_ENABLE')

    def measured_speed(self, axis: int) -> float:
        self.require_connection()
        return self.lib.get_f32(self._handle, self.lib.get_mspeed, axis, 'read MSPEED')

    def read_all_axis_info(self, count: int) -> dict[str, np.ndarray]:
        """Read idle flag, DPOS, MPOS and AXIS_STATUS for ``count`` axes in one round trip."""
        self.require_connection()
        idle = (ctypes.c_int32 * count)()
        dpos = (ctypes.c_float * count)()
        mpos = (ctypes.c_float * count)()
        status = (ctypes.c_int32 * count)()
        code = self.lib.get_all_axis_info(
            self._handle, ctypes.c_int32(count),
            idle, dpos, mpos, status,
        )
        self.lib._check(code, 'read all axis info')
        return {
            'idle': np.array([int(v) for v in idle], dtype=int),
            'dpos': np.array([float(v) for v in dpos], dtype=float),
            'mpos': np.array([float(v) for v in mpos], dtype=float),
            'status': np.array([int(v) for v in status], dtype=int),
        }

    # -- motion ----------------------------------------------------------
    def move_absolute(self, axis: int, position: float) -> None:
        """Move to an absolute position expressed in user units."""
        self.require_connection()
        self.lib._check(
            self.lib.move_abs(self._handle, ctypes.c_int32(axis), ctypes.c_float(position)),
            'MOVEABS')

    def move_relative(self, axis: int, distance: float) -> None:
        """Move by a signed distance expressed in user units."""
        self.require_connection()
        self.lib._check(
            self.lib.move_rel(self._handle, ctypes.c_int32(axis), ctypes.c_float(distance)),
            'MOVE')

    def jog(self, axis: int, direction: int) -> None:
        """Start continuous motion. ``direction`` is ``+1`` or ``-1``."""
        self.require_connection()
        if direction not in (1, -1):
            raise ValueError('jog direction must be +1 or -1')
        self.lib._check(
            self.lib.vmove(self._handle, ctypes.c_int32(axis), ctypes.c_int32(direction)),
            'VMOVE')

    def stop_axis(self, axis: int, mode: int = 2) -> None:
        """Cancel motion on one axis. ``mode`` 2 also clears the motion buffer."""
        self.require_connection()
        self.lib._check(
            self.lib.cancel(self._handle, ctypes.c_int32(axis), ctypes.c_int32(mode)),
            'CANCEL')

    def stop_axes(self, axes: Iterable[int], mode: int = 2) -> None:
        """Cancel motion on several axes with a single round trip."""
        self.require_connection()
        axis_list = [int(a) for a in axes]
        if not axis_list:
            return
        buffer = (ctypes.c_int32 * len(axis_list))(*axis_list)
        self.lib._check(
            self.lib.cancel_axis_list(self._handle, ctypes.c_int32(len(axis_list)), buffer,
                                      ctypes.c_int32(mode)),
            'CANCEL axis list')

    def emergency_stop(self) -> None:
        """Rapid stop of every axis on the controller.

        This is a software action and does not replace a hardware interlock.
        """
        if not self.connected:
            return
        self.lib._check(self.lib.rapidstop(self._handle, ctypes.c_int32(0)), 'RAPIDSTOP')

    def home_axis(self, axis: int, mode: int = 1) -> None:
        """Run a homing cycle on one axis.

        ``mode`` selects the controller's homing strategy; the default (1) searches the
        origin input in the negative direction.
        """
        self.require_connection()
        self.lib._check(
            self.lib.datum(self._handle, ctypes.c_int32(axis), ctypes.c_int32(mode)),
            'DATUM')

    def wait_idle(self, axes: Sequence[int], timeout: float) -> bool:
        """Block until every listed axis reports idle. ``False`` means timeout."""
        import time
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            if all(self.if_idle(axis) for axis in axes):
                return True
            time.sleep(0.02)
        return False

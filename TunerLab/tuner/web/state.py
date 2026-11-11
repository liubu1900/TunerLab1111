"""Shared application state for the web console.

Two independent sub-systems live here:

* ``hardware`` — the real Zmotion controller, owned by a single
  :class:`tuner.ui_hardware.HardwareSession`. All hardware access goes through it
  so the web console and the desktop console never fight over the link.
* ``studio`` — the Mock calibration workflow (virtual motion, synthetic VNA,
  HDF5 database, gamma search). It is simulation-only and shares no hardware
  with the real controller.

Both sub-systems are guarded by their own re-entrant locks because HTTP handlers
run on different threads of the :class:`http.server.ThreadingHTTPServer`.
"""
from __future__ import annotations

import threading
from typing import Any

import numpy as np

from ..ui_hardware import HardwareSession

#: Default VNA sweep used by the Mock studio (1–20 GHz, 41 points).
DEFAULT_FREQUENCIES = tuple(float(f) for f in np.linspace(1e9, 20e9, 41))


class AppState:
    """Process-wide singleton holding the hardware link and the studio workflow."""

    def __init__(self) -> None:
        # --- hardware ----------------------------------------------------
        self.hw: HardwareSession = HardwareSession()
        # Serialises normal motion / parameter / connection operations so two of
        # them never drive the controller at once.
        self.hw_lock = threading.RLock()
        # A *separate* lock for stop / emergency-stop / reset.  Those commands must
        # always be able to fire, even while a normal move is holding ``hw_lock``
        # for the duration of a blocking wait -- otherwise a stuck move could
        # delay the only way to halt the machine (P0-01).
        self.hw_emergency_lock = threading.Lock()

        # --- studio (Mock calibration) ----------------------------------
        self.studio_lock = threading.RLock()
        self.studio: dict[str, Any] = {
            'config': None,
            'motion': None,
            'motion_source': 'mock',
            'homed_axes': [],
            'planner': None,
            'vna': None,
            'manager': None,
            'database': None,
            'data': None,
            'search_engine': None,
            'last_candidates': [],
            'last_tuned': None,
            'validation_report': None,
            'interpolation_report': None,
            'calibration_thread': None,
            'calibration_progress': (0, 0),
            'calibration_state': 'idle',
            'frequency_hz': 10e9,
            # --- P0 (0.4.0) -------------------------------------------
            'frequency_mode': 'fo_list',
            'frequency_plan': None,
            'prerequisite_report': None,
            'interpolation_mode': None,
            'interpolation_error': None,
            'plan_table': None,
            'point_plan': 'cartesian',
        }

        # --- VNA (real vector network analyzer, TCP/IP or VISA GPIB) ------
        # Separate instrument from the Zmotion motion controller: a VNA is a
        # different box on the LAN / GPIB bus and is driven by
        # tuner.vna_drivers.TCPVNA over a pluggable transport.
        self.vna_lock = threading.RLock()
        self.vna: dict[str, Any] = {
            'controller': None,
            'connected': False,
            'idn': None,
            'vendor': None,
            'last_error': None,
            'config': {
                'host': '192.168.2.111', 'port': 5025, 'timeout_ms': 10000,
                'vendor_key': 'auto',
                'interface': 'tcpip',
                'resource': 'GPIB0::17::INSTR',
                #: SCPI commands pinned by the debug tool's probe (in-memory).
                'dialect': {},
                'start_hz': 1e9, 'stop_hz': 20e9, 'points': 401,
                'power_dbm': -10.0, 'ifbw_hz': 1000.0,
            },
        }

    # -- hardware helpers ------------------------------------------------
    @property
    def hw_connected(self) -> bool:
        with self.hw_lock:
            return self.hw.connected

    def hw_status(self) -> dict[str, Any]:
        """Snapshot of the connection plus a live position reading."""
        with self.hw_lock:
            link = self.hw.link
            controller = self.hw.controller
            snapshot = []
            if controller is not None and self.hw.connected:
                try:
                    snapshot = controller.axis_status_snapshot()
                except Exception:  # noqa: BLE001 - never break the poll
                    snapshot = []
            return {
                'connected': self.hw.connected,
                'ip': self.hw.settings.connection.ip if self.hw.settings else '',
                'firmware': link.firmware if link else None,
                'address': link.ip if link else None,
                'library': self._library_path(),
                'settings_path': str(self.hw.store.path),
                'axis_count': (self.hw.settings.axis_count
                               if self.hw.settings else 0),
                'mode': (self.hw.settings.mode if self.hw.settings else 'real'),
                'active_axes': (list(self.hw.axes) if self.hw.settings else []),
                'axes': snapshot,
            }

    @staticmethod
    def _library_path() -> str:
        from ..zmotion import vendor_dir
        path = vendor_dir() / 'zauxdll.dll'
        return str(path) if path.is_file() else f'{path} (missing)'

    # -- studio helpers --------------------------------------------------
    def studio_overview(self) -> dict[str, Any]:
        with self.studio_lock:
            s = self.studio
            data = s['data']
            valid = int(np.count_nonzero(data['valid'])) if data is not None else 0
            total = len(data['valid']) if data is not None else 0
            freqs = data['frequencies'] if data is not None else None
            return {
                'has_hardware': s['motion'] is not None,
                'vna_source': s.get('vna_source', 'mock'),
                'motion_source': s.get('motion_source', 'mock'),
                'homed_axes': list(s.get('homed_axes', [])),
                'config': (
                    _config_to_dict(s['config']) if s['config'] is not None else None),
                'has_database': s['database'] is not None,
                'database_path': str(s['database'].path) if s['database'] else None,
                'valid': valid,
                'total': total,
                'frequencies': (
                    [float(freqs[0]), float(freqs[-1])] if freqs is not None else None),
                'has_search': s['search_engine'] is not None,
                'calibration_state': s['calibration_state'],
                'calibration_progress': list(s['calibration_progress']),
                'frequency_hz': s['frequency_hz'],
                'validation_report': s['validation_report'],
                # P0 status bar / prerequisite gate
                'frequency_mode': s.get('frequency_mode', 'fo_list'),
                'frequency_plan': s.get('frequency_plan'),
                'point_plan': s.get('point_plan', 'cartesian'),
                'interpolation_mode': s.get('interpolation_mode'),
                'prerequisite_passed': bool(
                    (s.get('prerequisite_report') or {}).get('passed', False)),
                'database_cal_id': _database_cal_id(s.get('database')),
            }

    # -- VNA helpers -----------------------------------------------------
    def vna_status(self) -> dict[str, Any]:
        with self.vna_lock:
            v = self.vna
            cfg = v['config']
            controller = v['controller']
            live_cfg = controller.get_config() if controller is not None else None
            return {
                'connected': v['connected'],
                'idn': v['idn'],
                'vendor': v['vendor'],
                'last_error': v['last_error'],
                'config': cfg,
                'live_config': live_cfg,
            }


def _database_cal_id(database) -> str:
    """Best-effort schema-v2 Calibration ID for the status bar.

    Old v1 libraries simply return an empty string; a broken/unreadable file
    must never break the overview poll.
    """
    if database is None:
        return ''
    try:
        if not database.path.is_file():
            return ''
        return str(database.read_metadata().get('cal_id', ''))
    except Exception:  # noqa: BLE001 - status bar is informational only
        return ''


def _config_to_dict(config) -> dict[str, Any]:
    from dataclasses import asdict
    return asdict(config)


#: The single instance shared by every request handler.
state = AppState()

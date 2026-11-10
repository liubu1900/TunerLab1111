"""TunerLab web console.

A dependency-free (standard library only) local web server that hosts the same
backend the desktop application uses:

* the real Zmotion hardware through :class:`tuner.ui_hardware.HardwareSession`,
* the Mock calibration studio through the pure-Python ``motion`` / ``vna`` /
  ``calibration`` / ``database`` / ``search`` / ``rf`` modules.

Run with ``python -m tuner.web``. The server binds to ``127.0.0.1`` and opens a
browser. The Zmotion DLL is Windows-only, so this must be launched on the
machine physically connected to the controller; the browser may be on the same
host or anywhere on the LAN.
"""
from __future__ import annotations

from .server import main

__all__ = ['main']

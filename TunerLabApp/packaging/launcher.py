"""Entry script for the frozen (PyInstaller) build.

PyInstaller needs a real script rather than a ``-m`` module invocation, so this
file only forwards to the application's ``main``.
"""
from __future__ import annotations

import sys


def run() -> int:
    from tunerlab_app.shell import main
    return main()


if __name__ == '__main__':
    sys.exit(run())

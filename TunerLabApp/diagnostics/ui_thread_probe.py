"""Reproduce the silent crash: widget updates from a worker thread.

``AxisMotionPage._run_motion`` sends the blocking move to ``QThreadPool``, but
``_jog`` / ``_absolute`` / ``_home`` / ``_probe_scale`` finish by calling
``self.refresh_positions()`` -- which writes into ``QTableWidget``, ``QLabel``
and ``QProgressBar``.  Qt widgets may only be touched from the GUI thread, so
this walks straight into undefined behaviour: the process dies without a Python
traceback.

This script drives exactly that path (a background thread calling
``refresh_positions`` in a tight loop) so the failure can be observed and then
verified as fixed.

    python diagnostics/ui_thread_probe.py --iterations 20000
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--iterations', type=int, default=20000)
    parser.add_argument('--threads', type=int, default=1)
    args = parser.parse_args()

    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

    import tunerlab_app  # noqa: F401 - puts the original project on sys.path
    from PySide6.QtWidgets import QApplication
    from tuner.settings import SettingsStore
    from tunerlab_app.pages_motion import SafeAxisMotionPage
    from tunerlab_app.session import TunerSession

    app = QApplication([])
    session = TunerSession(SettingsStore())
    session.connect(mode='virtual')
    page = SafeAxisMotionPage(session)
    page.resize(1200, 800)
    page.show()
    for _ in range(20):
        app.processEvents()

    stop = threading.Event()
    done = [0] * args.threads
    errors = [None] * args.threads

    def worker(slot: int) -> None:
        count = 0
        try:
            while not stop.is_set() and count < args.iterations:
                page.refresh_positions()
                count += 1
        except BaseException as exc:  # noqa: BLE001 - reported
            errors[slot] = f'{type(exc).__name__}: {exc}'
        finally:
            done[slot] = count

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(args.threads)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    # Keep the GUI thread pumping so the timer path also runs concurrently.
    while any(thread.is_alive() for thread in threads):
        app.processEvents()
        if time.monotonic() - started > 120:
            break
    stop.set()
    for thread in threads:
        thread.join(timeout=5)
    print(f'worker refreshes: {done} in {time.monotonic() - started:.1f}s', flush=True)
    failures = [err for err in errors if err]
    print(f'worker exceptions: {failures[:3] if failures else "none"}', flush=True)
    print('survived', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Read-only probe: hammer one controller handle from several threads.

The motion page polls on the GUI thread every 300 ms while a click runs on a
worker thread, so two threads can be inside ``zauxdll`` at the same time.  This
script reproduces just that pattern -- no motion commands at all -- and reports
whether the process survives.

Run it in a subprocess: a native access violation kills the interpreter without
a Python traceback, and the exit code is then the only evidence.

    python diagnostics/dll_concurrency_probe.py --seconds 15 --threads 6
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
    parser.add_argument('--ip', default='192.168.0.11')
    parser.add_argument('--seconds', type=float, default=15.0)
    parser.add_argument('--threads', type=int, default=6)
    args = parser.parse_args()

    from tuner.zmotion import ZmotionController

    link = ZmotionController()
    link.open(args.ip, timeout=3.0, retries=2)
    print(f'connected: firmware={link.firmware!r}', flush=True)

    stop = threading.Event()
    counters = [0] * args.threads
    errors = [None] * args.threads

    def worker(slot: int) -> None:
        count = 0
        try:
            while not stop.is_set():
                link.get_dpos(0)
                link.get_mpos(0)
                link.axis_status(0)
                link.if_idle(0)
                link._basic_query('?DPOS(0)')
                count += 1
        except BaseException as exc:  # noqa: BLE001 - reported, not raised
            errors[slot] = f'{type(exc).__name__}: {exc}'
        finally:
            counters[slot] = count

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(args.threads)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    time.sleep(args.seconds)
    stop.set()
    for thread in threads:
        thread.join(timeout=5.0)

    elapsed = time.monotonic() - started
    total = sum(counters)
    print(f'rounds={counters} total={total} in {elapsed:.1f}s '
          f'({total / elapsed:.0f} round/s per {args.threads} threads)', flush=True)
    failures = [err for err in errors if err]
    if failures:
        print(f'thread errors ({len(failures)}): {failures[:3]}', flush=True)
    else:
        print('thread errors: none', flush=True)
    link.close()
    print('survived', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

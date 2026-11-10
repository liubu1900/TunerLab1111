"""Read-only hardware probe for the live bench.

Talks to the real Zmotion controller and the real VNA and prints what comes
back.  Nothing in here writes to a machine: every call is a query.  The point is
to establish that the two links are alive and that the driver layer speaks them
correctly, before any button is pressed.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'TunerLab'))
sys.path.insert(0, str(ROOT / 'TunerLabApp'))

CONTROLLER_IP = '192.168.0.11'
VNA_IP = '192.168.2.111'
VNA_PORT = 5025

#: Axis name -> controller axis index.
AXES = {'X': 0, 'Z1': 1, 'Z2': 2, 'Z3': 3}


def head(title: str) -> None:
    print()
    print('=' * 68)
    print(title)
    print('=' * 68)


def step(label: str, fn):
    """Run one probe, print PASS/FAIL, never raise."""
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001 - a probe must report, not abort
        print(f'  [FAIL] {label}: {type(exc).__name__}: {exc}')
        if '--verbose' in sys.argv:
            traceback.print_exc()
        return None, False
    print(f'  [ ok ] {label}: {value}')
    return value, True


def probe_controller() -> dict:
    from tuner.zmotion import ZmotionController, ZmotionLibrary, load_library

    head('A1. Zmotion controller - link')
    lib = ZmotionLibrary(load_library())
    print(f'  DLL loaded: {lib.dll._name}')
    ctl = ZmotionController(lib, CONTROLLER_IP)
    ctl.open(CONTROLLER_IP, timeout=5.0)
    print(f'  link opened: {ctl.ip}  firmware={ctl.firmware!r}')
    out: dict = {}

    head('A2. Zmotion controller - identity & status')
    step('firmware', ctl.read_firmware)
    step('error code', ctl.read_error_code)

    head('A3. Zmotion controller - per axis read-only snapshot')
    for name, idx in AXES.items():
        print(f'-- axis {name} (index {idx})')
        step(f'{name}.atype', lambda i=idx: ctl.get_atype(i))
        step(f'{name}.units', lambda i=idx: ctl.get_units(i))
        step(f'{name}.speed', lambda i=idx: ctl.get_speed(i))
        step(f'{name}.accel', lambda i=idx: ctl.get_accel(i))
        step(f'{name}.decel', lambda i=idx: ctl.get_decel(i))
        step(f'{name}.creep', lambda i=idx: ctl.get_creep(i))
        step(f'{name}.jog_speed', lambda i=idx: ctl.get_jog_speed(i))
        step(f'{name}.invert_step', lambda i=idx: ctl.get_invert_step(i))
        step(f'{name}.datum_in', lambda i=idx: ctl.get_datum_in(i))
        step(f'{name}.home_wait', lambda i=idx: ctl.get_home_wait(i))
        step(f'{name}.fs_limit', lambda i=idx: ctl.get_fs_limit(i))
        step(f'{name}.rs_limit', lambda i=idx: ctl.get_rs_limit(i))
        step(f'{name}.DPOS(mm)', lambda i=idx: ctl.get_dpos(i))
        step(f'{name}.MPOS(counts)', lambda i=idx: ctl.get_mpos(i))
        step(f'{name}.if_idle', lambda i=idx: ctl.if_idle(i))
        step(f'{name}.home_status', lambda i=idx: ctl.home_status(i))
        step(f'{name}.axis_status', lambda i=idx: ctl.axis_status(i))
        step(f'{name}.axis_stop_reason', lambda i=idx: ctl.axis_stop_reason(i))
        step(f'{name}.axis_enable', lambda i=idx: ctl.axis_enable(i))
        step(f'{name}.measured_speed', lambda i=idx: ctl.measured_speed(i))

    head('A4. Zmotion controller - bulk axis info')
    step('read_all_axis_info(4)', lambda: ctl.read_all_axis_info(4))
    step('basic query INPOSITION(0)', lambda: ctl._basic_query('INPOSITION(0)'))
    step('basic query SPEED(0)', lambda: ctl._basic_query('SPEED(0)'))

    head('A5. Zmotion controller - close')
    ctl.close()
    print('  link closed cleanly')
    return out


def probe_vna() -> dict:
    from tuner.vna_drivers import create_vna

    head('B1. VNA - connect')
    vna = create_vna('auto', VNA_IP, VNA_PORT, timeout=12.0)
    idn = step('*IDN?', lambda: vna.connect())
    if not idn:
        print('  VNA did not identify - stopping VNA probe')
        return {}
    step('vendor detect', lambda: vna.vendor_key)
    step('config readback', lambda: vna.get_config())

    head('B2. VNA - error queue')
    step('read_errors', vna.read_errors)

    head('B3. VNA - sweep configuration')
    step('set_sweep(1e9..6e9, 21 pts)', lambda: vna.set_sweep(1e9, 6e9, 21))
    step('set_power(-15 dBm)', lambda: vna.set_power(-15.0))
    step('set_ifbw(1000 Hz)', lambda: vna.set_ifbw(1000.0))
    step('config readback', lambda: vna.get_config())

    head('B4. VNA - single sweep & trace fetch')
    step('trigger_single', vna.trigger_single)
    traces = step('fetch_all', vna.fetch_all)
    if isinstance(traces, dict):
        for key, arr in traces.items():
            arr = arr if arr is not None else []
            n = len(arr)
            if n:
                print(f'      {key}: {n} pts, first={arr[0]}, last={arr[-1]}')
            else:
                print(f'      {key}: EMPTY')
    meas = step('measure()', vna.measure)
    if meas is not None:
        print(f'      measurement: {meas}')

    head('B5. VNA - close')
    vna.close()
    print('  socket closed cleanly')
    return {}


def main() -> int:
    print('TunerLab live hardware probe')
    print(f'controller {CONTROLLER_IP} | vna {VNA_IP}:{VNA_PORT}')
    failed = 0
    for name, fn in (('controller', probe_controller), ('vna', probe_vna)):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f'!! {name} probe aborted: {type(exc).__name__}: {exc}')
            if '--verbose' in sys.argv:
                traceback.print_exc()
    head('RESULT')
    print('probe finished; check the [FAIL] lines above')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())

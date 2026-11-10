"""Live-bench verification of the 0.5.7 UI fixes (dist_057 feature set).

Runs against the real Zmotion controller and the real VNA, loads the real
1152-point calibration library and exercises the three fixes with *real* Qt
events rather than by calling methods directly:

  1. progress / completion state on StudioRunPage
  2. the live Smith Γ cloud fed from the real HDF5 library
  3. wheel zoom, cursor-anchored Γ picking, middle-drag pan, double-click
     reset and the explicit zoom buttons on both Smith widget families

``--with-motion`` additionally performs a short, controlled X-axis round trip
(+2 mm and back) with a real VNA read, and restores the original DPOS exactly
(the bench is left wherever the operator parked it).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'TunerLab'))
sys.path.insert(0, str(ROOT / 'TunerLabApp'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

DEFAULT_DB = (r'C:\Users\Eoulu\Documents\TunerLab\calibrations'
              r'\cal_20261109_181953.h5')
OUT = ROOT / 'verification' / 'out' / 'live_057.json'

CONTROLLER_IP = '192.168.0.11'
VNA_IP = '192.168.2.111'


class Report:
    def __init__(self):
        self.checks: list[dict] = []
        self.notes: list[str] = []

    def check(self, name: str, ok: bool, detail: str = '') -> None:
        self.checks.append({'name': name, 'ok': bool(ok), 'detail': str(detail)})
        print(f'  [{"PASS" if ok else "FAIL"}] {name}: {detail}', flush=True)

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f'  [info] {text}', flush=True)

    @property
    def failures(self) -> int:
        return sum(1 for c in self.checks if not c['ok'])


def head(title: str) -> None:
    print(f'\n{"=" * 72}\n{title}\n{"=" * 72}', flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--with-motion', action='store_true')
    ap.add_argument('--no-hardware', action='store_true',
                    help='skip studio_init_hardware (library + UI checks only)')
    args = ap.parse_args()

    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent, QWheelEvent
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    rep = Report()

    head('0. Hardware link & real application shell')
    from tunerlab_app.shell import MainWindow
    window = MainWindow(smoke_test=False)
    window.resize(1440, 940)
    window.show()
    app.processEvents()

    deadline = time.time() + 8
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
        if window.session.connected:
            break
    app.processEvents()
    rep.check('controller link', bool(window.session.connected),
              f'ip={CONTROLLER_IP} connected={window.session.connected}')
    rep.note(f'pages built: {len(window.pages)}')

    # -- read-only machine snapshot --------------------------------------
    ctl = None
    try:
        ctl = window.session.require_controller()
        dpos = [round(float(ctl.link.get_dpos(i)), 4) for i in range(4)]
        idle = [bool(ctl.link.if_idle(i)) for i in range(4)]
        rep.note(f'DPOS(mm)={dpos} idle={idle} '
                 f'homed_axes={sorted(getattr(ctl, "homed_axes", []))} '
                 f'faulted={getattr(ctl, "faulted", None)}')
        rep.check('axes idle before test', all(idle), f'idle={idle}')
    except Exception as exc:  # noqa: BLE001
        rep.note(f'read-only controller snapshot unavailable: {exc}')

    # -- real studio initialisation --------------------------------------
    from tuner.web import handlers
    if not args.no_hardware:
        head('1. Real studio initialisation (motion + VNA)')
        try:
            handlers.studio_init_hardware(window.state, {
                'motion_source': 'real',
                'x_min': 0.0, 'x_max': 380.0,
                'z_min': 0.0, 'z_max': 10.0,
                'safe_z': 0.0,
                'vna_source': 'real', 'vna_vendor': 'auto',
                'vna_ip': VNA_IP, 'vna_port': 5025,
                'vna_start_hz': 5e9, 'vna_stop_hz': 5e9, 'vna_points': 1,
                'vna_timeout_ms': 15000,
            })
            studio = window.state.studio
            cfg = studio.get('config')
            rep.check('studio motion planner', studio.get('planner') is not None,
                      f'planner={type(studio.get("planner")).__name__}')
            rep.check('studio real VNA', studio.get('vna') is not None,
                      f'vna={type(studio.get("vna")).__name__}')
            timeout = getattr(cfg, 'motion_timeout', None)
            rep.note(f'effective motion_timeout = {timeout} s')
        except Exception as exc:  # noqa: BLE001
            rep.note(f'studio_init_hardware failed: {type(exc).__name__}: {exc}')
            if '--verbose' in sys.argv:
                traceback.print_exc()

    # -- load the real library -------------------------------------------
    head('2. Load the real 1152-point calibration library')
    db_path = Path(args.db)
    rep.check('library exists', db_path.is_file(), str(db_path))
    try:
        overview = handlers.studio_open_db(window.state, {'path': str(db_path)})
        rep.note(f'overview: valid={overview.get("valid_points")} '
                 f'total={overview.get("total_points")}')
    except Exception as exc:  # noqa: BLE001
        rep.check('studio_open_db', False, f'{type(exc).__name__}: {exc}')
        return 1

    # -- handler-level authoritative counts ------------------------------
    head('3. Authoritative progress & live Γ snapshot (handlers)')
    task = handlers.studio_task(window.state, {})
    done, total = task.get('completed'), task.get('total_points')
    rep.check('studio_task completed == total',
              done == total and total == 1152,
              f'completed={done} total_points={total} '
              f'legacy_progress={task.get("progress")} state={task.get("state")}')
    snap = handlers.studio_live_smith(window.state, {'frequency': None})
    pts = snap.get('points') or []
    rep.check('live Smith Γ cloud size', len(pts) == 1152,
              f'points={len(pts)} freq={snap.get("frequency_hz")} '
              f'freqs={snap.get("frequencies")}')
    finite = all(abs(complex(p['re'], p['im'])) <= 1.5 for p in pts[:50])
    rep.check('Γ values finite & in range', finite,
              f'first={pts[0] if pts else None}')
    rep.check('latest point present', snap.get('latest') is not None,
              f'latest={snap.get("latest")}')

    # -- real UI page ----------------------------------------------------
    head('4. StudioRunPage - real widgets, real refresh')
    page = window.pages.get('studio-run')
    rep.check('StudioRunPage present', page is not None,
              f'page={type(page).__name__}')
    window.go('studio-run')
    app.processEvents()
    page.refresh()
    app.processEvents()
    counter = page.counter.text()
    pct = page.progress.value()
    pill_text = page.state_pill.text() if hasattr(page.state_pill, 'text') else ''
    rep.check('counter reads 1152 / 1152', counter == '1152 / 1152',
              f'counter={counter!r}')
    rep.check('progress bar reads 100%', pct == 100, f'value={pct}')
    rep.check('state pill reads 已完成', pill_text == '已完成',
              f'pill={pill_text!r}')
    cloud_pts = len(page.live_smith.points)
    rep.check('live Smith widget holds 1152 Γ', cloud_pts == 1152,
              f'points={cloud_pts}')
    rep.check('frequency picker populated',
              page.smith_frequency.count() == 1,
              f'count={page.smith_frequency.count()} '
              f'text={page.smith_frequency.currentText()!r}')
    caption = page.smith_caption.text()
    rep.check('Smith caption shows full count', '1152 / 1152' in caption,
              f'caption={caption!r}')
    rep.check('latest point highlighted', page.live_smith.selected is not None,
              f'selected={page.live_smith.selected}')

    # -- real Qt events --------------------------------------------------
    head('5. Real Qt events on the live Smith chart')
    chart = page.live_smith
    chart.resize(520, 460)
    app.processEvents()
    W, H = chart.width(), chart.height()
    ax, ay = W / 2.0 + 34.0, H / 2.0 - 22.0
    inset = chart._INSET

    def wheel(x, y, delta):
        ev = QWheelEvent(QPointF(x, y), QPointF(x, y), QPoint(),
                         QPoint(0, delta), Qt.MouseButton.NoButton,
                         Qt.KeyboardModifier.NoModifier,
                         Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(chart, ev)

    def mouse(kind, x, y, button, buttons, modifiers=Qt.KeyboardModifier.NoModifier):
        ev = QMouseEvent(kind, QPointF(x, y), QPointF(x, y),
                         button, buttons, modifiers)
        QApplication.sendEvent(chart, ev)

    g0 = chart.view.to_gamma(ax, ay, W, H, inset)
    rep.note(f'anchor pixel=({ax:.0f},{ay:.0f}) Γ0={g0:.5f} zoom0={chart.view.zoom:.3f}')
    for _ in range(3):
        wheel(ax, ay, 120)
    z1 = chart.view.zoom
    g1 = chart.view.to_gamma(ax, ay, W, H, inset)
    rep.check('wheel event zooms in', z1 > 1.2,
              f'zoom {1.0:.3f} -> {z1:.3f}, badge={chart.zoom_text()}')
    rep.check('cursor-anchored Γ unchanged after zoom',
              abs(g1 - g0) < 1e-9, f'Γ0={g0:.6f} Γ1={g1:.6f} err={abs(g1-g0):.3g}')

    picked = []
    chart.picked.connect(lambda g: picked.append(g))
    mouse(QEvent.Type.MouseButtonPress, ax, ay,
          Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    app.processEvents()
    rep.check('real left-click emits the correct Γ',
              len(picked) == 1 and abs(picked[0] - g0) < 1e-9,
              f'picked={picked} expected={g0}')

    # Middle-button drag pans by exactly the mouse displacement.
    pan0 = (chart.view.pan_x, chart.view.pan_y)
    mouse(QEvent.Type.MouseButtonPress, ax, ay,
          Qt.MouseButton.MiddleButton, Qt.MouseButton.MiddleButton)
    mouse(QEvent.Type.MouseMove, ax + 24, ay - 13,
          Qt.MouseButton.MiddleButton, Qt.MouseButton.MiddleButton)
    mouse(QEvent.Type.MouseButtonRelease, ax + 24, ay - 13,
          Qt.MouseButton.MiddleButton, Qt.MouseButton.NoButton)
    pan1 = (chart.view.pan_x, chart.view.pan_y)
    rep.check('middle-drag pans the view',
              abs((pan1[0]-pan0[0]) - 24) < 1e-6 and
              abs((pan1[1]-pan0[1]) + 13) < 1e-6,
              f'pan {pan0} -> {pan1}')

    mouse(QEvent.Type.MouseButtonDblClick, W/2, H/2,
          Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    rep.check('double-click resets the view',
              abs(chart.view.zoom - 1.0) < 1e-9 and
              abs(chart.view.pan_x) < 1e-9 and abs(chart.view.pan_y) < 1e-9,
              f'zoom={chart.view.zoom} pan=({chart.view.pan_x},{chart.view.pan_y})')

    page.smith_zoom_in.click()
    z_in = chart.view.zoom
    page.smith_zoom_out.click()
    z_out = chart.view.zoom
    page.smith_zoom_reset.click()
    z_reset = chart.view.zoom
    rep.check('放大 / 缩小 / 复位 buttons',
              z_in > 1.0 and abs(z_out - 1.0) < 1e-9 and
              abs(z_reset - 1.0) < 1e-9,
              f'zoom_in={z_in:.3f} zoom_out={z_out:.3f} reset={z_reset:.3f}')

    # -- second widget family -------------------------------------------
    head('6. Real Qt events on the wizard SmithPreview')
    from tunerlab_app.ui import SmithPreview
    preview = SmithPreview()
    preview.resize(320, 300)
    preview.set_points(64)
    app.processEvents()
    pw, ph = preview.width(), preview.height()
    px, py = pw / 2.0 + 20, ph / 2.0 - 15

    def pwheel(x, y, delta):
        ev = QWheelEvent(QPointF(x, y), QPointF(x, y), QPoint(),
                         QPoint(0, delta), Qt.MouseButton.NoButton,
                         Qt.KeyboardModifier.NoModifier,
                         Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(preview, ev)

    pwheel(px, py, 120)
    pwheel(px, py, 120)
    pz = preview.view.zoom
    preview.reset_view()
    rep.check('SmithPreview wheel zoom + reset',
              pz > 1.2 and abs(preview.view.zoom - 1.0) < 1e-9,
              f'zoomed={pz:.3f} reset={preview.view.zoom:.3f}')

    # -- optional real motion -------------------------------------------
    if args.with_motion:
        head('7. Controlled real motion round trip (+2 mm X and back)')
        try:
            import numpy as np
            planner = window.state.studio.get('planner')
            vna = window.state.studio.get('vna')
            rep.check('planner & VNA available',
                      planner is not None and vna is not None,
                      f'planner={planner is not None} vna={vna is not None}')
            # Use the planner's OWN controller - never a second link object.
            motion = planner.controller
            rep.check('planner controller == session controller',
                      motion is ctl, f'same object={motion is ctl}')

            pos0 = np.asarray(motion.read_position(), dtype=float).copy()
            rep.note(f'start DPOS = {pos0.tolist()}')
            if not getattr(motion, 'homed_axes', None):
                for axis in motion.axes:
                    motion.home_via_output(axis)
                rep.note(f'OP-homed axes: {sorted(motion.homed_axes)}')

            target = pos0.copy()
            target[0] = pos0[0] + 2.0
            # Hard guard: this verification must never issue a long move.
            travel = float(np.max(np.abs(target - pos0)))
            rep.check('planned travel is a short nudge (<= 5 mm)',
                      travel <= 5.0, f'travel={travel:.3f} mm')
            if travel > 5.0:
                raise RuntimeError('refusing to move more than 5 mm')
            planner.move_to(target)
            pos1 = np.asarray(motion.read_position(), dtype=float).copy()
            rep.check('X +2 mm reached', abs(pos1[0] - target[0]) < 0.05,
                      f'target={target[0]:.3f} readback={pos1[0]:.3f}')

            meas = vna.measure()
            rep.check('real VNA measure() after move', meas is not None,
                      f'measurement shape={getattr(meas, "shape", None)}')

            planner.move_to(pos0)
            pos2 = np.asarray(motion.read_position(), dtype=float).copy()
            rep.check('returned to start DPOS',
                      float(np.max(np.abs(pos2 - pos0))) < 0.05,
                      f'delta={np.max(np.abs(pos2-pos0)):.4f} mm')

            # Leave the bench exactly as we found it (the operator parked the
            # axes here after the 1152-point sweep - do NOT re-home them).
            pos3 = np.asarray(motion.read_position(), dtype=float).copy()
            rep.note(f'final DPOS = {pos3.tolist()} '
                     f'faulted={getattr(motion, "faulted", None)} '
                     f'homed={sorted(getattr(motion, "homed_axes", []))}')
            rep.check('machine restored, no latched fault',
                      float(np.max(np.abs(pos3 - pos0))) < 0.05 and
                      not getattr(motion, 'faulted', False),
                      f'delta={float(np.max(np.abs(pos3-pos0))):.4f} mm')
        except Exception as exc:  # noqa: BLE001
            rep.check('controlled motion round trip', False,
                      f'{type(exc).__name__}: {exc}')
            traceback.print_exc()

    # -- report ----------------------------------------------------------
    head('RESULT')
    print(f'checks: {len(rep.checks)}  passed: {len(rep.checks)-rep.failures}  '
          f'failed: {rep.failures}', flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        'library': str(db_path),
        'with_motion': args.with_motion,
        'checks': rep.checks,
        'notes': rep.notes,
        'failures': rep.failures,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'report: {OUT}', flush=True)
    try:
        window.session.disconnect()
    except Exception:  # noqa: BLE001
        pass
    return 1 if rep.failures else 0


if __name__ == '__main__':
    raise SystemExit(main())

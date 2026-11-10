"""Button-by-button sweep of the live TunerLab window.

Builds the real :class:`MainWindow` against the real controller and the real
VNA, walks every navigation page, clicks every button and records what
happened: unhandled exceptions, modal dialogs, status-bar messages and new
operation-log rows.

Motion buttons are skipped unless ``--with-motion`` is given.  The About page
states the rule this follows: *"各轴独立运动：X 轴不再受探针位置约束，软件也不会
自动回缩探针；移动 X 前的碰撞判断由操作员与硬件联锁负责"* — an unattended sweep
must not move the probes.

Usage::

    python verification/sweep_buttons.py [--with-motion] [--settle 900]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'TunerLab'))
sys.path.insert(0, str(ROOT / 'TunerLabApp'))

OUT_DIR = ROOT / 'verification' / 'out'

#: Button texts that drive the machine.  Skipped unless --with-motion is given.
MOTION_MARKERS = (
    '点动', '定位', '回零', '标度自检', '移动', '自动 A→B→A',
    '回缩', '开始 / 继续', '重复性验证', '插值验证',
)

#: Buttons that would leave the app in a state the next test cannot use.
#: They are still clicked, but the harness notes the state change.
STATEFUL_MARKERS = ('断开', '取消', '清空', '清除')

#: Buttons that drop the link.  They are deferred to the end of the sweep so
#: the pages after them are still tested against live hardware.
LINK_MARKERS = ('断开',)


class Sweep:
    def __init__(self, settle_ms: int, with_motion: bool):
        self.settle_ms = settle_ms
        self.with_motion = with_motion
        self.records: list[dict] = []
        self.dialogs: list[dict] = []
        self.exceptions: list[dict] = []
        self._exc_sink: list[dict] = []

    # -- plumbing --------------------------------------------------------
    def install_excepthook(self) -> None:
        previous = sys.excepthook

        def hook(exc_type, exc, tb):
            self._exc_sink.append({
                'type': exc_type.__name__,
                'message': str(exc),
                'traceback': ''.join(traceback.format_exception(exc_type, exc, tb))[-2500:],
            })
            previous(exc_type, exc, tb)

        sys.excepthook = hook

    def drain_exceptions(self, context: str) -> list[dict]:
        out = list(self._exc_sink)
        for item in out:
            item['context'] = context
        self._exc_sink.clear()
        self.exceptions.extend(out)
        return out


def collect_buttons(page):
    """Every push button on a page, in visual order, with a readable label.

    The motion page builds one ``QGroupBox`` per axis, so the axis name is
    taken from the nearest enclosing group box -- without it the report would
    list four identical "点动 −" buttons with no way to tell them apart.
    """
    from PySide6.QtWidgets import QAbstractButton, QGroupBox, QPushButton, QToolButton

    found = []
    for widget in page.findChildren(QAbstractButton):
        if not isinstance(widget, (QPushButton, QToolButton)):
            continue
        if not widget.isVisible():
            continue
        text = (widget.text() or '').strip()
        if not text:
            continue
        axis = ''
        parent = widget.parent()
        while parent is not None:
            if isinstance(parent, QGroupBox):
                axis = parent.title()
                break
            parent = parent.parent()
        found.append((widget, f'[{axis}] {text}' if axis else text))
    return found


def select_first_rows(page) -> None:
    """Select the first row of every table on the page.

    Several studio buttons work on a *selected* row ("移动到选中候选",
    "实测验证当前位置").  Clicking them with an empty selection only proves
    that the guard message appears, so the harness picks a row first.
    """
    from PySide6.QtWidgets import QTableWidget

    for table in page.findChildren(QTableWidget):
        try:
            if table.rowCount() > 0:
                table.selectRow(0)
        except Exception:  # noqa: BLE001 - a table that refuses selection is fine
            continue


def read_positions(window) -> dict:
    """Current DPOS of every axis, or an empty dict when nothing is linked."""
    session = getattr(window, 'session', None)
    link = getattr(session, 'link', None) if session else None
    if link is None or not getattr(link, 'connected', False):
        return {}
    axes = list(session.settings.axes)
    out = {}
    for index, name in enumerate(axes):
        try:
            out[name] = round(float(link.get_dpos(index)), 4)
        except Exception:  # noqa: BLE001 - a failed read must not abort the sweep
            out[name] = None
    return out


def wait_quiet(app, page, window, timeout_s: float = 40.0) -> str:
    """Wait until no hardware operation is in flight.

    Three things have to settle before the next button is clicked:

    * the desktop shell's background pool (``tunerlab_app.bg``);
    * the hardware page's own motion task flag (``_motion_busy``) -- the motion
      page runs moves on its own ``_MotionTask`` and not through that pool, so
      waiting on the pool alone would click the next button while the axis is
      still travelling;
    * the controller itself, which must report every axis idle again.
    """
    import time as _time

    from tunerlab_app import bg

    deadline = _time.time() + timeout_s
    while _time.time() < deadline:
        app.processEvents()
        pending = bool(bg._pending)
        busy = bool(getattr(page, '_motion_busy', False))
        axes_busy = False
        session = getattr(window, 'session', None)
        link = getattr(session, 'link', None) if session else None
        if link is not None and getattr(link, 'connected', False):
            try:
                axes_busy = any(not link.if_idle(i)
                                for i in range(len(session.settings.axes)))
            except Exception:  # noqa: BLE001 - a failed poll is not a verdict
                axes_busy = False
        if not pending and not busy and not axes_busy:
            return 'quiet'
        _time.sleep(0.05)
    return 'timeout'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--with-motion', action='store_true',
                    help='also click the buttons that drive the axes')
    ap.add_argument('--settle', type=int, default=900,
                    help='milliseconds to wait after each click')
    ap.add_argument('--pages', default='', help='comma separated page keys')
    ap.add_argument('--load-db', default='',
                    help='calibration library (.h5) to load into the studio state')
    ap.add_argument('--init-studio', action='store_true',
                    help='initialise the studio with real motion and the real VNA')
    ap.add_argument('--home', action='store_true',
                    help='home every axis that is not homed (OP pulse, like the UI)')
    args = ap.parse_args()

    os_env = 'offscreen'
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', os_env)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication(sys.argv)

    sweep = Sweep(args.settle, args.with_motion)
    sweep.install_excepthook()

    # Any modal dialog (message box, file dialog, confirmation) would block the
    # sweep, so it is closed as soon as it appears — and recorded.
    from PySide6.QtWidgets import QDialog, QMessageBox

    def close_modals() -> None:
        w = QApplication.activeModalWidget()
        if w is None:
            return
        kind = type(w).__name__
        title = w.windowTitle()
        text = ''
        if isinstance(w, QMessageBox):
            text = w.text()
        sweep.dialogs.append({'kind': kind, 'title': title, 'text': text[:400]})
        try:
            w.reject()
        except Exception:  # noqa: BLE001
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass

    modal_timer = QTimer()
    modal_timer.timeout.connect(close_modals)
    modal_timer.start(200)

    from tuner.operation_log import operation_log
    from tunerlab_app.shell import MainWindow

    print('building MainWindow (real hardware mode) ...', flush=True)
    window = MainWindow(smoke_test=False)
    window.resize(1400, 900)
    window.show()
    app.processEvents()
    # The shell auto-connects 400 ms after start-up when the setting asks for it.
    deadline = time.time() + 8
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
        if window.session.connected:
            break
    app.processEvents()

    print(f'controller connected: {window.session.connected}', flush=True)
    print(f'pages built        : {len(window.pages)}', flush=True)

    if args.home:
        # The drive owns the homing cycle, so the controller's HOME_STATUS stays 0
        # for an OP-homed axis and a fresh session does not know it was homed.
        controller = window.session.require_controller()
        for axis in controller.axes:
            if axis in controller.homed_axes:
                continue
            try:
                controller.home_via_output(axis)
                print(f'homed {axis}', flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f'home {axis} failed: {type(exc).__name__}: {exc}', flush=True)
        print(f'homed axes: {sorted(controller.homed_axes)}', flush=True)

    if args.init_studio:
        # The studio keeps its own instrument slots, separate from the VNA
        # page's, so the wizard's "初始化" step is what puts a *real* VNA and
        # a *real* motion planner behind the calibration pages.
        from tuner.web import handlers
        handlers.studio_init_hardware(window.state, {
            'motion_source': 'real',
            'x_min': 0.0, 'x_max': 380.0,
            'z_min': 0.0, 'z_max': 10.0,
            'safe_z': 0.0,
            'vna_source': 'real',
            'vna_vendor': 'auto',
            'vna_ip': '192.168.2.111',
            'vna_port': 5025,
            'vna_start_hz': 5e9, 'vna_stop_hz': 5e9, 'vna_points': 1,
            'vna_timeout_ms': 15000,
        })
        studio = window.state.studio
        print(f'studio initialised: vna={studio.get("vna_source")} '
              f'planner={studio.get("planner") is not None} '
              f'homed={studio.get("homed_axes")}', flush=True)

    if args.load_db:
        from tuner.web import handlers
        handlers.studio_open_db(window.state, {'path': args.load_db})
        overview = handlers.studio_overview(window.state, {})
        print(f'calibration loaded : {args.load_db} '
              f'(valid {overview.get("valid_points", "?")}/'
              f'{overview.get("total_points", "?")})', flush=True)

    # Only these pages have a live-hardware dependency worth exercising.
    all_keys = list(window.pages.keys())
    wanted = [k for k in all_keys if args.pages.strip() == '' or k in args.pages.split(',')]

    results = []
    deferred = []          # link-dropping buttons, clicked at the very end

    def ensure_linked() -> str:
        """Make sure the controller link is up before a page is exercised."""
        if window.session.connected:
            return ''
        try:
            window.session.connect(window.session.settings.connection.ip, mode='real')
            return ''
        except Exception as exc:  # noqa: BLE001
            return f'{type(exc).__name__}: {exc}'

    for key in wanted:
        page = window.pages.get(key)
        if page is None:
            continue
        link_note = ensure_linked()
        try:
            window.go(key)
        except Exception as exc:  # noqa: BLE001
            results.append({'page': key, 'button': '<navigate>', 'result': 'FAIL',
                            'error': f'{type(exc).__name__}: {exc}'})
            continue
        app.processEvents()
        time.sleep(0.15)
        app.processEvents()

        buttons = collect_buttons(page)
        if not buttons:
            continue
        print(f'\n=== page {key}  ({len(buttons)} buttons) ==='
              + (f'  [link: {link_note}]' if link_note else ''), flush=True)

        for widget, text in buttons:
            is_motion = any(m in text for m in MOTION_MARKERS)
            if is_motion and not args.with_motion:
                results.append({'page': key, 'button': text, 'result': 'SKIP',
                                'note': '运动类按钮，未启用 --with-motion'})
                print(f'  [skip] {text}   (motion)', flush=True)
                continue

            if any(m in text for m in LINK_MARKERS):
                deferred.append((key, widget, text))
                results.append({'page': key, 'button': text, 'result': 'DEFER',
                                'note': '会断开链路，扫描结束后再测'})
                print(f'  [defer] {text}', flush=True)
                continue

            log_before = operation_log.count()
            status_before = window.statusBar().currentMessage() if hasattr(window, 'statusBar') else ''
            sweep.dialogs.clear()
            sweep._exc_sink.clear()
            pos_before = read_positions(window)

            try:
                widget.click()
            except Exception as exc:  # noqa: BLE001
                results.append({'page': key, 'button': text, 'result': 'FAIL',
                                'error': f'{type(exc).__name__}: {exc}'})
                print(f'  [FAIL] {text}: {exc}', flush=True)
                continue

            # Let the click settle: background tasks, timers, repaints.
            end = time.time() + args.settle / 1000.0
            while time.time() < end:
                app.processEvents()
                time.sleep(0.02)

            # Wait for the hardware to be idle again, so the next button is not
            # clicked while an axis is still travelling.
            settle_state = wait_quiet(app, page, window)
            app.processEvents()
            # Give the row-dependent buttons something to work on.
            select_first_rows(page)
            app.processEvents()

            exc_list = list(sweep._exc_sink)
            sweep.exceptions.extend(exc_list)
            sweep._exc_sink.clear()

            status_after = window.statusBar().currentMessage() if hasattr(window, 'statusBar') else ''
            new_logs = []
            if operation_log.count() > log_before:
                recent = operation_log.recent(limit=40)
                new_logs = [e if isinstance(e, dict) else e.to_dict() for e in recent][-6:]

            dialog_txt = '; '.join(
                f'{d["kind"]}:{d["title"]}/{d["text"]}' for d in sweep.dialogs)[:300]

            record = {
                'page': key,
                'button': text,
                'status_before': status_before,
                'status_after': status_after,
                'dialogs': dialog_txt,
                'exceptions': [f'{e["type"]}: {e["message"]}' for e in exc_list],
                'logs': [f'{e.get("level")} {e.get("category")}/{e.get("action")}: {e.get("detail")}'
                         for e in new_logs],
                'settle': settle_state,
                'positions_before': pos_before,
                'positions_after': read_positions(window),
            }
            if exc_list:
                record['result'] = 'ERROR'
            elif dialog_txt and ('操作失败' in dialog_txt or '失败' in dialog_txt
                                 or '错误' in dialog_txt):
                record['result'] = 'DIALOG'
            else:
                record['result'] = 'OK'
            results.append(record)

            flag = {'OK': ' ok ', 'DIALOG': 'dlg ', 'ERROR': 'ERR '}[record['result']]
            print(f'  [{flag}] {text}', flush=True)
            if record['dialogs']:
                print(f'         dialog: {record["dialogs"]}', flush=True)
            if record['exceptions']:
                for e in record['exceptions']:
                    print(f'         EXC: {e}', flush=True)
            for line in record['logs']:
                print(f'         log : {line}', flush=True)
            before = record['positions_before']
            after = record['positions_after']
            if before and after and before != after:
                moved = ', '.join(
                    f'{name} {before.get(name)}→{after.get(name)}'
                    for name in after if before.get(name) != after.get(name))
                print(f'         move: {moved}', flush=True)
            if settle_state != 'quiet':
                print(f'         warn: settle={settle_state}', flush=True)

    # ---- deferred link-dropping buttons --------------------------------
    if deferred:
        print('\n=== deferred (link dropping) ===', flush=True)
        for key, widget, text in deferred:
            try:
                widget.click()
                end = time.time() + 2.0
                while time.time() < end:
                    app.processEvents()
                    time.sleep(0.02)
                results.append({'page': key, 'button': text, 'result': 'OK',
                                'note': '断开链路'})
                print(f'  [ ok ] {text}', flush=True)
            except Exception as exc:  # noqa: BLE001
                results.append({'page': key, 'button': text, 'result': 'FAIL',
                                'error': f'{type(exc).__name__}: {exc}'})
                print(f'  [FAIL] {text}: {exc}', flush=True)

    # ---- summary -------------------------------------------------------
    print('\n' + '=' * 70)
    print('SUMMARY')
    print('=' * 70)
    counts: dict[str, int] = {}
    for r in results:
        counts[r['result']] = counts.get(r['result'], 0) + 1
    for key, n in sorted(counts.items()):
        print(f'  {key:<8} {n}')
    print(f'  TOTAL    {len(results)}')

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / 'button_sweep.json'
    out.write_text(json.dumps({
        'controller_connected': bool(window.session.connected),
        'with_motion': args.with_motion,
        'results': results,
        'exceptions': sweep.exceptions,
        'dialogs': sweep.dialogs,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\nreport -> {out}')

    try:
        window.session.disconnect()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

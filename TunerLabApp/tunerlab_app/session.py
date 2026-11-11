"""Session wrapper: realistic motion timeout and a correct homing mode.

Two machine-specific corrections live here, both verified against the ZMC
documentation (``RTBasic.chm`` → ``DATUM``) and the machine itself:

1. **Motion timeout.**  ``HardwareSession`` builds a :class:`tuner.models.TunerConfig`
   whose ``motion_timeout`` keeps its 5 s default.  X is 400 mm at 10 mm/s, so any
   move longer than ~50 mm — and every homing cycle — exceeded it and was reported
   as failed while the axis was still travelling.

2. **Homing mode.**  ``default_axis_params`` ships ``datum_mode=1``, which the vendor
   documents as *"axis runs forward at CREEP speed until the Z signal appears"* — a
   direction search for an encoder index, not an origin-switch search.  On this tuner
   the probes therefore rose instead of descending, and the cycle ended on the positive
   hard limit without ever clearing ``DPOS``.  Mode **4** is the documented
   origin-switch search in the negative direction: run backwards at ``SPEED`` until the
   switch closes, creep forward off it, then set ``DPOS=0`` and correct ``MPOS``.

The X axis keeps its configured mode: it reports a successful homing and the operator
did not report a direction problem there.
"""
from __future__ import annotations

from tuner.operation_log import operation_log
from tuner.ui_hardware import HardwareSession
from tuner.zmotion import ZmotionController

from .link_lock import LockedLink
from .motion_adapter import MPOS_COUNTS_PER_MM, TunerMotionController

__all__ = ['TunerSession']

#: Wall-clock budget for one move, in seconds.
#:
#: 400 mm at 10 mm/s is 40 s; the margin covers acceleration and a slow axis.
DEFAULT_MOTION_TIMEOUT = 60.0

#: Vendor DATUM modes that search a Z/index signal instead of the origin switch.
_Z_SIGNAL_MODES = (1, 2, 5, 6)

#: Documented origin-switch search: negative direction, creep off, DPOS cleared.
_DATUM_MODE_NEGATIVE_SWITCH = 4

#: Axes whose homing mode is corrected automatically.
_CORRECTED_AXES = ('Z1', 'Z2', 'Z3')


class TunerSession(HardwareSession):
    """``HardwareSession`` with the timeouts and homing modes this machine needs."""

    motion_timeout = DEFAULT_MOTION_TIMEOUT

    def __init__(self, store=None, parent=None):
        super().__init__(store, parent)
        self._ensure_homing_modes()
        self._ensure_feedback_scale()

    def connect(self, ip: str | None = None, mode: str | None = None) -> None:
        super().connect(ip, mode)
        self._serialise_link()
        self._upgrade_controller()
        self._apply_timeout()

    def disconnect(self) -> None:
        """Close the controller link.

        Declared explicitly on purpose: ``QObject`` also has a ``disconnect``
        method, and Shiboken resolves that built-in ahead of an inherited Python
        implementation.  Without this override ``session.disconnect()`` raises
        ``TypeError: QObject.disconnect(): not enough arguments`` — the "断开"
        button would fail with a dialog instead of closing the link.
        """
        HardwareSession.disconnect(self)

    def set_axis_count(self, axis_count: int) -> None:
        # The base implementation rebuilds the motion backend for the new model,
        # which resets TunerConfig to its defaults.
        super().set_axis_count(axis_count)
        self._upgrade_controller()
        self._apply_timeout()

    def set_active_unit(self, key: str) -> None:
        """Select the tuner the console commands.

        The base implementation rebuilds the stock backend, which would lose
        the millimetre handling of this machine, so the adapter is put back in
        place afterwards.
        """
        super().set_active_unit(key)
        self._upgrade_controller()
        self._apply_timeout()

    def apply_scale_correction(self, axis: str, units: float) -> dict:
        """Write UNITS and keep ``feedback_scale`` at the encoder ratio.

        The base implementation resets ``feedback_scale`` to 1.0 because it assumes
        commanded unit == encoder unit.  On this machine the encoder reports 10 µm
        counts, so that would disable the encoder check again.
        """
        result = super().apply_scale_correction(axis, units)
        params = self.settings.axes.get(axis)
        if params is not None:
            params.feedback_scale = MPOS_COUNTS_PER_MM
            self.store.save(self.settings)
        return result

    # -- internals -------------------------------------------------------
    def _serialise_link(self) -> None:
        """Route every DLL call through one lock.

        The status timer runs on the GUI thread while a move runs on a worker
        thread, and *Stop* deliberately runs on the GUI thread so it can preempt
        a move.  A ``zauxdll`` handle is one UDP session with internal state;
        overlapping calls are not documented as safe and a mismatch inside the DLL
        kills the process with no traceback.
        """
        link = self.link
        if not isinstance(link, ZmotionController) or isinstance(link, LockedLink):
            return
        proxy = LockedLink(link)
        self.link = proxy
        controller = self.controller
        if controller is not None and getattr(controller, 'link', None) is link:
            controller.link = proxy

    def _apply_timeout(self) -> None:
        if self.controller is None:
            return
        self.controller.config.motion_timeout = float(self.motion_timeout)

    def _upgrade_controller(self) -> None:
        """Replace the stock adapter with the millimetre-aware one.

        The stock adapter compares a 10 µm ``MPOS`` delta with a millimetre
        ``DPOS`` delta, so it rejects every real move.  The replacement keeps the
        same contract and the same link; only the unit handling and the homing
        path differ.
        """
        controller = self.controller
        if controller is None or getattr(controller, 'simulated', True):
            return
        if isinstance(controller, TunerMotionController):
            return
        upgraded = TunerMotionController(
            controller.config, self.link, self.settings.axes,
            axis_count=self.settings.axis_count,
            axis_base=getattr(controller, 'axis_base', 0),
            op_base=getattr(controller, 'op_base', 0),
            datum_in_base=getattr(controller, 'datum_in_base', -1))
        upgraded.initialize()
        self.controller = upgraded

    def _ensure_homing_modes(self) -> None:
        """Switch the probe axes from Z-signal search to origin-switch search.

        Only a mode that is clearly the Z-signal family is replaced, so an
        operator who deliberately configured another strategy keeps it.
        """
        changed: dict[str, tuple[int, int]] = {}
        for name in _CORRECTED_AXES:
            params = self.settings.axes.get(name)
            if params is None or params.datum_in < 0:
                continue
            if params.datum_mode in _Z_SIGNAL_MODES:
                changed[name] = (int(params.datum_mode), _DATUM_MODE_NEGATIVE_SWITCH)
                params.datum_mode = _DATUM_MODE_NEGATIVE_SWITCH
        if not changed:
            return
        try:
            self.store.save(self.settings)
        except Exception as exc:  # noqa: BLE001 - never block startup on a save error
            operation_log.exception('参数', '保存回零模式', exc)
            return
        detail = ', '.join(f'{name} {old}→{new}' for name, (old, new) in changed.items())
        operation_log.warn('参数', '修正回零模式', detail + '（找原点开关，反向）')

    def _ensure_feedback_scale(self) -> None:
        """Record that one encoder count is 10 µm, not one millimetre.

        ``feedback_scale`` is the encoder-units-per-commanded-unit ratio used by the
        motion adapter.  With ``MPOS`` in 10 µm and ``DPOS`` in mm it is 100; leaving
        it at 1.0 makes every move fail its encoder verification.
        """
        changed = []
        for name, params in self.settings.axes.items():
            if abs(float(params.feedback_scale) - MPOS_COUNTS_PER_MM) > 1e-6:
                changed.append(f'{name} {params.feedback_scale:g}→{MPOS_COUNTS_PER_MM:g}')
                params.feedback_scale = MPOS_COUNTS_PER_MM
        if not changed:
            return
        try:
            self.store.save(self.settings)
        except Exception as exc:  # noqa: BLE001 - never block startup on a save error
            operation_log.exception('参数', '保存反馈标度', exc)
            return
        operation_log.warn('参数', '修正反馈标度', ', '.join(changed) +
                           '（MPOS 单位 10 µm，DPOS 单位 mm）')

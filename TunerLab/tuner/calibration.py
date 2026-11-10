"""Resumable sequential calibration. Workers own hardware calls; pause at boundaries."""
import threading
import numpy as np
from .motion import MotionPlanner, MotionError, MotionTimeout


class CalibrationManager:
    def __init__(self,config,motion,vna,database,retries=2,motion_timeout_retries=3):
        if retries < 0: raise ValueError('retries must be nonnegative')
        if motion_timeout_retries < 0: raise ValueError('motion_timeout_retries must be nonnegative')
        self.config=config; self.motion=motion; self.vna=vna; self.database=database
        self.planner=MotionPlanner(config,motion)
        self.retries=retries; self.motion_timeout_retries=motion_timeout_retries
        self.state='idle'; self._pause=threading.Event(); self._cancel=threading.Event()
        self._running=threading.Lock()

    def prepare(self,positions,metadata=None):
        """Create the library.

        ``metadata`` carries P0 (schema v2) header extensions such as the point
        plan, frequency mode, interpolation mode and the prerequisite snapshot.
        It is optional so every pre-0.4.0 caller keeps working unchanged.
        """
        frequencies=getattr(self.vna,'frequencies',None)
        if frequencies is None: raise ValueError('VNA must expose configured frequencies before prepare')
        header={'source':'mock' if self.config.simulated else 'instrument'}
        if metadata: header.update(dict(metadata))
        self.database.create(self.config,frequencies,positions,header)
        self.state='ready'

    def pause(self): self._pause.set()
    def resume(self): self._pause.clear()
    def cancel(self): self._cancel.set()

    def _clear_motion_fault(self) -> None:
        """Drop the controller's software fault latch so a timed-out move can be re-issued.

        Only real hardware backends (``ZmotionMotionController`` / the desktop
        ``TunerMotionController``) raise a latched :class:`MotionTimeout`; they
        expose ``reset_fault``.  The deterministic mock controller has no latch
        and no ``reset_fault``, so the call is guarded and is a no-op there.
        The controller's homing state is re-adopted on the next move, so a
        transient timeout does not force a manual re-home.
        """
        reset = getattr(self.motion, 'reset_fault', None)
        if callable(reset):
            reset()

    def run(self,progress=None):
        if not self._running.acquire(blocking=False): raise RuntimeError('calibration already running')
        try:
            data=self.database.load()
            stored=data['metadata']['config']
            if stored['probes'] != self.config.probes or bool(data['metadata']['simulated']) != self.config.simulated:
                raise ValueError('configuration/provenance mismatch')
            from .models import comparable_config_dict
            import json
            if json.dumps(comparable_config_dict(stored),sort_keys=True) != json.dumps(comparable_config_dict(self.config),sort_keys=True):
                raise ValueError('configuration differs from calibration database')
            self.state='running'
            for index in self.database.pending_indices():
                if self._cancel.is_set(): self.state='cancelled'; break
                if self._pause.is_set(): self.state='paused'; break
                aborted = False
                # Allow the longer of the two retry budgets so neither kind of
                # transient failure is cut short by the loop bound.
                attempts_allowed = max(self.retries, self.motion_timeout_retries) + 1
                for attempt in range(attempts_allowed):
                    try:
                        self.planner.move_to(data['positions'][index])
                        measurement=self.vna.measure()
                        self.database.write_point(index,self.motion.read_position(),measurement)
                        break
                    except MotionTimeout as exc:
                        # A timeout is treated as transient: the controller was
                        # simply given too little time (a slow long traverse) or
                        # stalled momentarily, not made physically unsafe. Clear
                        # the software fault latch (set by the timeout) and retry
                        # up to ``motion_timeout_retries`` times. After that the
                        # point is recorded and the run aborts so the operator can
                        # recover the machine by hand.
                        self.database.record_failure(index,exc)
                        if attempt < self.motion_timeout_retries:
                            self._clear_motion_fault()
                            if self._cancel.is_set() or self._pause.is_set(): break
                            continue
                        aborted = True
                        break
                    except MotionError as exc:
                        # A non-timeout motion fault (limit trip, feedback
                        # mismatch, an unhomed axis or an emergency stop) is not
                        # transient: retrying it just re-issues the unsafe
                        # command. Record it and abort the task.
                        self.database.record_failure(index,exc)
                        aborted = True
                        break
                    except Exception as exc:
                        # Acquisition errors (VNA read, trigger, format) may be
                        # transient, so they are retried up to ``retries`` times.
                        self.database.record_failure(index,exc)
                        if attempt == self.retries: break
                        if self._cancel.is_set() or self._pause.is_set(): break
                if aborted:
                    self.state='failed'
                    break
                if progress: progress(index,len(data['positions']))
            else:
                self.state='completed' if not self.database.pending_indices() else 'completed_with_errors'
            return self.state
        except Exception:
            self.state='failed'
            raise
        finally:
            self._running.release()

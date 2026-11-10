from dataclasses import dataclass, field, asdict
import itertools
import numpy as np

@dataclass
class TunerConfig:
    probes: int = 2
    x_limits: tuple = (0., 10.)
    z_limits: tuple = (0., 1.)
    safe_z: float = 0.
    fixed_offsets: tuple = (0., 2.)
    z0: float = 50.
    motion_timeout: float = 5.
    simulated: bool = True
    #: Treat every axis as independent (default). When true, the shared X axis
    #: may be commanded whatever the probe positions are, the motion planner
    #: never retracts probes on X's behalf, and X homing does not wait for the
    #: probes. Single-axis guards (soft limits, homing, emergency stop, encoder
    #: verification) are unaffected. Set to False to restore the legacy
    #: retract-before-X interlock and the staged planner.
    independent_axes: bool = True
    def __post_init__(self):
        for limits in (self.x_limits, self.z_limits):
            if len(limits) != 2 or not np.isfinite(limits).all() or limits[0] > limits[1]:
                raise ValueError('limits must be finite ordered pairs')
        if not np.isfinite(self.fixed_offsets).all(): raise ValueError('offsets must be finite')
        if not np.isfinite(self.z0) or self.z0 <= 0: raise ValueError('Z0 must be positive finite')
        if not np.isfinite(self.motion_timeout) or self.motion_timeout <= 0: raise ValueError('timeout must be positive finite')
        if self.probes not in (2,3): raise ValueError('probes must be 2 or 3')
        if len(self.fixed_offsets) != self.probes: raise ValueError('one fixed offset per probe required')
        if not self.z_limits[0] <= self.safe_z <= self.z_limits[1]: raise ValueError('invalid safe Z')
    @property
    def axes(self): return ('X',) + tuple(f'Z{i+1}' for i in range(self.probes))
    def validate_position(self, position):
        p=np.asarray(position,dtype=float)
        if p.shape != (self.probes+1,) or not np.isfinite(p).all(): raise ValueError('invalid coordinate shape/value')
        for v, limits in zip(p, [self.x_limits]+[self.z_limits]*self.probes):
            if not limits[0] <= v <= limits[1]: raise ValueError('soft limit violation')
        return p
    def grid(self, x_values, z_values):
        return np.asarray([self.validate_position(p) for p in itertools.product(x_values,*([z_values]*self.probes))])


#: Runtime policies describe how *this build* moves, not the geometry of the
#: measured grid. They are kept out of stored calibration metadata so a
#: library stays readable by builds that predate them, and -- crucially -- a
#: library can be *resumed* with a different safety budget than the one it was
#: created with. ``motion_timeout`` is how long we wait for the axes to report
#: idle; raising it (e.g. 5 s -> 60 s after a bug fix) must not block a resume,
#: and the resumed run should use the new, larger budget.
RUNTIME_POLICY_FIELDS = ('independent_axes', 'motion_timeout')


def comparable_config_dict(value):
    """Config dict for storage/comparison, without runtime-only policies.

    Accepts a :class:`TunerConfig` or the plain dict from HDF5 metadata. Both
    sides must pass through this function before comparison: older libraries
    have no such field, while hand-built fixtures may include it.
    """
    data = asdict(value) if hasattr(value, '__dataclass_fields__') else dict(value)
    return {key: item for key, item in data.items()
            if key not in RUNTIME_POLICY_FIELDS}


@dataclass
class Measurement:
    frequencies: np.ndarray
    s: np.ndarray  # [frequency, output port, input port]; S21 = s[:,1,0]
    simulated: bool = True
    metadata: dict = field(default_factory=dict)

@dataclass
class SearchResult:
    index: int
    position: np.ndarray
    gamma: complex
    impedance: complex
    error: float
    reachable: bool
    predicted: bool = True

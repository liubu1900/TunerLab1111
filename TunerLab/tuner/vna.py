"""Full two-port acquisition with explicit simulated/physical provenance."""
from dataclasses import dataclass
from typing import Protocol, Mapping
import numpy as np
from .models import Measurement, TunerConfig
from . import vna_calibration

class VNAController(Protocol):
    def initialize(self): ...
    def measure(self) -> Measurement: ...
    def close(self): ...


def _frequencies(values):
    f = np.asarray(values, dtype=float)
    if f.ndim != 1 or len(f) == 0 or not np.isfinite(f).all() or np.any(f <= 0) or np.any(np.diff(f) <= 0):
        raise ValueError('frequencies must be positive, finite and strictly increasing')
    return f

class MockVNAController:
    """Passive reciprocal transmission line with independent shunt capacitors.

    Coordinates are interpreted as mm only for this illustrative synthetic model.
    Probe positions share X and have the configured fixed offsets. No real tuner
    response, bandwidth, loss, or achievable reflection is claimed.
    """
    simulated = True
    def __init__(self, config: TunerConfig, motion, frequencies):
        self.config, self.motion = config, motion
        self.frequencies = _frequencies(frequencies)
        self.initialized = False
    def initialize(self):
        self.initialized = True
        return 'MOCK VNA — synthetic passive network'
    def close(self):
        self.initialized = False
    def measure(self):
        if not self.initialized: raise RuntimeError('VNA not initialized')
        p = self.config.validate_position(self.motion.read_position())
        f = self.frequencies
        identity = np.broadcast_to(np.eye(2, dtype=complex), (len(f),2,2)).copy()
        abcd = identity
        # Reference-plane margins keep every simulated line segment nonnegative.
        offsets = np.asarray(self.config.fixed_offsets, float)
        order = np.argsort(offsets)
        absolute = p[0] - self.config.x_limits[0] + offsets - offsets.min() + 1.
        total = self.config.x_limits[1]-self.config.x_limits[0]+np.ptp(offsets)+2.
        previous = 0.
        zspan = self.config.z_limits[1]-self.config.z_limits[0]
        if zspan <= 0: raise ValueError('Mock requires nonzero Z range')
        def line(length):
            # Synthetic normalized ABCD: weak loss and air propagation.
            gl = (0.002 + 1j*2*np.pi*f/299792458.) * length * 1e-3
            a = np.empty_like(identity)
            a[:,0,0] = a[:,1,1] = np.cosh(gl)
            a[:,0,1] = a[:,1,0] = np.sinh(gl)
            return a
        for probe in order:
            abcd = abcd @ line(absolute[probe]-previous)
            insertion = (p[probe+1]-self.config.z_limits[0])/zspan
            shunt = identity.copy()
            shunt[:,1,0] = 1j * 3.0 * insertion**2 * f/10e9
            abcd = abcd @ shunt
            previous = absolute[probe]
        abcd = abcd @ line(total-previous)
        a,b,c,d = abcd[:,0,0],abcd[:,0,1],abcd[:,1,0],abcd[:,1,1]
        den = a+b+c+d
        s = np.empty_like(identity)
        s[:,0,0] = (a+b-c-d)/den
        s[:,1,1] = (-a+b-c+d)/den
        s[:,1,0] = 2/den
        s[:,0,1] = 2*(a*d-b*c)/den
        return Measurement(f.copy(), s, True, {'source':'mock','model':'passive-shunt-line-v1', 'position':p.tolist()})

@dataclass(frozen=True)
class VISAProfile:
    """All SCPI and transport settings must come from verified instrument setup.

    Trace queries must return ASCII real,imag pairs, in the active frequency order.
    Configure a calibrated full two-port channel and matching traces beforehand.
    """
    resource: str
    timeout_ms: int
    frequency_query: str
    trigger_commands: tuple[str, ...]
    completion_query: str
    trace_queries: Mapping[str, str]
    setup_commands: tuple[str, ...] = ()
    completion_expected: str = '1'
    read_termination: str = '\n'
    write_termination: str = '\n'

class PyVISAVNAController:
    simulated = False
    def __init__(self, profile: VISAProfile, *, enabled=False, resource_manager=None):
        self.profile, self.enabled = profile, enabled
        self._manager = resource_manager
        self._owns_manager = resource_manager is None
        self.instrument = None
    def initialize(self):
        if not self.enabled: raise PermissionError('Physical VNA I/O disabled; explicitly enable after verifying profile')
        p = self.profile
        if p.timeout_ms <= 0 or set(p.trace_queries) != {'S11','S21','S12','S22'}:
            raise ValueError('positive timeout and all four trace queries required')
        if not p.resource or not p.frequency_query or not p.completion_query:
            raise ValueError('resource, frequency and completion queries required')
        try:
            if self._manager is None:
                import pyvisa
                self._manager = pyvisa.ResourceManager()
            self.instrument = self._manager.open_resource(p.resource)
            self.instrument.timeout = p.timeout_ms
            self.instrument.read_termination = p.read_termination
            self.instrument.write_termination = p.write_termination
            for command in p.setup_commands: self.instrument.write(command)
        except Exception:
            self.close()
            raise
    def measure(self):
        if self.instrument is None: raise RuntimeError('VNA not initialized')
        p, inst = self.profile, self.instrument
        for command in p.trigger_commands: inst.write(command)
        if inst.query(p.completion_query).strip() != p.completion_expected:
            raise RuntimeError('VNA acquisition completion not confirmed')
        f = _frequencies(inst.query_ascii_values(p.frequency_query))
        s = np.empty((len(f),2,2), complex)
        for key, (out_port,in_port) in {'S11':(0,0),'S21':(1,0),'S12':(0,1),'S22':(1,1)}.items():
            raw = np.asarray(inst.query_ascii_values(p.trace_queries[key]), float)
            if raw.shape != (2*len(f),) or not np.isfinite(raw).all():
                raise ValueError(f'{key}: expected {2*len(f)} finite ASCII real/imag values')
            s[:,out_port,in_port] = raw[::2]+1j*raw[1::2]
        cal = vna_calibration.get_state()
        return Measurement(f, s, False, {
            'source':'physical_vna','resource':p.resource,
            'calibration_validated': bool(cal.verified),
            'calibration_method': cal.method,
            'calibration_timestamp': cal.timestamp,
            'calibration_valid': bool(vna_calibration.is_valid()),
        })
    def close(self):
        if self.instrument is not None:
            try: self.instrument.close()
            finally: self.instrument = None
        if self._owns_manager and self._manager is not None:
            try: self._manager.close()
            finally: self._manager = None

"""Synthetic data generation for the virtual mode.

The virtual controller answers motion commands, but a *calibration* is more
than positions: it is a table of S-parameters measured at every point.  This
module produces such a table from an analytic model of a tuner, so the Smith
chart, the impedance tuning and the validation pages have something real to
work on without a machine and without a VNA.

The model is deliberately simple and physically plausible:

* the probe depth sets the reflection magnitude (deeper probe → larger |Γ|),
* the X position sets the reflection phase (a full 360° over the travel), and
* the transmission stays close to unity while the reflection is small.

That is enough to exercise the whole chain: interpolation, the two-stage
search and the interpolation validation.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .calibration import CalibrationManager
from .database import CalibrationDatabase
from .models import Measurement, TunerConfig
from .motion import MotionController, MotionPlanner
from .vna import MockVNAController

__all__ = [
    'VirtualTunerModel', 'generate_calibration', 'DEFAULT_FREQUENCIES',
]

#: Frequency grid of the generated data (1–20 GHz, 41 points).
DEFAULT_FREQUENCIES = tuple(float(f) for f in np.linspace(1e9, 20e9, 41))


class VirtualTunerModel:
    """An analytic tuner response used to synthesise S-parameters.

    Parameters
    ----------
    gamma_min, gamma_max:
        Reflection magnitude at the fully retracted and fully inserted probe.
    phase_span:
        How much reflection phase the X axis sweeps over its whole travel.
    """

    def __init__(self, gamma_min: float = 0.05, gamma_max: float = 0.92,
                 phase_span: float = 360.0):
        self.gamma_min = float(gamma_min)
        self.gamma_max = float(gamma_max)
        self.phase_span = float(phase_span)

    def gamma(self, position, frequency: float = 0.0) -> complex:
        """Reflection coefficient at one position.

        The probe depths do more than set the magnitude: each probe carries its
        own phase contribution, so the probes together can reach any phase.
        That is what makes the reachable part of the Smith chart wide instead
        of a single ring.
        """
        point = np.asarray(position, dtype=float).ravel()
        x = float(point[0])
        probes = point[1:]
        if probes.size:
            # Each probe contributes a phase that grows with its insertion.
            weights = np.arange(1, probes.size + 1, dtype=float)
            weights = weights / weights.sum()
            phase_probe = float(np.sum(weights * probes))
            insertion = float(np.mean(probes))
        else:
            phase_probe = 0.0
            insertion = 0.0
        radius = self.gamma_min + (self.gamma_max - self.gamma_min) * insertion
        phase = np.deg2rad(self.phase_span * x + 180.0 * phase_probe)
        return complex(radius * np.cos(phase), radius * np.sin(phase))

    def s_parameters(self, position, frequencies) -> np.ndarray:
        """The four S-parameters at one position, shaped ``[freq, 2, 2]``."""
        freqs = np.asarray(frequencies, dtype=float)
        s = np.zeros((len(freqs), 2, 2), dtype=complex)
        for index, frequency in enumerate(freqs):
            gamma = self.gamma(position, frequency)
            # A little dispersion so the frequency axis is not degenerate.
            scale = 1.0 + 0.05 * np.sin(2 * np.pi * frequency / 20e9)
            s[index, 0, 0] = gamma * scale
            s[index, 1, 1] = gamma * scale
            # Transmission is high where the reflection is low.
            s[index, 1, 0] = 0.95 * (1.0 - 0.5 * abs(gamma))
            s[index, 0, 1] = 0.95 * (1.0 - 0.5 * abs(gamma))
        return s


def generate_calibration(path: str | Path,
                         config: TunerConfig,
                         x_count: int = 7,
                         z_count: int = 4,
                         frequencies=DEFAULT_FREQUENCIES,
                         model: VirtualTunerModel | None = None,
                         progress=None) -> dict:
    """Write a synthetic calibration database and return a summary.

    The database is produced through the *real* calibration manager with the
    Mock motion and Mock VNA backends, so the file has exactly the same
    structure and provenance flags as a measured one.
    """
    target = Path(path)
    if target.exists():
        raise FileExistsError(f'{target} already exists; choose another name')
    freqs = np.asarray(frequencies, dtype=float)
    model = model or VirtualTunerModel()

    motion = MotionController(config)
    motion.initialize()
    motion.home()
    planner = MotionPlanner(config, motion)
    vna = MockVNAController(config, motion, freqs)
    vna.initialize()

    positions = config.grid(np.linspace(*config.x_limits, x_count),
                            np.linspace(*config.z_limits, z_count))
    database = CalibrationDatabase(target)
    manager = CalibrationManager(config, planner, vna, database)
    manager.prepare(positions)

    # Write the synthesised S-parameters through the database, so the
    # provenance and the shape are exactly those of a measured file.
    for index, point in enumerate(positions):
        s = model.s_parameters(point, freqs)
        measurement = Measurement(freqs, s, simulated=True,
                                  metadata={'source': 'virtual-generator'})
        database.write_point(index, point, measurement)
        if progress is not None:
            progress(index + 1, len(positions))

    data = database.load()
    return {
        'path': str(target),
        'points': len(positions),
        'valid': int(np.count_nonzero(data['valid'])),
        'frequencies': [float(freqs[0]), float(freqs[-1])],
        'probes': config.probes,
    }

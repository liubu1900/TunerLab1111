"""Full-band complex repeatability measurements; never infer RF success from motion."""
from __future__ import annotations
import numpy as np


def vector_statistics(samples):
    """Complex mean-centred deviations across repetitions [repeat, frequency].

    RMS and maximum are linear complex-plane distances (not dB differences).
    Sample standard deviation uses n-1; one sample cannot establish repeatability.
    """
    a = np.asarray(samples, complex)
    if a.ndim != 2 or a.shape[0] < 2 or not np.isfinite(a).all():
        raise ValueError('at least two finite full-frequency repetitions required')
    mean = a.mean(axis=0)
    deviation = np.abs(a - mean)
    return {'mean_real': mean.real.tolist(), 'mean_imag': mean.imag.tolist(),
            'rms_vector': np.sqrt(np.mean(deviation**2,axis=0)).tolist(),
            'sample_std_vector': np.sqrt(np.sum(deviation**2,axis=0)/(len(a)-1)).tolist(),
            'max_vector': deviation.max(axis=0).tolist()}


def repeatability_report(motion, vna, position_a, position_b, repeats=3):
    """Measure A then B for every cycle, retaining complete S11/S21 spectra."""
    if repeats < 2: raise ValueError('repeats must be >= 2')
    measurements = {'A': [], 'B': []}
    freq = None
    provenance = None
    actual = {'A': [], 'B': []}
    for _ in range(repeats):
        for name, position in [('A',position_a),('B',position_b)]:
            motion.move_to(position)
            m = vna.measure()
            if freq is None:
                freq = np.asarray(m.frequencies)
                provenance = bool(m.simulated)
            if not np.array_equal(freq,m.frequencies) or provenance != bool(m.simulated):
                raise ValueError('measurement frequency/provenance changed')
            measurements[name].append(np.asarray(m.s,complex))
            actual[name].append(np.asarray(motion.read_position()).tolist())
    report = {'kind':'vector_repeatability','simulated':provenance,
              'rf_hardware_verified':False, 'real_measurements':not provenance, 'repeats':repeats,
              'frequencies_hz':freq.tolist(),'positions':{},
              'definition':'Complex deviations from mean per frequency; linear units.'}
    for name, values in measurements.items():
        a=np.asarray(values)
        report['positions'][name]={'actual':actual[name],
            'S11':vector_statistics(a[:,:,0,0]), 'S21':vector_statistics(a[:,:,1,0])}
    return report

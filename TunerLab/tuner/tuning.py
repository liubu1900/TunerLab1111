import numpy as np
from .rf import RFNetworkEngine as RF
from .motion import MotionPlanner
from .models import comparable_config_dict
from dataclasses import asdict
import json

class TuningManager:
    def __init__(self,config,motion,search_engine,vna=None):
        metadata=search_engine.data.get('metadata',{})
        stored=metadata.get('config')
        if not isinstance(stored,dict) or 'simulated' not in metadata:
            raise ValueError('tuning requires calibration configuration and provenance metadata')
        if bool(metadata['simulated']) != bool(config.simulated) or stored.get('simulated') != config.simulated:
            raise ValueError('active configuration and calibration provenance mismatch')
        active=asdict(config)
        # Timeout and the axis-independence policy are execution settings; all
        # geometric and RF fields must match. Older libraries do not carry the
        # policy field, so both sides are normalised before comparison.
        stored_geometry=comparable_config_dict(stored)
        for key in active:
            if key in ('motion_timeout','independent_axes'): continue
            if key not in stored_geometry or json.dumps(stored_geometry[key]) != json.dumps(active[key]):
                raise ValueError(f'active configuration differs from calibration: {key}')
        if hasattr(motion,'simulated') and bool(motion.simulated)!=bool(config.simulated):
            raise ValueError('motion controller provenance mismatch')
        if hasattr(motion,'config'):
            for key in ('probes','fixed_offsets','x_limits','z_limits','safe_z'):
                if json.dumps(getattr(motion.config,key)) != json.dumps(active[key]):
                    raise ValueError(f'motion controller configuration mismatch: {key}')
        self.config=config
        self.motion=motion if isinstance(motion,MotionPlanner) else MotionPlanner(config,motion)
        self.search=search_engine; self.vna=vna
    def tune(self,frequency,target_impedance,tolerance=.08,verify=False):
        target=RF.z_to_gamma(target_impedance,self.config.z0)
        result=self.search.search(frequency,target,k=1,tolerance=tolerance)[0]
        self.motion.move_to(result.position)
        actual=np.asarray(self.motion.read_position(),float)
        mechanically_positioned=bool(np.allclose(actual,result.position,rtol=0,atol=1e-6))
        report={'candidate':result,'target_gamma':target,'actual_position':actual.tolist(),'mechanically_positioned':mechanically_positioned,'predicted_gamma':result.gamma,'predicted_impedance':result.impedance,'predicted_error':result.error,'rf_verified':False,'simulated':self.config.simulated,'verification_kind':'not_measured'}
        if verify:
            if self.vna is None: raise ValueError('verification requires a VNA')
            measurement=self.vna.measure()
            if bool(measurement.simulated)!=bool(self.config.simulated): raise ValueError('measurement provenance mismatch')
            f=np.asarray(measurement.frequencies)
            if not f[0]<=frequency<=f[-1]: raise ValueError('verification frequency outside sweep')
            raw=np.array([[np.interp(frequency,f,measurement.s[:,i,j].real)+1j*np.interp(frequency,f,measurement.s[:,i,j].imag) for j in range(2)] for i in range(2)])
            fixture=self.search.fixture
            if fixture is not None and np.asarray(fixture).shape!=(2,2): fixture=self.search._at_frequency(np.asarray(fixture),frequency)
            measured=complex(RF.dut_gamma(raw,self.search.termination,fixture))
            error=float(abs(measured-target)); passed=bool(mechanically_positioned and error<=tolerance)
            report.update(measured_gamma=measured,measured_error=error,verification_passed=passed,rf_verified=bool(passed and not measurement.simulated),verification_kind='simulated_rf' if measurement.simulated else 'measured_rf')
        return report

"""Reproducible hardware-free calibration and impedance tuning acceptance flow."""
from __future__ import annotations
import argparse
from dataclasses import asdict, is_dataclass
from pathlib import Path
import json
import numpy as np
from .models import TunerConfig
from .motion import MockMotionController, MotionPlanner
from .vna import MockVNAController
from .database import CalibrationDatabase
from .calibration import CalibrationManager
from .search import GammaSearchEngine
from .tuning import TuningManager
from .validation import repeatability_report


def json_safe(value):
    if is_dataclass(value): return json_safe(asdict(value))
    if isinstance(value, dict): return {str(k):json_safe(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [json_safe(v) for v in value]
    if isinstance(value,np.ndarray): return json_safe(value.tolist())
    if isinstance(value,complex): return {'real':value.real,'imag':value.imag}
    if isinstance(value,np.generic): return json_safe(value.item())
    return value


def run_demo(output='examples', probes=2):
    out=Path(output); out.mkdir(parents=True,exist_ok=True)
    config=TunerConfig(probes=probes,fixed_offsets=tuple(2.*i for i in range(probes)))
    controller=MockMotionController(config)
    controller.initialize(); controller.home()
    planner=MotionPlanner(config,controller)
    freq=np.linspace(1e9,10e9,19)
    vna=MockVNAController(config,controller,freq); vna.initialize()
    positions=config.grid(np.linspace(0,10,5),np.linspace(0,1,3))
    path=out/f'mock_{probes}probe.h5'
    if path.exists():
        raise FileExistsError(f'{path} exists; choose a new output folder to preserve calibration versions')
    database=CalibrationDatabase(path)
    manager=CalibrationManager(config,planner,vna,database)
    manager.prepare(positions); manager.run()
    # A fresh database object ensures the search depends on persisted bytes.
    reloaded=CalibrationDatabase(path); data=reloaded.load()
    if not np.asarray(data['valid']).all():
        raise RuntimeError('Mock acceptance requires all positions valid; database retains pending points for diagnosis/resume')
    search=GammaSearchEngine(data,z0=config.z0)
    frequency=float(freq[len(freq)//2])
    index=len(positions)//2+1
    target_gamma=complex(data['s'][index,len(freq)//2,0,0])
    target_z=config.z0*(1+target_gamma)/(1-target_gamma)
    candidates=search.search(frequency,target_gamma,k=5,tolerance=1e-8)
    tuning=TuningManager(config,planner,search,vna)
    tuned=tuning.tune(frequency,target_z,tolerance=1e-8,verify=True)
    repeatability=repeatability_report(planner,vna,positions[1],positions[-1],repeats=3)
    reloaded.save_validation(repeatability)
    report=json_safe({'software_version':'0.1.0','simulated':True,
        'rf_hardware_verified':False,'probes':probes,'points':len(positions),
        'valid_points':int(np.asarray(data['valid']).sum()),'frequency_hz':frequency,
        'target_gamma':target_gamma,'target_impedance':target_z,
        'candidates':candidates,'tuning':tuned,'repeatability':repeatability})
    report_path=out/f'mock_{probes}probe_report.json'
    report_path.write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    vna.close()
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='examples/run')
    parser.add_argument('--probes',type=int,choices=(2,3),default=2)
    args=parser.parse_args()
    report=run_demo(args.output,args.probes)
    print(json.dumps({k:report[k] for k in ['simulated','rf_hardware_verified','probes','points','valid_points']},indent=2))

if __name__=='__main__': main()

import sys, json
sys.path.insert(0, r"D:/TunerLab_v0.1.0-20261104/TunerLab_v0.1.0-20261104/TunerLabApp")
sys.path.insert(0, r"D:/TunerLab_v0.1.0-20261104/TunerLab_v0.1.0-20261104/TunerLab")

from tuner.models import TunerConfig, comparable_config_dict, RUNTIME_POLICY_FIELDS
from tuner.zmotion_motion import build_tuner_config
from tuner.axis_params import AxisParams

print("RUNTIME_POLICY_FIELDS:", RUNTIME_POLICY_FIELDS)
assert 'motion_timeout' in RUNTIME_POLICY_FIELDS, "motion_timeout must be a runtime policy"

# 1) build_tuner_config default timeout must be 5.0 (the bug source)
cfg = build_tuner_config(
    {'X': AxisParams(rs_limit=0.0, fs_limit=380.0),
     'Z1': AxisParams(rs_limit=0.0, fs_limit=6.0),
     'Z2': AxisParams(rs_limit=0.0, fs_limit=6.0),
     'Z3': AxisParams(rs_limit=0.0, fs_limit=6.0)},
    x_limits=(0.0, 380.0), z_limits=(0.0, 6.0), safe_z=0.0, axis_count=4)
print("build_tuner_config motion_timeout:", cfg.motion_timeout)
assert cfg.motion_timeout == 5.0, "build_tuner_config keeps the 5s default"

# 2) the studio_bridge inheritance logic (mirrors handlers.py:719-721)
class FakeController:
    def __init__(self):
        self.config = TunerConfig(probes=3, x_limits=(0.0, 380.0),
                                  z_limits=(0.0, 6.0), simulated=False,
                                  fixed_offsets=(0.0, 2.0, 4.0),
                                  motion_timeout=60.0)
controller = FakeController()
controller_config = getattr(controller, 'config', None)
if controller_config is not None:
    cfg.motion_timeout = float(controller_config.motion_timeout)
print("after inheritance motion_timeout:", cfg.motion_timeout)
assert cfg.motion_timeout == 60.0, "controller budget must be inherited"

# 3) comparable_config_dict must exclude motion_timeout so resume is not blocked
stored = {'probes': 3, 'x_limits': [0.0, 380.0], 'z_limits': [0.0, 6.0],
          'safe_z': 0.0, 'fixed_offsets': [0.0, 0.0, 0.0], 'z0': 50.0,
          'motion_timeout': 5.0, 'simulated': False, 'independent_axes': True}
runtime = TunerConfig(probes=3, x_limits=(0.0, 380.0), z_limits=(0.0, 6.0),
                      simulated=False, fixed_offsets=(0.0, 0.0, 0.0),
                      motion_timeout=60.0)
a = comparable_config_dict(stored)
b = comparable_config_dict(runtime)
print("stored comparable:", a)
print("runtime comparable:", b)
assert 'motion_timeout' not in a and 'motion_timeout' not in b
assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True), \
    "stored(5s) and runtime(60s) configs must compare equal after the fix"
print("ALL CHECKS PASSED")

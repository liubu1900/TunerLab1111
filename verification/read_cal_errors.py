import h5py, os, json, sys

p = r"C:\Users\Eoulu\Documents\TunerLab\calibrations\cal_20261109_135314.h5"
print("exists:", os.path.exists(p))
if not os.path.exists(p):
    sys.exit(1)

with h5py.File(p, 'r') as f:
    print("datasets:", list(f.keys()))
    if 'errors' in f:
        errs = f['errors'][:]
        print("errors shape:", errs.shape)
        for i, e in enumerate(errs):
            es = e if isinstance(e, str) else str(e)
            if es.strip():
                print(f"  point[{i}] error: {es!r}")
    if 'valid' in f:
        v = f['valid'][:]
        print("valid count:", int(v.sum()), "/", len(v))
        print("failed indices:", [i for i, x in enumerate(v) if not bool(x)])
    if 'attempts' in f:
        print("attempts:", f['attempts'][:].tolist())
    if 'actual' in f and 'positions' in f:
        act = f['actual'][:]
        pos = f['positions'][:]
        print("positions[0]:", pos[0])
        print("actual[0]:", act[0])
    if 'metadata' in f.attrs:
        md = json.loads(f.attrs['metadata'])
        print("schema_version:", md.get('schema_version'))
        print("simulated:", md.get('simulated'))
        print("created:", md.get('created'))
        cfg = md.get('config', {})
        print("config keys:", list(cfg.keys()))
        for k in ('probes', 'x_limits', 'z_limits', 'safe_z', 'motion_timeout', 'feedback_scale'):
            if k in cfg:
                print(f"  config.{k}:", cfg[k])

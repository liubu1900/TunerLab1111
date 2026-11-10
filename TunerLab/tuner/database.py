"""Checkpointed HDF5 calibration store with SQLite task/version index."""
from .models import TunerConfig, comparable_config_dict
from .metadata import new_calibration_metadata, normalise_metadata
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
import h5py
import numpy as np


#: Names of the optional per-point provenance datasets (F-02).  These are
#: *extra* datasets: legacy libraries simply do not have them and are read with
#: safe defaults, while the main data structure (frequencies/positions/actual/
#: s/valid/errors/validation) is unchanged.
_POINT_RECORD_KEYS = ('probe_idx', 'loss_db', 'timestamp', 'attempts')


def _default_point_records(n):
    """Default (empty) per-point record arrays for ``n`` calibration points."""
    return {
        'probe_idx': np.full(n, -1, dtype='int32'),
        'loss_db': np.full(n, np.nan, dtype='f8'),
        'timestamp': np.full(n, np.nan, dtype='f8'),
        'attempts': np.zeros(n, dtype='int32'),
    }


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _derive_loss_db(s):
    """Best-effort insertion loss from |S21|; never raises, returns nan on doubt."""
    try:
        mag = np.abs(np.asarray(s, dtype=complex)[:, 1, 0])
        with np.errstate(divide='ignore', invalid='ignore'):
            db = -20.0 * np.log10(mag)
        value = float(np.nanmean(db))
        return value if np.isfinite(value) else np.nan
    except Exception:
        return np.nan


class CalibrationDatabase:
    def __init__(self, path):
        self.path = Path(path)
        self.sqlite_path = self.path.with_suffix('.sqlite')
        self.lock = threading.RLock()

    def _sql(self):
        con = sqlite3.connect(self.sqlite_path, timeout=30)
        con.execute('CREATE TABLE IF NOT EXISTS versions (id TEXT PRIMARY KEY, created REAL, path TEXT, metadata TEXT)')
        con.execute('CREATE TABLE IF NOT EXISTS tasks (version TEXT, point INTEGER, status TEXT, attempts INTEGER DEFAULT 0, error TEXT, updated REAL, PRIMARY KEY(version,point))')
        return con

    def create(self, config, frequencies, positions, metadata=None):
        f = np.asarray(frequencies, float)
        p = np.asarray(positions, float)
        if f.ndim != 1 or not len(f) or not np.isfinite(f).all() or np.any(f <= 0) or np.any(np.diff(f) <= 0):
            raise ValueError('frequencies must be finite, positive and strictly increasing')
        if p.ndim != 2 or not len(p):
            raise ValueError('nonempty position matrix required')
        p = np.asarray([config.validate_position(x) for x in p])
        # Runtime motion policy (``independent_axes``) is excluded: a library
        # written by this build stays readable by builds that predate the
        # switch, which would reject an unknown TunerConfig keyword.
        stored_config = comparable_config_dict(config)
        overrides = dict(metadata or {})
        # Core identity fields always come from the live config / fresh
        # generation; caller metadata contributes extension keys only.
        for key in ('config', 'simulated', 'version_id', 'created',
                    'cal_id', 'schema_version'):
            overrides.pop(key, None)
        meta = new_calibration_metadata(stored_config, bool(config.simulated), **overrides)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            # Exclusive creation prevents silently destroying calibration history.
            with h5py.File(self.path, 'x') as h:
                h.attrs['metadata'] = json.dumps(meta)
                h.create_dataset('frequencies', data=f)
                h.create_dataset('positions', data=p)
                h.create_dataset('actual', shape=p.shape, dtype='f8', fillvalue=np.nan)
                h.create_dataset('s', shape=(len(p),len(f),2,2), dtype='c16', fillvalue=complex(np.nan,np.nan), chunks=(1,len(f),2,2))
                h.create_dataset('valid', data=np.zeros(len(p), bool))
                h.create_dataset('errors', shape=(len(p),), dtype=h5py.string_dtype(), data=['']*len(p))
                for name, values in _default_point_records(len(p)).items():
                    h.create_dataset(name, data=values)
                h.create_group('validation')
                h.flush()
            with self._sql() as con:
                con.execute('INSERT INTO versions VALUES (?,?,?,?)', (meta['version_id'],meta['created'],str(self.path),json.dumps(meta)))
                con.executemany('INSERT INTO tasks VALUES (?,?,?,0,?,?)', [(meta['version_id'],i,'pending','',time.time()) for i in range(len(p))])
        return meta['version_id']

    def write_point(self, index, actual, measurement):
        with self.lock, h5py.File(self.path, 'r+') as h:
            meta=json.loads(h.attrs['metadata'])
            if bool(measurement.simulated) != meta['simulated']:
                raise ValueError('simulated/real provenance mismatch')
            if not np.array_equal(measurement.frequencies, h['frequencies'][:]):
                raise ValueError('frequency grid mismatch')
            s=np.asarray(measurement.s, complex); actual=np.asarray(actual,float)
            if s.shape != h['s'].shape[1:] or not np.isfinite(s).all(): raise ValueError('invalid S2P')
            if actual.shape != h['actual'].shape[1:] or not np.isfinite(actual).all(): raise ValueError('invalid actual coordinates')
            TunerConfig(**meta['config']).validate_position(actual)
            if not 0 <= index < len(h['valid']): raise IndexError(index)
            # Valid flag is the commit marker; uncommitted rows can safely be repeated.
            h['valid'][index]=False; h.flush()
            h['actual'][index]=actual; h['s'][index]=s; h['errors'][index]=''; h.flush()
            # Optional per-point provenance (F-02).  Read from whichever dict
            # the measurement exposes ('info' or the dataclass 'metadata');
            # missing/bad values degrade to safe defaults and never raise.
            info = getattr(measurement, 'info', None)
            if not isinstance(info, dict):
                info = getattr(measurement, 'metadata', None)
            if not isinstance(info, dict):
                info = {}
            for name, values in _default_point_records(len(h['valid'])).items():
                if name not in h:
                    h.create_dataset(name, data=values)
            loss = _as_float(info.get('loss_db'), np.nan)
            if not np.isfinite(loss):
                loss = _derive_loss_db(s)
            h['probe_idx'][index] = _as_int(info.get('probe_idx'), -1)
            h['loss_db'][index] = loss
            h['timestamp'][index] = time.time()
            h['attempts'][index] = _as_int(info.get('attempts'), 0)
            h.flush()
            h['valid'][index]=True; h.flush()
            with self._sql() as con:
                con.execute('UPDATE tasks SET status=?,attempts=attempts+1,error=?,updated=? WHERE version=? AND point=?',('complete','',time.time(),meta['version_id'],index))

    def append_positions(self, positions, metadata=None):
        """Append new calibration points to an existing database.

        Used by the Smith-chart refinement pass: the first sweep provides the
        measured model, and the refinement proposes the points that even out the
        coverage.  Those points join the *same* calibration so the run continues
        where it stopped instead of starting a second database.

        The file is rewritten atomically (write to a temporary file, then
        replace), so an interrupted append cannot destroy the existing history.
        Returns the indices of the appended points.
        """
        data = self.load()
        records = self.read_point_records()
        config = TunerConfig(**data['metadata']['config'])
        existing = np.asarray(data['positions'], float)
        new = np.asarray([config.validate_position(p) for p in positions], float)
        if new.ndim != 2 or not len(new):
            raise ValueError('nonempty position matrix required')
        if len(existing) and len(new) and new.shape[1] != existing.shape[1]:
            raise ValueError('appended positions must have the same number of axes')
        total = len(existing) + len(new)
        frequencies = np.asarray(data['frequencies'], float)
        defaults_new = _default_point_records(len(new))
        combined_records = {
            name: np.concatenate([records[name], defaults_new[name]])
            for name in _POINT_RECORD_KEYS
        }

        combined_positions = np.vstack([existing, new]) if len(existing) else new
        axes = combined_positions.shape[1]
        combined_actual = np.vstack([
            np.asarray(data['actual'], float), np.full((len(new), axes), np.nan)
        ])
        combined_s = np.concatenate([
            np.asarray(data['s'], complex),
            np.full((len(new), len(frequencies), 2, 2), complex(np.nan, np.nan)),
        ])
        combined_valid = np.concatenate([
            np.asarray(data['valid'], bool), np.zeros(len(new), bool)
        ])
        combined_errors = np.array(
            [_decode_text(value) for value in data.get('errors', [])] + [''] * len(new),
            dtype=object)

        meta = normalise_metadata(data['metadata'])
        meta.update(metadata or {})
        meta.setdefault('version_id', str(uuid.uuid4()))
        meta['appended'] = int(meta.get('appended', 0)) + len(new)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + '.append.tmp')
        with self.lock:
            with h5py.File(temporary, 'w') as h:
                h.attrs['metadata'] = json.dumps(meta)
                h.create_dataset('frequencies', data=frequencies)
                h.create_dataset('positions', data=combined_positions)
                h.create_dataset('actual', data=combined_actual)
                h.create_dataset('s', data=combined_s, chunks=(1, len(frequencies), 2, 2))
                h.create_dataset('valid', data=combined_valid)
                h.create_dataset('errors',
                                 data=np.array([str(v) for v in combined_errors],
                                               dtype=h5py.string_dtype()))
                for name, values in combined_records.items():
                    h.create_dataset(name, data=values)
                group = h.create_group('validation')
                for key, value in data.get('validation', {}).items():
                    group.attrs[str(key)] = json.dumps(
                        value, default=lambda x: x.tolist() if hasattr(x, 'tolist') else str(x))
                h.flush()
            os.replace(temporary, self.path)
            with self._sql() as con:
                first = len(existing)
                con.executemany(
                    'INSERT OR REPLACE INTO tasks VALUES (?,?,?,0,?,?)',
                    [(meta['version_id'], first + offset, 'pending', '', time.time())
                     for offset in range(len(new))])
        return list(range(len(existing), total))

    def load(self):
        with self.lock, h5py.File(self.path,'r') as h:
            out={key:h[key][:] for key in ['frequencies','positions','actual','s','valid']}
            out['errors']=h['errors'][:] if 'errors' in h else np.array([],dtype=object)
            out['metadata']=json.loads(h.attrs['metadata'])
            out['validation']={key:json.loads(value) for key,value in h['validation'].attrs.items()}
            return out

    def read_metadata(self):
        """Return the normalised (v2-shaped) library header without changing the file.

        Existing keys of a legacy library (notably ``schema_version`` == 1 and
        the original config/simulated/version_id) are preserved verbatim; only
        missing v2 fields are filled with safe defaults.
        """
        with self.lock, h5py.File(self.path, 'r') as h:
            raw = json.loads(h.attrs['metadata'])
        return normalise_metadata(raw)

    def update_metadata(self, **changes):
        """Write header fields back into the library.

        Used when a derived library refers to a *different* reference plane
        than its parent: the header has to say which plane the numbers are
        now referred to, otherwise the next reader would take them for the
        parent's plane.  Only the given fields change; everything else —
        including the lineage fields — is left alone.
        """
        with self.lock:
            with h5py.File(self.path, 'r+') as h:
                meta = normalise_metadata(json.loads(h.attrs['metadata']))
                meta.update(changes)
                h.attrs['metadata'] = json.dumps(meta)
                h.flush()
        return meta

    def read_point_records(self):
        """Return the optional per-point provenance arrays (F-02).

        A legacy library without these datasets degrades gracefully: default
        arrays whose length equals ``valid`` are returned instead of raising.
        The main ``load()`` dictionary is intentionally left untouched.
        """
        with self.lock, h5py.File(self.path, 'r') as h:
            n = len(h['valid']) if 'valid' in h else 0
            defaults = _default_point_records(n)
            out = {}
            for name in _POINT_RECORD_KEYS:
                out[name] = np.array(h[name][:]) if name in h else defaults[name]
            return out

    def pending_indices(self):
        with self.lock, h5py.File(self.path,'r') as h: return np.flatnonzero(~h['valid'][:]).tolist()

    def progress_counts(self):
        """Return ``(completed, total)`` from the committed ``valid`` flags.

        This is the authoritative progress source for the UI: unlike the
        ``progress`` callback (which reports the *index* of the point just
        written, and is therefore off by one and wrong on a resumed run), it
        counts every successfully committed point.  Only the small boolean
        dataset is read, so polling it a few times a second is cheap.
        Returns ``(0, 0)`` for a library that does not exist yet.
        """
        if not self.path.exists():
            return 0, 0
        with self.lock, h5py.File(self.path, 'r') as h:
            if 'valid' not in h:
                return 0, 0
            flags = np.asarray(h['valid'][:], dtype=bool)
        return int(np.count_nonzero(flags)), int(len(flags))

    def live_smith_snapshot(self, frequency_hz=None):
        """Lightweight Γ snapshot for the acquisition page's live Smith chart.

        Reads one frequency slice (an ``N×2×2`` block) plus the small flag and
        frequency arrays — never the whole ``s`` cube — so polling it every
        fraction of a second during a multi-hour sweep stays cheap.

        Returns ``(indices, gammas, used_frequency_hz, frequencies)``.
        ``gammas[i]`` is the DUT-plane Γ of committed point ``indices[i]``,
        using the same convention as
        :meth:`tuner.search.GammaSearchEngine.gammas`.
        """
        from .rf import RFNetworkEngine as RF
        with self.lock, h5py.File(self.path, 'r') as h:
            valid = np.asarray(h['valid'][:], dtype=bool)
            freqs = np.asarray(h['frequencies'][:], dtype=float)
            nfreq = int(h['s'].shape[1])
            if frequency_hz is None or not np.isfinite(frequency_hz):
                index = 0
            else:
                index = int(np.clip(np.searchsorted(freqs, float(frequency_hz)), 0, nfreq - 1))
            s = np.asarray(h['s'][:, index, :, :], dtype=complex)
            used = float(freqs[index]) if len(freqs) else float(frequency_hz or 0.0)
        indices = np.flatnonzero(valid)
        if not len(indices):
            return indices, np.array([], dtype=complex), used, freqs
        with np.errstate(divide='ignore', invalid='ignore'):
            g = np.asarray(RF.dut_gamma(s[indices], 0j, None), dtype=complex)
        g = np.where(np.isfinite(g), g, np.nan + 0j)
        return indices, g, used, freqs

    def record_failure(self,index,error):
        with self.lock, h5py.File(self.path,'r+') as h:
            h['valid'][index]=False; h['errors'][index]=str(error); h.flush()
            version=json.loads(h.attrs['metadata'])['version_id']
            with self._sql() as con:
                con.execute('UPDATE tasks SET status=?,attempts=attempts+1,error=?,updated=? WHERE version=? AND point=?',('failed',str(error),time.time(),version,index))

    def save_validation(self,data):
        with self.lock,h5py.File(self.path,'r+') as h:
            h['validation'].attrs[str(uuid.uuid4())]=json.dumps(data,default=lambda x: x.tolist() if hasattr(x,'tolist') else str(x))
            h.flush()

    def task_status(self):
        with self._sql() as con: return con.execute('SELECT point,status,attempts,error FROM tasks ORDER BY point').fetchall()

    def fork_version(self, new_path, reason):
        """Create an independent version, preserving the source and lineage."""
        data=self.load()
        target=CalibrationDatabase(new_path)
        source_meta=normalise_metadata(data['metadata'])
        exclude={'version_id','created','schema_version','config','simulated','cal_id'}
        metadata={k:v for k,v in source_meta.items() if k not in exclude}
        metadata.update(parent_version=data['metadata']['version_id'],version_reason=str(reason))
        target.create(TunerConfig(**data['metadata']['config']),data['frequencies'],data['positions'],metadata)
        from .models import Measurement
        for index in np.flatnonzero(data['valid']):
            target.write_point(int(index),data['actual'][index],Measurement(data['frequencies'],data['s'][index],data['metadata']['simulated']))
        for value in data['validation'].values(): target.save_validation(value)
        return target


def _decode_text(value) -> str:
    """Decode an HDF5 string entry, which h5py returns as bytes."""
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return str(value)

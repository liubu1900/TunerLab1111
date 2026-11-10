"""Calibration library header (schema v2) helpers.

Pure standard library, no I/O side effects, no UI / h5py / numpy dependency.
The helpers here only *build* metadata dictionaries; they never open or mutate
an HDF5 file.  ``normalise_metadata`` deliberately preserves every existing
key (in particular ``schema_version`` / ``config`` / ``simulated`` /
``version_id``) so a legacy v1 library is never rewritten to v2 on read.
"""
from __future__ import annotations

import copy
import hashlib
import time
import uuid
from pathlib import Path

#: Version of the *library header* produced by :func:`new_calibration_metadata`.
#: Legacy libraries keep their own ``schema_version`` (1) untouched.
SCHEMA_VERSION = 2

#: The 14 canonical header fields of a schema-v2 library.
METADATA_FIELDS = (
    'cal_id',
    'reference_plane',
    'cal_type',
    'vna_cal',
    'deembed_s2p',
    'fixture',
    'termination',
    'cal_method',
    'density',
    'frequency_list',
    'harmonics',
    'created',
    'sha256',
    'schema_version',
)

#: Safe, JSON-serialisable defaults for every field in :data:`METADATA_FIELDS`.
#: ``schema_version`` defaults to the legacy value so that a header missing the
#: key is never silently promoted to v2.
DEFAULT_METADATA = {
    'cal_id': '',
    'reference_plane': '',
    'cal_type': '',
    'vna_cal': {},
    'deembed_s2p': '',
    'fixture': '',
    'termination': '',
    'cal_method': '',
    'density': '',
    'frequency_list': [],
    'harmonics': [],
    'created': 0.0,
    'sha256': '',
    'schema_version': 1,
}


def normalise_metadata(meta: dict) -> dict:
    """Return a copy of ``meta`` with missing v2 fields filled in.

    Existing keys are preserved verbatim; only fields absent from ``meta`` are
    added from :data:`DEFAULT_METADATA`.  Mutable defaults are deep-copied so
    different headers never share the same dict/list object.
    """
    result = copy.deepcopy(dict(meta or {}))
    for field in METADATA_FIELDS:
        if field not in result:
            result[field] = copy.deepcopy(DEFAULT_METADATA[field])
    return result


def _readable_cal_id(now: float) -> str:
    """Human-readable short id, e.g. ``tunerlab-20261108-105400-ab12``."""
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now))
    return f'tunerlab-{stamp}-{uuid.uuid4().hex[:4]}'


def new_calibration_metadata(config_dict: dict, simulated: bool, **overrides) -> dict:
    """Build a fresh schema-v2 library header.

    ``config_dict`` and ``simulated`` come from the tuner configuration and are
    always authoritative.  ``overrides`` may carry extension keys such as
    ``point_plan`` / ``prerequisite_snapshot`` / ``interpolation_mode`` /
    ``frequency_mode``; they are merged on top of the generated defaults.
    """
    meta = normalise_metadata({})
    now = time.time()
    meta.update({
        'version_id': str(uuid.uuid4()),
        'created': now,
        'cal_id': _readable_cal_id(now),
        'schema_version': SCHEMA_VERSION,
        'config': copy.deepcopy(dict(config_dict)),
        'simulated': bool(simulated),
    })
    meta.update(overrides)
    return meta


def compute_file_sha256(path) -> str:
    """Return the hex SHA-256 of a file, read in chunks.

    Returns an empty string when the file does not exist (or cannot be read),
    so callers can safely embed the digest into metadata without exceptions.
    """
    p = Path(path)
    if not p.is_file():
        return ''
    digest = hashlib.sha256()
    try:
        with p.open('rb') as handle:
            for chunk in iter(lambda: handle.read(65536), b''):
                digest.update(chunk)
    except OSError:
        return ''
    return digest.hexdigest()

"""Calibration audit trail (P2).

The audit page answers the question an engineer asks months later: *what
exactly did this machine measure, and can I prove it?*  It gathers three
independent sources:

* **the library headers** — every ``*.h5`` in the calibrations folder, with
  its 14-field schema-v2 header, the file's SHA-256 and its point counts;
* **the validation records** — the reports the library carries: the
  interpolation checks, the repeatability runs and their verdicts;
* **the operation log** — what the software actually did, with the counts per
  category and the faults it recorded.

Nothing here modifies anything: the audit is a read-only view over the files
that already exist, and it is the evidence behind an export.
"""
from __future__ import annotations

import json
from pathlib import Path

from .database import CalibrationDatabase
from .metadata import compute_file_sha256, normalise_metadata

__all__ = ['audit_libraries', 'audit_report', 'read_operation_log']


def audit_libraries(directory, frequency_hz=None) -> dict:
    """One audit row per calibration library in ``directory``.

    A library that cannot be opened is reported with its error instead of
    being skipped — a file that exists but cannot be read is itself a finding.
    """
    folder = Path(directory)
    rows = []
    errors = []
    if not folder.is_dir():
        return {'libraries': [], 'errors': [], 'directory': str(folder)}
    for path in sorted(folder.glob('*.h5')):
        try:
            database = CalibrationDatabase(path)
            meta = normalise_metadata(database.read_metadata())
            data = database.load()
            valid = int(sum(1 for flag in data['valid'] if flag))
            total = len(data['valid'])
            frequencies = [float(value) for value in data['frequencies']]
            validations = data.get('validation') or {}
            rows.append({
                'path': str(path),
                'name': path.name,
                'cal_id': str(meta.get('cal_id', '')),
                'created': float(meta.get('created', 0.0)),
                'schema_version': int(meta.get('schema_version', 1)),
                'simulated': bool(meta.get('simulated', False)),
                'cal_type': str(meta.get('cal_type', '')),
                'cal_method': str(meta.get('cal_method', '')),
                'reference_plane': str(meta.get('reference_plane', '')),
                'density': str(meta.get('density', '')),
                'frequency_list_hz': frequencies,
                'frequency_ghz': [value / 1e9 for value in frequencies],
                'parent_version': str(meta.get('parent_version', '')),
                'version_reason': str(meta.get('version_reason', '')),
                'operation': str(meta.get('operation', '')),
                'points_valid': valid,
                'points_total': total,
                'points_percent': (100.0 * valid / total) if total else 0.0,
                'sha256': str(meta.get('sha256', '')),
                'file_sha256': compute_file_sha256(path),
                'validation_count': len(validations),
                'validation_kinds': sorted({
                    str(record.get('kind', ''))
                    for record in validations.values()
                    if isinstance(record, dict)}),
                'validations': [
                    dict(record) for record in validations.values()
                    if isinstance(record, dict)],
                'size_bytes': path.stat().st_size,
            })
        except Exception as exc:  # noqa: BLE001 - one bad file must not hide the rest
            errors.append({'path': str(path), 'error': f'{type(exc).__name__}: {exc}'})
    return {'libraries': rows, 'errors': errors, 'directory': str(folder)}


def read_operation_log(path, limit=5000) -> dict:
    """Summarise the on-disk operation log.

    The log is a JSON-lines file; a malformed line is counted and skipped
    rather than aborting the whole audit.
    """
    target = Path(path)
    summary = {
        'path': str(target),
        'exists': target.is_file(),
        'entries': 0,
        'malformed': 0,
        'by_category': {},
        'by_level': {},
        'faults': [],
        'calibrations': [],
        'validations': [],
        'first': None,
        'last': None,
    }
    if not summary['exists']:
        return summary
    entries = []
    with target.open('r', encoding='utf-8', errors='replace') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                summary['malformed'] += 1
    summary['entries'] = len(entries)
    for entry in entries:
        category = str(entry.get('category', ''))
        level = str(entry.get('level', 'info'))
        summary['by_category'][category] = summary['by_category'].get(category, 0) + 1
        summary['by_level'][level] = summary['by_level'].get(level, 0) + 1
        action = str(entry.get('action', ''))
        if level in ('warn', 'error'):
            summary['faults'].append({
                'time_text': str(entry.get('time_text', '')),
                'category': category,
                'action': action,
                'detail': str(entry.get('detail', '')),
                'level': level,
            })
        if '建立校准库' in action or '建库' in action:
            summary['calibrations'].append({
                'time_text': str(entry.get('time_text', '')),
                'detail': str(entry.get('detail', '')),
            })
        if '验证' in action or '重复性' in action:
            summary['validations'].append({
                'time_text': str(entry.get('time_text', '')),
                'action': action,
                'detail': str(entry.get('detail', '')),
            })
    if entries:
        summary['first'] = str(entries[0].get('time_text', ''))
        summary['last'] = str(entries[-1].get('time_text', ''))
    # Only the most recent faults are interesting; the file can hold thousands.
    summary['faults'] = summary['faults'][-50:]
    summary['calibrations'] = summary['calibrations'][-50:]
    summary['validations'] = summary['validations'][-50:]
    return summary


def audit_report(directory, operation_log_path=None) -> dict:
    """The whole audit: the libraries, their validations and the operation log."""
    libraries = audit_libraries(directory)
    validations = []
    for row in libraries['libraries']:
        for record in row.get('validations', []):
            validations.append({
                'library': row['name'],
                'cal_id': row['cal_id'],
                'kind': str(record.get('kind', '')),
                'passed': record.get('passed', record.get('all_within_tolerance')),
                'within_tolerance': record.get('within_tolerance'),
                'n': record.get('n'),
                'mean_delta_gamma': record.get('mean_delta_gamma'),
                'max_delta_gamma': record.get('max_delta_gamma'),
                'reset_dB': record.get('reset_dB'),
                'message': str(record.get('message', '')),
            })
    log = read_operation_log(operation_log_path) if operation_log_path else {
        'exists': False, 'entries': 0, 'by_category': {}, 'by_level': {},
        'faults': [], 'calibrations': [], 'validations': [], 'malformed': 0,
        'path': '', 'first': None, 'last': None,
    }
    return {
        'libraries': libraries['libraries'],
        'library_errors': libraries['errors'],
        'directory': libraries['directory'],
        'validations': validations,
        'log': log,
        'library_count': len(libraries['libraries']),
        'validation_count': len(validations),
        'fault_count': len(log.get('faults', [])),
        'total_points': sum(row['points_valid'] for row in libraries['libraries']),
    }

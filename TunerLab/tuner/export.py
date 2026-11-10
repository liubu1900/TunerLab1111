"""Export a calibrated tuner library to standard text formats (P1-F13).

The export functions are pure file writers: they read a *loaded* library dict
(``CalibrationDatabase.load()``) plus its header (``read_metadata()``) and emit
one of:

* ``.tef`` — Tuner Characterization file, 14 columns, one row per point.
* ``.csv`` — the raw data table with derived |Γ| / VSWR columns.
* ``.s2p`` — Touchstone 2-port (RI) for one point (or every point of a library).
* an HDF5 byte-for-byte copy of the whole library.

Every writer returns a small JSON-serialisable summary (path / rows / points)
so the desktop console and the web API can report what was produced without
re-opening the file.  The export **never mutates** the source library.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np

from .rf import RFNetworkEngine as RF


def _axis_names(n_axes: int) -> list[str]:
    """Column labels for the mechanical axes (``X, Z1, Z2, ...``)."""
    return ['X'] + [f'Z{i}' for i in range(1, n_axes)]


def _tef_columns(n_axes: int) -> tuple[str, ...]:
    """Column order of the ``.tef`` file, axis-count aware.

    ``X, Z1, Z2, ...`` are the tuner mechanical axes (2 probes → 3 axes,
    3 probes → 4 axes); the four S-parameters follow as real/imaginary pairs.
    All S values are complex reflection/transmission at ``Freq`` (Hz).
    """
    pos = _axis_names(n_axes)
    return tuple(['Pt', 'Freq_Hz', *pos,
                  'S11_re', 'S11_im', 'S21_re', 'S21_im',
                  'S12_re', 'S12_im', 'S22_re', 'S22_im', 'loss_dB'])


def _csv_columns(n_axes: int) -> tuple[str, ...]:
    """Raw-table CSV columns (F-12), axis-count aware."""
    pos = _axis_names(n_axes)
    return tuple(['Pt', *pos,
                  'S11_mag', 'S11_deg', 'S21_mag',
                  'Gamma_mag', 'Gamma_deg', 'VSWR', 'ReturnLoss_dB', 'loss_dB'])


def _selected_indices(data, valid_only):
    valid = np.asarray(data['valid'], bool)
    if valid_only:
        return np.flatnonzero(valid).tolist()
    return list(range(len(valid)))


def _point_rows(data, metadata, valid_only, include_probe=True):
    """Yield (index, position[4], s[2,2], loss) tuples for the chosen points."""
    positions = np.asarray(data.get('actual', data['positions']), float)
    s = np.asarray(data['s'], complex)
    records = data.get('point_records') or {}
    loss = np.asarray(records.get('loss_db'), float) if 'loss_db' in records else None
    freqs = np.asarray(data['frequencies'], float)
    for i in _selected_indices(data, valid_only):
        pos = positions[i]
        sij = s[i, 0] if s.shape[1] == 1 else s[i, 0, :, :]
        if sij.shape != (2, 2):
            # single-frequency store keeps s as (n,1,2,2); take the slice
            sij = s[i, 0]
        point_loss = float(loss[i]) if loss is not None and np.isfinite(loss[i]) else float('nan')
        yield i, np.asarray(pos, float), np.asarray(sij, complex), point_loss


def export_tef(path, data, metadata=None, valid_only=True) -> dict:
    """Write the 14-column ``.tef`` file.

    One row per (chosen) calibration point: ``Pt Freq X Z1 Z2 Z3`` followed by the
    four S-parameters as real/imaginary pairs and the insertion loss (dB).  The
    file header documents the column order and units so any text tool can read it.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    freqs = np.asarray(data['frequencies'], float)
    cal_id = (metadata or {}).get('cal_id', '') if metadata else ''
    freq_hz = float(freqs[0]) if len(freqs) else float('nan')
    n_axes = int(np.asarray(data.get('actual', data['positions']), float).shape[1])
    columns = _tef_columns(n_axes)
    lines = [
        '! TunerLab Tuner Characterization file (.tef)',
        f'! Calibration ID : {cal_id}',
        f'! Frequencies (Hz): {", ".join(f"{f:.6f}" for f in freqs)}',
        f'! Z0 (ohm)       : 50',
        '! Columns (tab-separated): '
        + ' | '.join(columns)
        + '  -- S params are real,imag pairs; loss_dB is insertion loss |S21|',
    ]
    n = 0
    for i, pos, sij, point_loss in _point_rows(data, metadata, valid_only, True):
        row = [f'{i}', f'{freq_hz:.6f}']
        row += [f'{v:.6f}' for v in pos[:n_axes]]
        for a in (0, 1):
            for b in (0, 1):
                row.append(f'{sij[a, b].real:.8e}')
                row.append(f'{sij[a, b].imag:.8e}')
        row.append(f'{point_loss:.6f}' if np.isfinite(point_loss) else 'nan')
        lines.append('\t'.join(row))
        n += 1
    p.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return {'path': str(p), 'format': 'tef', 'rows': n,
            'columns': list(columns)}


def export_csv(path, data, metadata=None, valid_only=True) -> dict:
    """Write the raw data table (measured points) with derived |Γ|/VSWR columns.

    ``|Γ|`` is the load reflection coefficient referred to the DUT (termination
    0 Ω, no fixture) — the same definition the Smith chart uses.  VSWR and return
    loss are **derived** quantities and are labelled as such in the header.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    z0 = 50.0
    meta = metadata or {}
    config = (meta.get('config') or {})
    if isinstance(config, dict) and config.get('z0'):
        try:
            z0 = float(config['z0'])
        except (TypeError, ValueError):
            pass
    termination = complex(meta.get('termination') or 0)
    n_axes = int(np.asarray(data.get('actual', data['positions']), float).shape[1])
    columns = _csv_columns(n_axes)
    lines = [
        '! TunerLab calibration raw data export (.csv)',
        f'! Z0 (ohm): {z0}  termination: {termination}',
        '! VSWR and ReturnLoss are DERIVED from |Gamma|; the stored quantity '
        'is the four S-parameters.',
        ','.join(columns),
    ]
    n = 0
    for i, pos, sij, point_loss in _point_rows(data, metadata, valid_only, True):
        g = RF.dut_gamma(sij, termination)
        mag = float(abs(g))
        deg = float(np.degrees(np.angle(g)))
        if mag >= 1.0 - 1e-12:
            vswr = float('inf')
            rl = 0.0
        else:
            vswr = (1.0 + mag) / (1.0 - mag)
            rl = 0.0 if mag == 0 else float(-20.0 * np.log10(mag))
        s11_mag = float(abs(sij[0, 0]))
        s11_deg = float(np.degrees(np.angle(sij[0, 0])))
        s21_mag = float(abs(sij[1, 0]))
        row = [f'{i}', *(f'{v:.6f}' for v in pos[:n_axes]),
               f'{s11_mag:.8e}', f'{s11_deg:.6f}', f'{s21_mag:.8e}',
               f'{mag:.8e}', f'{deg:.6f}',
               ('inf' if not np.isfinite(vswr) else f'{vswr:.6f}'),
               f'{rl:.6f}',
               (f'{point_loss:.6f}' if np.isfinite(point_loss) else 'nan')]
        lines.append(','.join(row))
        n += 1
    p.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return {'path': str(p), 'format': 'csv', 'rows': n,
            'columns': list(columns)}


def export_s2p(path, data, point_index, freq_index=0) -> dict:
    """Write one Touchstone 2-port ``.s2p`` (RI) for a single calibration point.

    The S-parameters of ``point_index`` at ``freq_index`` are written as
    ``S11 S21 S12 S22`` real/imaginary pairs at the single library frequency.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    s = np.asarray(data['s'], complex)
    freqs = np.asarray(data['frequencies'], float)
    if not 0 <= point_index < s.shape[0]:
        raise IndexError(f'point index {point_index} out of range')
    if not 0 <= freq_index < freqs.size:
        raise IndexError(f'frequency index {freq_index} out of range')
    sij = s[point_index, freq_index]
    f_hz = float(freqs[freq_index])
    lines = [
        '# Hz S RI R 50',
        '! TunerLab point export (.s2p)',
        f'! Pt {point_index}  Freq {f_hz:.6f} Hz',
        f'{f_hz:.6f} '
        f'{sij[0, 0].real:.8e} {sij[0, 0].imag:.8e} '
        f'{sij[1, 0].real:.8e} {sij[1, 0].imag:.8e} '
        f'{sij[0, 1].real:.8e} {sij[0, 1].imag:.8e} '
        f'{sij[1, 1].real:.8e} {sij[1, 1].imag:.8e}',
    ]
    p.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return {'path': str(p), 'format': 's2p', 'point': int(point_index),
            'frequency_hz': f_hz}


def export_s2p_library(dir_out, data, freq_index=0, valid_only=True) -> dict:
    """Write one ``.s2p`` per chosen point into ``dir_out``.

    Returns the list of written file paths.  Useful for feeding a calibrated
    library into tuner control software or a third-party load-pull tool.
    """
    d = Path(dir_out)
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in _selected_indices(data, valid_only):
        out = d / f'point_{i:05d}.s2p'
        export_s2p(out, data, i, freq_index=freq_index)
        paths.append(str(out))
    return {'dir': str(d), 'format': 's2p_library', 'points': len(paths),
            'paths': paths}


def export_hdf5_copy(src, dst) -> dict:
    """Copy the whole HDF5 library verbatim (byte-for-byte)."""
    src_p = Path(src)
    dst_p = Path(dst)
    if not src_p.is_file():
        raise FileNotFoundError(src)
    dst_p.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_p, dst_p)
    return {'path': str(dst_p), 'format': 'hdf5', 'bytes': dst_p.stat().st_size}


def _json_safe(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)

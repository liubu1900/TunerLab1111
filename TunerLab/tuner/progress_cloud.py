"""Acquisition progress as a layered point cloud (P1-F11).

A tuner workbench shows, while a calibration is running, a *point cloud* of the acquisition
grid: every planned point, coloured by whether it has been measured yet.  The
operator sees the shape of the grid filling in rather than a bare percentage,
which is what makes "the run is stuck on one corner" obvious.

The grid layout comes from :meth:`tuner.models.TunerConfig.grid`, which builds
``itertools.product(x_values, z_values * probes)`` — so a layer is one distinct
X value and the rows inside it are the Z1/Z2/Z3 combinations.  The grouping is
derived from the stored positions, never assumed, so a library written with a
different probe count still renders correctly.

A point counts as done only when it has been *written* to the library, which is
exactly the guarantee the acquisition loop provides: a point that was measured
but not committed is still pending.
"""
from __future__ import annotations

import numpy as np

__all__ = ['layer_of_point', 'progress_cloud']


def _positions(data) -> np.ndarray:
    """The planned acquisition grid.

    ``positions`` is the command grid the library was created with and is always
    complete; ``actual`` is filled in point by point as the machine reaches each
    position and is still all-NaN for the points that were never written.  The
    cloud describes the *plan*, so the plan is the source — reading ``actual``
    here would make every not-yet-measured point land in its own NaN layer.
    """
    array = np.asarray(data.get('positions'), float)
    if array.ndim == 1:
        array = array.reshape(1, -1)
    return array


def layer_of_point(data) -> list[dict]:
    """Group the planned points into layers.

    Each layer is one distinct X coordinate; the rows inside it are the
    (Z1, Z2, Z3) combinations that belong to that X.  Rows are sorted so the
    same combination lands on the same visual row in every layer, which is what
    makes the cloud readable as a grid.
    """
    positions = _positions(data)
    valid = np.asarray(data.get('valid', []), bool)
    if len(valid) != len(positions):
        valid = np.zeros(len(positions), bool)
    if positions.ndim != 2 or not len(positions):
        return []

    # Round to 1 µm: two commands that differ only by float noise belong to the
    # same layer, and the operator's mental model is "that X row".
    xs = np.round(positions[:, 0], 6)
    combos = [tuple(np.round(row, 6)) for row in positions[:, 1:]]

    layers: list[dict] = []
    for x in sorted(set(xs.tolist())):
        indices = [int(i) for i in np.flatnonzero(xs == x)]
        # The row order is the sorted Z combination, so a row index means the
        # same thing in every layer.
        ordered = sorted(indices, key=lambda i: combos[i])
        rows = [combos[i] for i in ordered]
        done_rows = [bool(valid[i]) for i in ordered]
        done = sum(1 for value in done_rows if value)
        layers.append({
            'x': float(x),
            'rows': [[float(v) for v in combo] for combo in rows],
            'indices': ordered,
            'done_rows': done_rows,
            'total': len(ordered),
            'done': done,
            'pending': len(ordered) - done,
            'complete': done == len(ordered),
        })
    return layers


def progress_cloud(data, errors=None) -> dict:
    """The point-cloud view of an acquisition.

    ``data`` is the dictionary :meth:`tuner.database.CalibrationDatabase.load`
    returns; ``errors`` is the optional per-point error array.  The result
    carries the overall counters, the layers and the failed points so the UI
    can render both the cloud and the failure list.
    """
    positions = _positions(data)
    valid = np.asarray(data.get('valid', []), bool)
    if len(valid) != len(positions):
        valid = np.zeros(len(positions), bool)
    layers = layer_of_point(data)

    failed = []
    if errors is not None:
        errors = np.asarray(errors, object)
        for index in np.flatnonzero(~valid):
            if index < len(errors):
                message = errors[index]
                if isinstance(message, bytes):
                    message = message.decode('utf-8', errors='replace')
                message = str(message or '')
                if message:
                    failed.append({'index': int(index), 'error': message})

    total = int(len(valid))
    done = int(np.count_nonzero(valid))
    return {
        'total': total,
        'done': done,
        'pending': total - done,
        'percent': (100.0 * done / total) if total else 0.0,
        'layers': layers,
        'layer_count': len(layers),
        'complete_layers': sum(1 for layer in layers if layer['complete']),
        'failed': failed,
        'axes': (positions.shape[1] if positions.ndim == 2 else 0),
    }

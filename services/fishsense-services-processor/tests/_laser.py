"""Test helpers for the laser stages: labels as the contract carries them.

v1's tests built `fishsense_api_sdk` `LaserLabel`s with an integer `id` and
`image_id`; v2's judgement reads `LaserLabelRow`s, whose `number` and
`capture_number` are those two ids for a migrated row. `label()` keeps v1's
call shape so the ported tests read as v1's did.
"""

from __future__ import annotations

import uuid

import numpy as np

from fishsense_services_contracts.laser import LaserLabelRow

SLOPE, INTERCEPT = 0.4, 100.0
NORMAL = np.array([-SLOPE, 1.0]) / np.hypot(SLOPE, 1.0)
DIVE = uuid.UUID("00000000-0000-0000-0000-00000000d1e0")


def label_uuid(number: int) -> uuid.UUID:
    """A stable uuid per label number, so a test can name either."""
    return uuid.uuid5(uuid.NAMESPACE_OID, f"laser-label-{number}")


def label(number, capture_number, xy, *, superseded=False, completed=True):
    return LaserLabelRow(
        label_id=label_uuid(number),
        number=number,
        capture_number=capture_number,
        x=None if xy is None else float(xy[0]),
        y=None if xy is None else float(xy[1]),
        superseded=superseded,
        completed=completed,
    )


def prod_like_dive(n=80, offsets=None, superseded=(), seed=0, sigma=1.85):
    """v1's `_dive`: n positives on one line with prod-like perpendicular
    noise (~1.85 px), plus per-index offsets along the normal."""
    rng = np.random.default_rng(seed)
    xs = np.linspace(50.0, 1500.0, n)
    perp = rng.normal(0.0, sigma, size=n)
    for i, off in (offsets or {}).items():
        perp[i] += off
    pts = np.column_stack([xs, SLOPE * xs + INTERCEPT]) + perp[:, None] * NORMAL
    return [
        label(i + 1, 1000 + i, p, superseded=i in superseded) for i, p in enumerate(pts)
    ]


def colinear_labels(n, *, capture_start=1000):
    """v1's `_colinear_labels`: y = 0.4x + 100 with 1 px Gaussian noise."""
    rng = np.random.default_rng(0)
    xs = np.linspace(50.0, 1500.0, n)
    ys = 0.4 * xs + 100.0 + rng.normal(0.0, 1.0, size=n)
    return [
        label(i + 1, capture_start + i, (x, y)) for i, (x, y) in enumerate(zip(xs, ys))
    ]


def reflection_dive(*, superseded_secondary=False):
    """Prod dive 77: a laser line and its specular reflection 45 px away."""
    labels = []
    for i in range(30):
        t = i * 30.0
        labels.append(label(i + 1, 100 + i, (1945.0 + 0.3 * t, 800.0 + t)))
    for i in range(20):
        t = i * 45.0
        labels.append(
            label(
                60 + i,
                200 + i,
                (1900.0 + 0.3 * t, 800.0 + t),
                superseded=superseded_secondary,
            )
        )
    return labels

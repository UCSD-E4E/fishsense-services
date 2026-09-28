"""Running the laser validator again must not supersede anything new.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_laser_validator_does_not_erode.py (#927). Names,
fixtures and reasons are v1's; v2 adaptation: v1's `FakeApi` served the rows
and persisted the PUTs, and here `_FakeStore` does the same for the rows the
orchestrator reads and the supersedes it writes back -- the store serves the
full population (the orchestrator's read includes superseded rows, in no
particular order unless asked) and a supersede persists across runs.

The validator runs hourly on every dive whose laser labelling is complete, and
superseding is a dead letter. It used to fetch only the still-live labels, fit
a line through them and supersede what `flag_outliers` flagged -- so each run
re-fitted the previous run's survivors, and more labels crossed the cut, pass
after pass. Prod dive 521 lost 15, then 7, then 1 label on three consecutive
hourly runs (2026-09-05); across all 272 dives 14,523 labels were superseded.
fishsense-core #88 documents the contract this file pins: fit and flag the
FULL population every run, superseded labels included, in a stable order.
"""

from __future__ import annotations

import random

import numpy as np
import pytest
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.laser import ValidateLaserLabelsInput
from fishsense_services_processor.laser_validation import judgement
from fishsense_services_processor.laser_validation.activities import (
    validate_laser_labels_for_dive,
)

from ._laser import DIVE, INTERCEPT, NORMAL, SLOPE, label


def _prod_like_dive(seed: int, n: int = 100, sigma: float = 1.85, offsets=None):
    """~100 positives on one laser line with prod-like perpendicular noise,
    plus a handful of genuine mislabels 30+ px off. `offsets` adds a
    per-frame shift along the normal (a drifting line)."""
    rng = np.random.default_rng(seed)
    xs = np.linspace(50.0, 1500.0, n)
    perp = rng.normal(0.0, sigma, size=n)
    if offsets is not None:
        perp = perp + offsets
    for i, off in zip(rng.choice(n, size=4, replace=False), (35.0, -42.0, 60.0, -90.0)):
        perp[i] += off
    pts = np.column_stack([xs, SLOPE * xs + INTERCEPT]) + perp[:, None] * NORMAL
    return [label(i + 1, 1000 + i, p) for i, p in enumerate(pts)]


class _FakeStore:
    """The dive's rows as the orchestrator reads them -- every row, superseded
    included -- and the supersedes it writes, which persist across runs."""

    def __init__(self, labels, *, shuffle_seed: int | None = None):
        self.labels = labels
        self.shuffle_seed = shuffle_seed
        self.writes: list[int] = []

    def read(self):
        rows = [row.model_copy() for row in self.labels]
        if self.shuffle_seed is not None:
            random.Random(self.shuffle_seed).shuffle(rows)
        return rows

    def write(self, result) -> int:
        by_id = {row.label_id: row for row in self.labels}
        written = 0
        for supersede in result.supersede:
            row = by_id[supersede.label_id]
            if not row.superseded:  # the store writes only still-live rows
                row.superseded = True
                self.writes.append(row.number)
                written += 1
        return written


async def _run(store: _FakeStore) -> int:
    result = await ActivityEnvironment().run(
        validate_laser_labels_for_dive,
        ValidateLaserLabelsInput(dive_id=DIVE, labels=store.read()),
    )
    return store.write(result)


@pytest.mark.parametrize("seed", range(5))
async def test_a_second_run_supersedes_nothing_new(seed):
    store = _FakeStore(_prod_like_dive(seed))

    first = await _run(store)
    after_first = list(store.writes)
    second = await _run(store)

    assert first >= 4, "the four genuine mislabels must still go"
    assert second == 0
    assert store.writes == after_first


async def test_a_drifting_line_does_not_erode_run_after_run():
    """Dive 223's shape: the line walks from -6.7 to +4.4 px across the dive.
    The first run's judgement must be the ONLY judgement."""
    n = 120
    store = _FakeStore(_prod_like_dive(11, n=n, offsets=np.linspace(-6.7, 4.4, n)))

    counts = [await _run(store) for _ in range(4)]

    assert counts[1:] == [0, 0, 0], f"supersedes per run: {counts}"


async def test_the_guard_does_not_depend_on_the_noise_estimate(monkeypatch):
    """ANY single-pass judge re-applied to its own survivors keeps finding
    someone; judging the same full population every time settles after one."""

    def farthest_one(xy, fit, **_kwargs):
        dist = fit.perpendicular_distance(xy[:, 0], xy[:, 1])
        flags = np.zeros(xy.shape[0], dtype=bool)
        flags[int(np.argmax(dist))] = True
        return flags

    monkeypatch.setattr(judgement, "flag_outliers", farthest_one)
    store = _FakeStore(_prod_like_dive(0))

    counts = [await _run(store) for _ in range(3)]

    assert counts == [1, 0, 0], f"supersedes per run: {counts}"


@pytest.mark.parametrize("shuffle_seed", range(5))
async def test_the_result_does_not_depend_on_row_order(shuffle_seed):
    """RANSAC picks point pairs by row index: the validator imposes its own."""
    reference = _FakeStore(_prod_like_dive(3))
    await _run(reference)

    shuffled = _FakeStore(_prod_like_dive(3), shuffle_seed=shuffle_seed)
    await _run(shuffled)

    assert sorted(shuffled.writes) == sorted(reference.writes)


async def test_a_superseded_label_the_fit_keeps_is_not_revived():
    """Reviving is the reviewed remediation tool's, never the hourly job's --
    and the result has no way to say "revive"."""
    labels = _prod_like_dive(0)
    labels[10].superseded = True
    store = _FakeStore(labels)

    result = await ActivityEnvironment().run(
        validate_laser_labels_for_dive,
        ValidateLaserLabelsInput(dive_id=DIVE, labels=store.read()),
    )

    assert labels[10].label_id not in {s.label_id for s in result.supersede}
    assert labels[10].superseded is True


async def test_an_already_superseded_outlier_is_not_asked_for_again():
    first = _FakeStore(_prod_like_dive(0))
    superseded_first = await _run(first)

    result = await ActivityEnvironment().run(
        validate_laser_labels_for_dive,
        ValidateLaserLabelsInput(dive_id=DIVE, labels=first.read()),
    )

    assert superseded_first > 0 and result.supersede == []


def test_the_fit_is_fishsense_cores_not_a_vendored_copy():
    import fishsense_core.laser as core_laser

    assert judgement.fit_dive_line is core_laser.fit_dive_line
    assert judgement.flag_outliers is core_laser.flag_outliers

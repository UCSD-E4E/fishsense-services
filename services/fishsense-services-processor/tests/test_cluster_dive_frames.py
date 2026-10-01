"""Unit tests for the stage-1 clustering kernel.

Ported from fishsense-lite@a8b2c3bc services/fishsense-data-processing-workflow-
worker/tests/test_cluster_dive_frames_activity.py. Test names, bodies and
reasons are v1's; v2 adaptation: frames are capture UUIDs (``UUID(int=i)`` keeps
v1's integer arithmetic readable), so the output is ``list[list[UUID]]``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts import ClusterDiveFrameImage
from fishsense_services_processor.clustering.activities import cluster_dive_frames


def _img(i: int, ts: datetime) -> ClusterDiveFrameImage:
    return ClusterDiveFrameImage(capture_id=UUID(int=i), taken_datetime=ts)


def _ints(result) -> list[list[int]]:
    return [[c.int for c in cluster] for cluster in result]


async def test_returns_capture_ids_grouped_by_temporal_proximity():
    """Two well-separated dense clusters (5 images each, 10 minutes
    apart) reliably resolve into two HDBSCAN clusters with default
    parameters. The contract check: output is `list[list[UUID]]` of
    capture ids, every input id appears, two distinct clusters survive."""
    base = datetime(2026, 5, 5, 10, 0, 0, tzinfo=timezone.utc)
    cluster_a = [_img(i, base + timedelta(seconds=i)) for i in range(1, 6)]
    cluster_b = [
        _img(10 + i, base + timedelta(minutes=10, seconds=i)) for i in range(1, 6)
    ]
    images = cluster_a + cluster_b

    result = await ActivityEnvironment().run(cluster_dive_frames, images)

    assert isinstance(result, list)
    assert all(isinstance(c, list) for c in result)
    assert all(isinstance(capture_id, UUID) for c in result for capture_id in c)
    flat = sorted(capture_id for c in result for capture_id in c)
    assert flat == sorted(img.capture_id for img in images)
    assert len(result) == 2
    cluster_sets = [set(c) for c in result]
    assert {img.capture_id for img in cluster_a} in cluster_sets
    assert {img.capture_id for img in cluster_b} in cluster_sets


async def test_returns_empty_when_no_images():
    result = await ActivityEnvironment().run(cluster_dive_frames, [])
    assert not result


# --- no frame may be dropped -------------------------------------------------
#
# Two defects, one root cause: HDBSCAN noise points (label -1) were discarded.
#
# 1. A dive whose frames are EVENLY SPACED has no density variation for HDBSCAN
#    to find, so every point is noise and the activity returned `[]`. Nothing
#    persisted, the stage-1 cohort's "has no PREDICTION cluster" gate stayed
#    true, and the dive was re-selected forever -- head-of-line blocking every
#    later dive. Prod dive 8 (2 frames, 2 s apart) was selected 8 times in a row
#    on 2026-09-16. Measured: uniform spacing returns all-noise for n = 2..6.
#
# 2. Even on a dive that clusters well, noise frames vanished. Prod dive 5 has
#    17 canonical frames and 16 in clusters, so one frame had no PREDICTION
#    cluster, never entered stage 2, and was invisible to species labelling.
#
# The fix is to treat a noise point as its own singleton cluster. A PREDICTION
# cluster is a *prediction* that labelers correct in stage 6.1, and "this frame
# groups with nothing" is the honest neutral answer -- unlike lumping every
# unclustered frame together, which would assert a grouping the data does not
# support and would be actively wrong on a reef dive holding many fish.


async def test_evenly_spaced_frames_still_produce_clusters():
    """The prod dive-8 shape: 2 canonical frames 2 s apart, both laser-valid.
    Returned `[]` before, which is what wedged the cohort."""
    base = datetime(2026, 5, 5, 10, 0, 0, tzinfo=timezone.utc)
    images = [_img(1, base), _img(2, base + timedelta(seconds=2))]

    result = await ActivityEnvironment().run(cluster_dive_frames, images)

    assert result, "an evenly-spaced dive must not return an empty cluster list"
    assert sorted(i for c in _ints(result) for i in c) == [1, 2]


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 9])
async def test_uniform_spacing_at_every_small_n(n):
    """Measured before the fix: HDBSCAN returns all-noise for uniform spacing
    across this whole range, so every one of these wedged."""
    base = datetime(2026, 5, 5, 10, 0, 0, tzinfo=timezone.utc)
    images = [_img(i, base + timedelta(seconds=2 * i)) for i in range(n)]

    result = await ActivityEnvironment().run(cluster_dive_frames, images)

    assert sorted(i for c in _ints(result) for i in c) == list(range(n))


async def test_a_noise_frame_becomes_its_own_cluster_rather_than_vanishing():
    """The prod dive-5 shape: a dense burst plus one stray frame far away. The
    stray must survive as a singleton -- before, it was dropped and never
    reached species labelling."""
    base = datetime(2026, 5, 5, 10, 0, 0, tzinfo=timezone.utc)
    burst = [_img(i, base + timedelta(seconds=i)) for i in range(1, 8)]
    stray = _img(99, base + timedelta(hours=3))

    result = _ints(
        await ActivityEnvironment().run(cluster_dive_frames, burst + [stray])
    )

    assert [99] in result, "the stray frame must appear as its own cluster"
    assert sorted(i for c in result for i in c) == list(range(1, 8)) + [99]


@pytest.mark.parametrize(
    "offsets",
    [
        [0, 1, 2, 3, 4],                      # one tight burst
        [0, 2, 4, 6, 8, 10],                  # uniform
        [0, 1, 2, 600, 601, 602],             # two bursts
        [0, 1, 2, 600, 601, 602, 7200],       # two bursts plus a stray
        [0],                                  # a single frame
        [0, 3600, 7200, 10800],               # uniform but widely spaced
    ],
)  # fmt: skip
async def test_every_frame_appears_in_exactly_one_cluster(offsets):
    """The invariant that fixes both defects at once. Whatever the temporal
    shape, the partition must cover the input exactly -- no frame dropped, none
    duplicated. A dropped frame is silently unprocessable; a duplicated one
    would double-count in stage 2's overlay."""
    base = datetime(2026, 5, 5, 10, 0, 0, tzinfo=timezone.utc)
    images = [_img(i, base + timedelta(seconds=o)) for i, o in enumerate(offsets)]

    result = _ints(await ActivityEnvironment().run(cluster_dive_frames, images))

    flat = [i for c in result for i in c]
    assert sorted(flat) == list(range(len(offsets)))
    assert len(flat) == len(set(flat)), "a frame appears in two clusters"
    assert all(c for c in result), "an empty cluster was emitted"


async def test_real_burst_structure_is_still_grouped():
    """Regression guard: the fix must not degrade a dive that HDBSCAN handles
    well into a pile of singletons. Prod dive 5 resolves into 8 clusters."""
    base = datetime(2026, 5, 5, 10, 0, 0, tzinfo=timezone.utc)
    images = []
    for burst in range(4):
        for k in range(3):
            images.append(
                _img(burst * 10 + k, base + timedelta(minutes=10 * burst, seconds=k))
            )

    result = await ActivityEnvironment().run(cluster_dive_frames, images)

    multi = [c for c in result if len(c) > 1]
    assert len(multi) == 4, f"expected 4 grouped bursts, got {result}"


async def test_empty_input_still_returns_empty():
    """A dive with no images to cluster has nothing to say. Unchanged -- the
    parent never dispatches this case, and inventing a cluster would be worse."""
    assert await ActivityEnvironment().run(cluster_dive_frames, []) == []


async def test_a_single_frame_dive_does_not_raise():
    """HDBSCAN raises `n_samples=1 while HDBSCAN requires more than one
    sample`, so before the guard a one-frame dive failed the activity, the
    child, and the parent -- a louder failure than the empty-list wedge but
    with the same outcome: the dive never drains."""
    base = datetime(2026, 5, 5, 10, 0, 0, tzinfo=timezone.utc)
    result = await ActivityEnvironment().run(cluster_dive_frames, [_img(7, base)])
    assert _ints(result) == [[7]]

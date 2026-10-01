"""Stage 1 (dive-frame clustering) activity.

Ported from fishsense-lite@a8b2c3bc services/fishsense-data-processing-workflow-
worker/src/fishsense_data_processing_workflow_worker/activities/
cluster_dive_frames.py. The kernel is verbatim (HDBSCAN on timestamps,
`min_cluster_size=2`, scikit-learn pinned to v1's lock). v2 changes: frames are
capture UUIDs, from the processing contract; HDBSCAN's `copy` is stated rather
than defaulted (see below).

**Every input frame comes back in exactly one cluster**, and that is the
contract this module exists to keep. The notebook port dropped HDBSCAN noise
points (label -1), which caused two defects with one root cause:

* A dive whose frames are EVENLY SPACED has no density variation for HDBSCAN to
  find, so every point is noise and the activity returned `[]`. Nothing
  persisted, the stage-1 cohort's "has no PREDICTION cluster" gate stayed true,
  and the dive was re-selected hourly forever -- head-of-line blocking every
  later dive. Prod dive 8 (2 frames, 2 s apart) was selected 8 times in a row
  on 2026-09-16, and uniform spacing was measured to return all-noise for
  n = 2..6.
* A dive holding exactly ONE frame made HDBSCAN raise
  (`n_samples=1 while HDBSCAN requires more than one sample`), failing the
  activity rather than returning nothing -- so the child and parent failed too,
  and the dive still never drained. Guarded below.
* Even where clustering worked, noise frames vanished. Prod dive 5 holds 17
  canonical frames and only 16 reached clusters, so one frame had no PREDICTION
  cluster, never entered stage 2, and was invisible to species labelling.

A noise point therefore becomes its own singleton cluster. That is the honest
neutral answer rather than a workaround: a PREDICTION cluster is a *prediction*
which labelers correct in stage 6.1, and "this frame groups with nothing" is
what the data says. The alternative -- one cluster holding every unclustered
frame -- would assert a grouping the data does not support, and would be
actively wrong on a reef dive holding many different fish.
"""

from __future__ import annotations

from typing import Iterable, List
from uuid import UUID

from sklearn.cluster import HDBSCAN
from temporalio import activity

from fishsense_services_contracts import ClusterDiveFrameImage

__all__ = ["cluster_dive_frames"]


@activity.defn
async def cluster_dive_frames(
    images: Iterable[ClusterDiveFrameImage],
) -> List[List[UUID]]:
    """Cluster a dive's captures by their taken_datetime timestamps.

    Returns:
        list[list[UUID]]: capture ids grouped by temporal cluster. Every input
        frame appears in exactly one cluster; a frame HDBSCAN calls noise
        becomes a singleton rather than being dropped. Input order is
        preserved within and across clusters, so the output is deterministic
        for a given input.
    """
    image_list = list(images)
    if not image_list:
        return []

    if len(image_list) == 1:
        # HDBSCAN raises `n_samples=1 while HDBSCAN requires more than one
        # sample`, so a one-frame dive would fail the activity outright rather
        # than merely returning nothing -- the child workflow fails, the parent
        # fails, and the dive still never drains. One frame is one group.
        activity.logger.info("Clustering 1 image: single frame, one cluster")
        return [[image_list[0].capture_id]]

    timestamps = [[img.taken_datetime.timestamp()] for img in image_list]

    activity.logger.info("Clustering %d images", len(image_list))

    # `copy=False` is the current default, stated: scikit-learn flips it in
    # 1.10. It only says whether HDBSCAN may modify its input, and `timestamps`
    # is built fresh here, so the clusters are the same either way.
    db = HDBSCAN(min_cluster_size=2, copy=False).fit(timestamps)
    labels = db.labels_

    clusters: dict[int, List[UUID]] = {}
    singletons: List[List[UUID]] = []
    for label, img in zip(labels, image_list):
        if int(label) == -1:
            # Noise: its own cluster. Not keyed into `clusters`, because every
            # noise point shares the label -1 and would otherwise collapse into
            # one bogus group.
            singletons.append([img.capture_id])
            continue
        clusters.setdefault(int(label), []).append(img.capture_id)

    grouped = list(clusters.values())
    activity.logger.info(
        "Clustered %d images into %d group(s) and %d singleton(s)",
        len(image_list),
        len(grouped),
        len(singletons),
    )
    return grouped + singletons

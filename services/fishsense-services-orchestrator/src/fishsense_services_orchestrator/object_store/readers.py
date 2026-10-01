"""Which children read a dive's raw scratch, and the ids they run under.

Ported from fishsense-lite@77e8f8e5
services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/
activities/cleanup_raw_bytes_for_dive_activity.py (`raw_scratch_reader_ids`,
`build_scratch_in_use_query`). Pure, so a parent workflow can import it to name
its child.

Scratch is keyed per **dive**, not per stage -- which is what lets a dive in
several cohorts stage once -- so deleting it is a cross-stage act, and the
cleanup gate must know every child that reads it. v1 kept that as a list and a
comment's promise; the checkerboard pair sat outside it for days, and a
preprocess cleanup could evict a calibration dive's scratch mid-fit.

v2 change: **a parent builds its raw-reading child's id with
`raw_scratch_reader_id`**, which refuses a reader not in `RAW_SCRATCH_READERS`.
Adding a raw-reading stage without telling the cleanup gate now fails when its
parent builds the id, instead of silently deleting frames under it.
"""

from __future__ import annotations

import uuid

__all__ = [
    "RAW_SCRATCH_READERS",
    "build_scratch_in_use_query",
    "raw_scratch_reader_id",
    "raw_scratch_reader_ids",
]

#: Every child workflow that reads a dive's raw scratch, by id prefix. The id
#: is ``{reader}-{dive_id}``.
RAW_SCRATCH_READERS = (
    "preprocess-laser",
    "preprocess-species",
    "preprocess-headtail",
    "preprocess-slate",
    "predict-laser",
    "predict-slate",
    "perform-checkerboard-calibration",
    "verify-checkerboard-lattice",
)


def raw_scratch_reader_id(reader: str, dive_id: uuid.UUID) -> str:
    """The deterministic id of a raw-reading child of this dive."""
    if reader not in RAW_SCRATCH_READERS:
        raise ValueError(
            f"{reader!r} is not in RAW_SCRATCH_READERS: add it there, or cleanup "
            "will delete this dive's raw scratch while the child is reading it"
        )
    return f"{reader}-{dive_id}"


def raw_scratch_reader_ids(dive_id: uuid.UUID) -> list[str]:
    """Every raw-reading child id this dive could have."""
    return [raw_scratch_reader_id(reader, dive_id) for reader in RAW_SCRATCH_READERS]


def build_scratch_in_use_query(dive_id: uuid.UUID) -> str:
    """Temporal visibility query: is any of this dive's readers still running?

    ``WorkflowId IN (...)``, deliberately not a prefix match -- v1's dive 44 must
    not hold dive 442's scratch open. (v2's dive ids are uuids, so a prefix
    could not collide the same way; exact ids are still the honest question.)
    """
    ids = ", ".join(f"'{wid}'" for wid in raw_scratch_reader_ids(dive_id))
    return f'ExecutionStatus = "Running" and WorkflowId in ({ids})'

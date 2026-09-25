"""The processing contract (PLAN.md §9.1): versioned, schema-first, pinned.

The orchestrator and the processor deploy independently -- the orchestrator in
the Incus slot, the processor on NRP, often days apart -- so a payload shape
that changes on one side only fails at runtime, on the other. The committed
JSON Schema for the current version is the agreement; changing a model without
publishing a new version fails here, in CI, instead.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from fishsense_services_contracts import (
    CONTRACT_VERSION,
    PROCESSOR_GPU_TASK_QUEUE,
    PROCESSOR_LIGHT_TASK_QUEUE,
    PROCESSOR_TASK_QUEUE,
    ClusterDiveFrameImage,
    ClusterDiveFramesInput,
    json_schema,
)

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


def test_the_published_schema_for_this_version_is_what_the_models_produce():
    """Regenerate with `python -m fishsense_services_contracts > schemas/vN.json`
    after bumping CONTRACT_VERSION -- never by editing the committed file for a
    version already deployed."""
    published = SCHEMAS / f"v{CONTRACT_VERSION}.json"
    assert published.exists(), f"no published schema for v{CONTRACT_VERSION}"

    assert json.loads(published.read_text()) == json_schema(), (
        "the contract changed without a new version: bump CONTRACT_VERSION and "
        "publish its schema"
    )


def test_every_published_version_is_kept():
    """Old versions stay, so a processor still speaking one can be checked
    against it during a rolling deploy."""
    versions = sorted(int(p.stem[1:]) for p in SCHEMAS.glob("v*.json"))

    assert versions == list(range(1, CONTRACT_VERSION + 1))


def test_v2_queues_never_collide_with_v1s():
    """Temporal is shared until cutover. A v2 child on a v1 queue would be
    taken by a v1 data-worker -- and accepted, and sit `Running` until its
    timeout with nothing in either worker's logs (v1's task_queues.py)."""
    v1 = {
        "fishsense_data_processing_queue",
        "fishsense_data_processing_gpu_queue",
        "fishsense_data_processing_light_queue",
        "fishsense_api_queue",
    }
    ours = {PROCESSOR_TASK_QUEUE, PROCESSOR_GPU_TASK_QUEUE, PROCESSOR_LIGHT_TASK_QUEUE}

    assert len(ours) == 3
    assert not ours & v1


def test_clustering_input_carries_capture_ids_and_timestamps():
    capture = uuid4()
    payload = ClusterDiveFramesInput(
        dive_id=uuid4(),
        images=[
            ClusterDiveFrameImage(
                capture_id=capture, taken_datetime=datetime(2024, 8, 21, tzinfo=UTC)
            )
        ],
    )

    round_tripped = ClusterDiveFramesInput.model_validate_json(
        payload.model_dump_json()
    )

    assert round_tripped == payload
    assert round_tripped.images[0].capture_id == capture


def test_a_naive_timestamp_is_refused():
    """Clustering is timestamp arithmetic; a naive value is ambiguous by
    exactly the camera's UTC offset. Every stored capture time is aware."""
    with pytest.raises(ValidationError):
        ClusterDiveFrameImage(capture_id=uuid4(), taken_datetime=datetime(2024, 8, 21))

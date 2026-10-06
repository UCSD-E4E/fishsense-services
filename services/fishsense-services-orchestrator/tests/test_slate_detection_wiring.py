"""The slate detector across the orchestrator and the processor.

New in v2, built as test_species_prediction_wiring.py is: the orchestrator's
real parent and activities and the processor's real workflow and activity, on
their real queues, with fakes only at the edges (the catalog, the scratch
staging, S3, the weights, the frame render and the classifier). Stubbed
activities cannot catch a payload that does not round-trip between the two
packages -- the raw ref, the intrinsics, the probability and its provenance
-- or a name one calls the other doesn't register.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from fishsense_services_api.slate_presence_store import (
    SlateDetectionCandidate,
    SlateDetectionCapture,
    SlateDetectionInputs,
)
from fishsense_services_contracts.slate_presence import SLATE_DETECTOR_VERSION
from fishsense_services_orchestrator.nrp.activities import NrpActivities
from fishsense_services_orchestrator.object_store.contracts import (
    CleanupRawBytesResult,
    StageRawBytesResult,
    StagingTarget,
)
from fishsense_services_orchestrator.slate_detect.activities import (
    SlateDetectionActivities,
)
from fishsense_services_orchestrator.slate_detect.workflow import (
    DetectSlatePresenceParentWorkflow,
)
from fishsense_services_orchestrator.worker import build_worker
from fishsense_services_processor.registry import ROLE_GPU, Stage
from fishsense_services_processor.slate_detect.activities import (
    SlateDetectActivities,
)
from fishsense_services_processor.slate_detect.workflow import (
    DetectSlatePresenceWorkflow,
)
from fishsense_services_processor.worker import build_worker as build_processor

from ._species import LAYOUT

TENANT = uuid.uuid4()
DIVE = uuid.uuid4()
T0 = datetime(2026, 9, 1, tzinfo=UTC)
K = [[3500.0, 0.0, 2000.0], [0.0, 3500.0, 1500.0], [0.0, 0.0, 1.0]]
D = [-0.1, 0.05, 0.0, 0.0, 0.0]
SHA = "b8d377ba22d155e7056a5e9ae747fdd0970c7c73dee981bbee17d95c8156cf78"


def _activities_of(instance) -> list:
    return [
        getattr(instance, name)
        for name in dir(instance)
        if hasattr(getattr(instance, name), "__temporal_activity_definition")
    ]


class _Catalog:
    def __init__(self):
        self.slate, self.water = uuid.uuid4(), uuid.uuid4()
        self.persisted = []

    async def member_tenants(self):
        return [TENANT]

    async def next_dive_for_slate_detection(
        self, tenant_id, *, model_version, exclude=()
    ):
        return None if DIVE in exclude else SlateDetectionCandidate(DIVE, T0)

    async def slate_detection_inputs(self, tenant_id, dive_id, *, model_version):
        return SlateDetectionInputs(
            DIVE,
            K,
            D,
            [
                SlateDetectionCapture(self.slate, "a" * 32),
                SlateDetectionCapture(self.water, "b" * 32),
            ],
        )

    async def persist_slate_presence(self, tenant_id, dive_id, rows):
        self.persisted.extend(rows)
        return len(rows)


class _ProcessorStore:
    def __init__(self):
        self.read = []

    async def download_raw(self, ref, directory):
        self.read.append(ref)
        path = Path(directory) / Path(ref.key).name
        path.write_text(ref.key)
        return path


@activity.defn(name="stage_raw_bytes_for_dive")
async def _stage(target: StagingTarget) -> StageRawBytesResult:
    return StageRawBytesResult(staged=2, skipped_already_present=0, no_path=0)


@activity.defn(name="cleanup_raw_bytes_for_dive")
async def _cleanup(target: StagingTarget) -> CleanupRawBytesResult:
    return CleanupRawBytesResult(deleted=2)


class _Classifier:  # pylint: disable=too-few-public-methods
    """The 'frame' is the raw's key; the `a...` frame holds a slate."""

    def probability(self, frame):
        return 0.98 if "a" * 32 in frame else 0.01


async def test_detection_runs_across_the_orchestrator_and_the_processor():
    catalog = _Catalog()
    store = _ProcessorStore()
    rendered = []

    async def weights():
        return Path("/cache/slate-detector/q1/slate_efficientnet_b0.pt"), SHA

    def render(raw, matrix, distortion):
        rendered.append((matrix, distortion))
        return Path(raw).read_text()

    processor = SlateDetectActivities(
        store_factory=lambda: store,
        weights=weights,
        load_classifier=lambda path, sha: _Classifier(),
        render=render,
    )
    orchestrator = SlateDetectionActivities(catalog=catalog, layout=LAYOUT)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            build_worker(
                env.client,
                activities=[
                    *_activities_of(orchestrator),
                    *_activities_of(NrpActivities(config=None)),
                    _stage,
                    _cleanup,
                ],
                task_queue="wiring",
            ),
            build_processor(
                env.client,
                role=ROLE_GPU,
                stages=[
                    Stage(
                        "slate_detect",
                        ROLE_GPU,
                        [DetectSlatePresenceWorkflow],
                        [processor.detect_slate_presence],
                    )
                ],
            ),
        ):
            target = await env.client.execute_workflow(
                DetectSlatePresenceParentWorkflow.run,
                id=f"wiring-{uuid.uuid4()}",
                task_queue="wiring",
            )

    assert target == StagingTarget(TENANT, DIVE)
    assert sorted(store.read, key=lambda r: r.key) == [
        LAYOUT.raw(TENANT, "a" * 32),
        LAYOUT.raw(TENANT, "b" * 32),
    ], "the staged raws the orchestrator issued"
    assert rendered == [(K, D)] * 2
    by_capture = {row.capture_id: row for row in catalog.persisted}
    assert by_capture[catalog.slate].probability == 0.98
    assert by_capture[catalog.water].probability == 0.01
    assert {
        (r.status, r.model_version, r.weights_sha256) for r in by_capture.values()
    } == {("predicted", SLATE_DETECTOR_VERSION, SHA)}
    row = by_capture[catalog.slate]
    assert (row.model_name, row.core_version) == (
        "slate-detector",
        version("fishsense-core"),
    )
    assert row.processor_version == version("fishsense-services-processor")
    assert (row.render["decode_config"], row.render["rectified"]) == (
        "production",
        True,
    )
    assert (row.render["input_width"], row.render["input_height"]) == (1024, 768)
    assert row.predicted_at.tzinfo is not None

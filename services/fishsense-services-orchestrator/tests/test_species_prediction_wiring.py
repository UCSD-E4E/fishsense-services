"""The species pre-annotation chain across the orchestrator and the processor.

New in v2 (no v1 counterpart), built as test_headtail_wiring.py is: the
orchestrator's real parent and activities and the processor's real workflow
and activity, on their real queues, with fakes only at the edges (the
catalogs, S3, BioCLIP's weights and encoder). Stubbed activities cannot catch
a payload that does not round-trip between the two packages -- the candidates,
the mask box, the scores -- or a name one calls the other doesn't register.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from fishsense_services_api.species_prediction_store import (
    SpeciesPredictCapture,
    SpeciesPredictionCandidate,
    SpeciesPredictionState,
)
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.nrp.activities import NrpActivities
from fishsense_services_orchestrator.object_store.layout import ObjectLayout
from fishsense_services_orchestrator.object_store.store import OrchestratorObjectStore
from fishsense_services_orchestrator.species_predict.activities import (
    SpeciesPredictActivities as OrchestratorActivities,
)
from fishsense_services_orchestrator.species_predict.activities import (
    SpeciesPredictTarget,
)
from fishsense_services_orchestrator.species_predict.settings import (
    SpeciesPredictionSettings,
)
from fishsense_services_orchestrator.species_predict.workflow import (
    PredictSpeciesImagesParentWorkflow,
)
from fishsense_services_orchestrator.worker import build_worker
from fishsense_services_processor.registry import ROLE_GPU, Stage
from fishsense_services_processor.species_predict.activities import (
    SpeciesPredictActivities as ProcessorActivities,
)
from fishsense_services_processor.species_predict.workflow import (
    PredictSpeciesImagesWorkflow,
)
from fishsense_services_processor.worker import build_worker as build_processor

TENANT = uuid.uuid4()
DIVE = uuid.uuid4()
T0 = datetime(2026, 9, 1, tzinfo=UTC)
LAYOUT = ObjectLayout(
    ObjectStoreConnection(
        endpoint_url="https://s3.example.test", region="garage", access_key_id="k",
        secret_access_key="s", bucket="fishsense-lite",
        labels_bucket="labels-fishsense-lite", legacy_labels_prefix="fishsense-lite",
    )  # fmt: skip
)
BOX = [150, 100, 250, 200]


def _activities_of(instance) -> list:
    return [
        getattr(instance, name)
        for name in dir(instance)
        if hasattr(getattr(instance, name), "__temporal_activity_definition")
    ]


class _Catalog:
    def __init__(self):
        self.capture = uuid.uuid4()
        self.headtail = uuid.uuid4()
        self.persisted = []

    async def member_tenants(self):
        return [TENANT]

    async def next_dive_for_species_prediction(self, tenant_id, *, predictor_version):
        return SpeciesPredictionCandidate(DIVE, T0, never_predicted=True)

    async def species_predict_captures(self, tenant_id, dive_id, *, predictor_version):
        return [
            SpeciesPredictCapture(
                capture_id=self.capture, checksum="a" * 32, from_v1=False,
                headtail_prediction_id=self.headtail, mask_bbox=BOX,
                has_existing_prediction=False,
            )
        ]  # fmt: skip

    async def persist_species_predictions(self, tenant_id, dive_id, rows):
        self.persisted.extend(rows)
        return len(rows)

    async def species_prediction_state(self, tenant_id, dive_id):
        return SpeciesPredictionState(42, [], [])


class _S3:
    """Every HEAD finds its object (head/tail rendered the JPEG)."""

    def head_object(self, Bucket, Key):  # pylint: disable=invalid-name
        return {}


class _ProcessorStore:
    def __init__(self, jpeg):
        self.jpeg = jpeg
        self.read = []

    async def download_processed_jpeg(self, ref):
        self.read.append(ref)
        return self.jpeg


class _Encoder:
    """Grey text for every species but Grey Snapper; the crop reads as grey."""

    logit_scale = 100.0

    def __init__(self):
        self.seen = []

    def encode_text(self, prompts):
        axis = 1 if "Lutjanus griseus" in prompts[0] else 0
        return np.stack([np.eye(2)[axis]] * len(prompts))

    def encode_image(self, image):
        self.seen.append(image.size)
        return np.array([0.0, 1.0])


async def test_prediction_runs_across_the_orchestrator_and_the_processor():
    ok, jpeg = cv2.imencode(".jpg", np.full((300, 400, 3), 90, dtype=np.uint8))
    assert ok
    catalog = _Catalog()
    store = _ProcessorStore(jpeg.tobytes())
    encoder = _Encoder()

    async def weights(model_id):
        return Path("/cache/bioclip/2.5-vith14"), "bioclip/2.5-vith14@0123456789ab"

    processor = ProcessorActivities(
        store_factory=lambda: store,
        bioclip_weights=weights,
        load_encoder=lambda _directory: encoder,
    )
    orchestrator = OrchestratorActivities(
        catalog=catalog,
        species_catalog=None,
        store=OrchestratorObjectStore(_S3(), LAYOUT),
        settings=SpeciesPredictionSettings(enabled=False),
        label_studio_factory=lambda: None,
    )

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            build_worker(
                env.client,
                activities=[
                    *_activities_of(orchestrator),
                    *_activities_of(NrpActivities(config=None)),
                ],
                task_queue="wiring",
            ),
            build_processor(
                env.client,
                role=ROLE_GPU,
                stages=[
                    Stage(
                        "species_predict",
                        ROLE_GPU,
                        [PredictSpeciesImagesWorkflow],
                        [processor.predict_species_image],
                    )
                ],
            ),
        ):
            target = await env.client.execute_workflow(
                PredictSpeciesImagesParentWorkflow.run,
                id=f"wiring-{uuid.uuid4()}",
                task_queue="wiring",
            )

    assert target == SpeciesPredictTarget(TENANT, DIVE)
    assert store.read == [
        LAYOUT.processed_jpeg(TENANT, "preprocess_headtail_jpeg", "a" * 32)
    ], "the head/tail stage's JPEG"
    assert encoder.seen == [(140, 140)], "the mask's box, padded 20%"
    (row,) = catalog.persisted
    assert (row.capture_id, row.headtail_prediction_id, row.status) == (
        catalog.capture,
        catalog.headtail,
        "predicted",
    )
    assert row.predicted_choice == "Fish, Grey Snapper (Lutjanus griseus)"
    assert len(row.top5) == 5 and row.top1_probability > 0.99
    assert (row.predictor_version, row.model_id) == (
        1,
        "bioclip/2.5-vith14@0123456789ab",
    )

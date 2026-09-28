"""The head/tail stages' orchestrator activities: select, resolve, clear, persist.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/tests/
test_select_next_high_priority_dive_for_headtail_preprocessing_activity.py,
test_resolve_headtail_preprocess_inputs_activity.py,
test_resolve_headtail_predict_inputs.py (`TestJpegPresenceGate`) and the
persist/clear activities' contracts. v1's activities were thin SDK calls; v2's
call a catalog, and the cohorts and resolvers are the store's, tested against
Postgres (fishsense-services-api tests/test_headtail_store.py). What is pinned
here is what the activities add:

* **the oldest candidate across every tenant the orchestrator serves**, and
  for prediction, never-predicted work before upgrades across tenants too;
* **the orchestrator issues the keys** (PLAN.md §9.11): a frame's raw ref is
  its tenant's scratch key, and its JPEG is written where it already is (v1's
  key for a migrated frame) or under the tenant;
* the predict resolver defers an image whose stage-5.1 JPEG isn't written yet
  (v1's `_only_with_rendered_jpeg`), and hands the processor the located ref;
* a refusal of the processor's output is final.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.headtail_store import (
    ForeignCapture,
    HeadtailCandidate,
    HeadtailPreprocessInputs,
    LaserDot,
    PredictCapture,
    PredictionCandidate,
    PreprocessCapture,
)
from fishsense_services_contracts.headtail import (
    HEADTAIL_PREDICTOR_VERSION,
    HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE,
    HeadtailPredictionResult,
)
from fishsense_services_contracts.object_store import ObjectRef, ObjectStoreConnection
from fishsense_services_orchestrator.headtail.activities import (
    ClearHeadtailReprocessFlags,
    HeadtailActivities,
    HeadtailTarget,
)
from fishsense_services_orchestrator.object_store.layout import ObjectLayout
from fishsense_services_orchestrator.object_store.store import OrchestratorObjectStore

T0 = datetime(2026, 9, 1, tzinfo=UTC)
LAB, REEF, PARTNER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
K = [[3000.0, 0.0, 2048.0], [0.0, 3000.0, 1536.0], [0.0, 0.0, 1.0]]
D = [-0.05, 0.01, 0.0, 0.0, 0.0]
SETTINGS = ObjectStoreConnection(
    endpoint_url="https://s3.example.test", region="garage", access_key_id="k",
    secret_access_key="s", bucket="fishsense-lite",
    labels_bucket="labels-fishsense-lite", legacy_labels_prefix="fishsense-lite",
)  # fmt: skip
LAYOUT = ObjectLayout(SETTINGS)


class FakeS3:
    """HEAD answers from the keys present; nothing else is called."""

    def __init__(self, present=()):
        self.present = {(r.bucket, r.key) for r in present}

    def head_object(self, Bucket, Key):  # pylint: disable=invalid-name
        if (Bucket, Key) not in self.present:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}


class FakeCatalog:
    def __init__(self, **answers):
        self.answers = answers
        self.calls: list[tuple] = []

    async def member_tenants(self):
        return [LAB, REEF, PARTNER]

    async def next_dive_for_headtail_preprocessing(self, tenant_id):
        return self.answers.get("preprocess", {}).get(tenant_id)

    async def next_dive_for_headtail_prediction(self, tenant_id, *, predictor_version):
        self.calls.append(("version", predictor_version))
        return self.answers.get("predict", {}).get(tenant_id)

    async def headtail_preprocess_inputs(self, tenant_id, dive_id):
        return self.answers["inputs"]

    async def headtail_predict_captures(self, tenant_id, dive_id, *, predictor_version):
        self.calls.append(("version", predictor_version))
        return self.answers["captures"]

    async def clear_headtail_needs_reprocess(self, tenant_id, dive_id, checksums=None):
        self.calls.append(("clear", tenant_id, dive_id, checksums))
        return 3

    async def persist_headtail_predictions(self, tenant_id, dive_id, rows):
        if "refuse" in self.answers:
            raise self.answers["refuse"]
        self.calls.append(("persist", tenant_id, dive_id, rows))
        return len(rows)


def _activities(catalog, present=()):
    return HeadtailActivities(
        catalog=catalog, store=OrchestratorObjectStore(FakeS3(present), LAYOUT)
    )


async def _run(fn, *args):
    return await ActivityEnvironment().run(fn, *args)


# -- the selectors -------------------------------------------------------------------


async def test_preprocessing_picks_the_oldest_candidate_across_tenants():
    older, newer = uuid.uuid4(), uuid.uuid4()
    catalog = FakeCatalog(
        preprocess={
            LAB: HeadtailCandidate(newer, T0 + timedelta(hours=1)),
            REEF: HeadtailCandidate(older, T0),
        }
    )

    target = await _run(
        _activities(catalog).select_next_dive_for_headtail_preprocessing
    )

    assert target == HeadtailTarget(tenant_id=REEF, dive_id=older)


async def test_preprocessing_returns_none_when_no_tenant_has_work():
    assert (
        await _run(
            _activities(FakeCatalog()).select_next_dive_for_headtail_preprocessing
        )
        is None
    )


async def test_prediction_prefers_never_predicted_work_across_tenants():
    """v1's ordering, kept across tenants: an upgrade-only dive (permanently
    stale while no GPU exists) must not starve another tenant's new work."""
    upgrade, fresh = uuid.uuid4(), uuid.uuid4()
    catalog = FakeCatalog(
        predict={
            LAB: PredictionCandidate(upgrade, T0, never_predicted=False),
            PARTNER: PredictionCandidate(fresh, T0 + timedelta(days=1), True),
        }
    )

    target = await _run(_activities(catalog).select_next_dive_for_headtail_prediction)

    assert target == HeadtailTarget(tenant_id=PARTNER, dive_id=fresh)
    assert ("version", HEADTAIL_PREDICTOR_VERSION) in catalog.calls


async def test_prediction_then_takes_the_oldest():
    older, newer = uuid.uuid4(), uuid.uuid4()
    catalog = FakeCatalog(
        predict={
            LAB: PredictionCandidate(newer, T0 + timedelta(hours=1), False),
            REEF: PredictionCandidate(older, T0, False),
        }
    )

    target = await _run(_activities(catalog).select_next_dive_for_headtail_prediction)

    assert target == HeadtailTarget(tenant_id=REEF, dive_id=older)


# -- the stage-5.1 resolver ----------------------------------------------------------


async def test_the_preprocess_inputs_carry_the_keys_the_orchestrator_issues():
    """A new frame is written under its tenant; a migrated frame whose JPEG v1
    wrote is re-rendered over it, where its Label Studio tasks point."""
    target = HeadtailTarget(LAB, uuid.uuid4())
    new, migrated = uuid.uuid4(), uuid.uuid4()
    catalog = FakeCatalog(
        inputs=HeadtailPreprocessInputs(
            captures=[
                PreprocessCapture(new, "a" * 32, from_v1=False),
                PreprocessCapture(migrated, "b" * 32, from_v1=True),
            ],
            camera_matrix=K,
            distortion_coefficients=D,
        )
    )
    v1_jpeg = LAYOUT.legacy_processed_jpeg("preprocess_headtail_jpeg", "b" * 32)

    inputs = await _run(
        _activities(catalog, present=[v1_jpeg]).resolve_headtail_preprocess_inputs,
        target,
    )

    assert (inputs.tenant_id, inputs.dive_id) == (LAB, target.dive_id)
    assert (inputs.camera_matrix, inputs.distortion_coefficients) == (K, D)
    first, second = inputs.images
    assert (first.capture_id, first.checksum) == (new, "a" * 32)
    assert first.raw == LAYOUT.raw(LAB, "a" * 32)
    assert first.jpeg == LAYOUT.processed_jpeg(
        LAB, "preprocess_headtail_jpeg", "a" * 32
    )
    assert second.jpeg == v1_jpeg


async def test_the_flag_clear_is_scoped_as_the_parent_asks():
    target = HeadtailTarget(LAB, uuid.uuid4())
    catalog = FakeCatalog()

    for checksums in (None, [], ["a" * 32]):
        cleared = await _run(
            _activities(catalog).clear_headtail_reprocess_flags,
            ClearHeadtailReprocessFlags(LAB, target.dive_id, checksums),
        )
        assert cleared == 3

    assert [c[3] for c in catalog.calls if c[0] == "clear"] == [None, [], ["a" * 32]]


# -- the predict resolver ------------------------------------------------------------


def _capture(checksum, *, from_v1=False, **flags):
    return PredictCapture(
        capture_id=uuid.uuid4(),
        checksum=checksum,
        from_v1=from_v1,
        dots=(LaserDot(uuid.uuid4(), 1.0, 2.0), LaserDot(uuid.uuid4(), 3.0, 4.0)),
        has_existing_prediction=flags.get("existing", False),
        existing_laser_superseded=flags.get("superseded", False),
    )


class TestJpegPresenceGate:
    """The predict stage reads the stage-5.1 JPEG, so it must not be
    dispatched for an image stage 5.1 hasn't rendered yet: the child would
    retry `NoSuchKey` with no ceiling until its own timeout."""

    async def test_defers_images_without_a_rendered_jpeg(self):
        rendered, late = _capture("a" * 32), _capture("b" * 32)
        catalog = FakeCatalog(captures=[rendered, late])
        jpeg = LAYOUT.processed_jpeg(LAB, "preprocess_headtail_jpeg", "a" * 32)

        inputs = await _run(
            _activities(catalog, present=[jpeg]).resolve_headtail_predict_inputs,
            HeadtailTarget(LAB, uuid.uuid4()),
        )

        assert [i.capture_id for i in inputs.images] == [rendered.capture_id]
        (image,) = inputs.images
        assert image.jpeg == jpeg, "the located ref is what the processor reads"
        assert image.laser_points == [[1.0, 2.0], [3.0, 4.0]]
        assert image.laser_label_ids == [d.laser_label_id for d in rendered.dots]

    async def test_a_migrated_frame_is_read_where_v1_wrote_it(self):
        migrated = _capture("c" * 32, from_v1=True, existing=True, superseded=True)
        v1_jpeg = LAYOUT.legacy_processed_jpeg("preprocess_headtail_jpeg", "c" * 32)

        inputs = await _run(
            _activities(
                FakeCatalog(captures=[migrated]), present=[v1_jpeg]
            ).resolve_headtail_predict_inputs,
            HeadtailTarget(LAB, uuid.uuid4()),
        )

        (image,) = inputs.images
        assert image.jpeg == v1_jpeg
        assert (image.has_existing_prediction, image.existing_laser_superseded) == (
            True,
            True,
        )

    async def test_empty_input_touches_no_object_store(self):
        class _Exploding:
            def head_object(self, **_):
                raise AssertionError("must not HEAD for an empty dive")

        activities = HeadtailActivities(
            catalog=FakeCatalog(captures=[]),
            store=OrchestratorObjectStore(_Exploding(), LAYOUT),
        )

        inputs = await _run(
            activities.resolve_headtail_predict_inputs,
            HeadtailTarget(LAB, uuid.uuid4()),
        )
        assert inputs.images == []


# -- persisting the processor's predictions -------------------------------------------


def _result(capture, **overrides):
    values = dict(
        capture_id=capture, status="predicted", head_x=1.0, head_y=2.0, tail_x=3.0,
        tail_y=4.0, width=4014, height=3016, laser_label_id=uuid.uuid4(),
        predictor_version=2, checkpoint="sam3/3.1@0123", core_version="4.1.0",
    )  # fmt: skip
    return HeadtailPredictionResult(**{**values, **overrides})


async def test_every_field_of_a_result_is_persisted():
    target = HeadtailTarget(LAB, uuid.uuid4())
    catalog = FakeCatalog()
    result = _result(uuid.uuid4(), mask_area_px=9, silhouette_ratio=0.2, crop_x=5)

    written = await _run(
        _activities(catalog).persist_headtail_predictions, target, [result]
    )

    assert written == 1
    _, tenant, dive, (row,) = next(c for c in catalog.calls if c[0] == "persist")
    assert (tenant, dive) == (LAB, target.dive_id)
    assert row.__dict__ == result.model_dump()


async def test_a_refusal_is_final():
    """Retrying re-reads the same results to the same conclusion."""
    catalog = FakeCatalog(refuse=ForeignCapture("not the dive's"))

    with pytest.raises(ApplicationError) as excinfo:
        await _run(
            _activities(catalog).persist_headtail_predictions,
            HeadtailTarget(LAB, uuid.uuid4()),
            [_result(uuid.uuid4())],
        )

    assert (excinfo.value.type, excinfo.value.non_retryable) == (
        "InvalidPredictions",
        True,
    )


async def test_a_worker_status_is_never_persisted():
    """`skipped_no_upgrade_available` is about the worker, not the image: the
    parent drops it, and a result that still carries it is refused rather
    than written over a good row."""
    catalog = FakeCatalog()

    with pytest.raises(ApplicationError, match="skipped_no_upgrade_available"):
        await _run(
            _activities(catalog).persist_headtail_predictions,
            HeadtailTarget(LAB, uuid.uuid4()),
            [_result(uuid.uuid4(), status=HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE)],
        )
    assert not [c for c in catalog.calls if c[0] == "persist"]

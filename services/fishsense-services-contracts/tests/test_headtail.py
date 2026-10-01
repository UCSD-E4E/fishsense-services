"""The head/tail stages' contract: what the orchestrator and processor exchange.

Ported from fishsense-lite@77e8f8e5 libs/fishsense-shared/tests/
test_headtail_geometry.py (which in fact tests `headtail_model_version_tag`),
the tier half of services/fishsense-data-processing-workflow-worker/tests/
test_predict_headtail_image.py (`TestLabelStudioTagFollowsTheRow`,
`TestNoGpuLeavesExistingRowsAlone`) and the version half of services/
fishsense-api/tests/test_headtail_prediction_cohort.py
(`test_a_fallback_row_is_permanently_stale`). Names and reasons are v1's.

v2 changes, each pinned here:

* ids are v2's UUIDs: a capture, not v1's integer image, and laser labels by
  their UUID (the table's foreign key);
* **the processor is handed ObjectRefs**, never a checksum and a folder: only
  the orchestrator issues keys (PLAN.md §9.11), so v1's `jpeg_folder` and
  `output_folder` are gone.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from fishsense_services_contracts.headtail import (
    HEADTAIL_CROP_HEIGHT,
    HEADTAIL_CROP_WIDTH,
    HEADTAIL_FALLBACK_PREDICTOR_VERSION,
    HEADTAIL_PREDICTOR_VERSION,
    HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE,
    HEADTAIL_STATUSES,
    HeadtailPredictionResult,
    PredictHeadtailImage,
    PredictHeadtailImagesInput,
    PreprocessHeadtailImage,
    PreprocessHeadtailImageInput,
    PreprocessHeadtailImagesInput,
    headtail_model_version_tag,
)
from fishsense_services_contracts.object_store import ObjectRef

RAW = ObjectRef(bucket="scratch", key=f"tenants/{uuid4()}/raw/abc.ORF")
JPEG = ObjectRef(
    bucket="labels", key=f"tenants/{uuid4()}/preprocess_headtail_jpeg/abc.JPG"
)
K = [[3000.0, 0.0, 2048.0], [0.0, 3000.0, 1536.0], [0.0, 0.0, 1.0]]
D = [-0.05, 0.01, 0.0, 0.0, 0.0]


class TestModelVersionTagIsAnIdempotencyKey:
    """The backfill keys on `(task_id, model_version)`, so the tag has to be a
    pure function of the stage's behaviour.

    It used to interpolate the checkpoint's pod-local filesystem path, so
    moving the cache directory produced a different key for byte-identical
    output and stacked a duplicate prediction onto every seeded task.
    """

    def test_is_stable_across_calls(self):
        assert headtail_model_version_tag() == headtail_model_version_tag()

    def test_carries_no_filesystem_path(self):
        tag = headtail_model_version_tag()
        assert "/" not in tag
        assert "checkpoint=" not in tag

    def test_names_the_behaviour_version_and_crop(self):
        tag = headtail_model_version_tag()
        assert f"v{HEADTAIL_PREDICTOR_VERSION}" in tag
        assert f"{HEADTAIL_CROP_WIDTH}x{HEADTAIL_CROP_HEIGHT}" in tag

    def test_is_v1s_exact_tag(self):
        """Label Studio already holds v1's predictions under these tags; a
        different spelling would read as "not attached" and stack a second
        prediction on every task the backfill touches."""
        assert headtail_model_version_tag() == "v2 crop=1800x1350"
        assert headtail_model_version_tag(-1) == "v-1 crop=1800x1350"


class TestLabelStudioTagFollowsTheRow:
    """Tagging a fallback prediction as SAM 3.1 makes the later upgrade look
    already-attached, and the labeler keeps the Mask R-CNN keypoints for good."""

    def test_the_two_tiers_get_different_tags(self):
        sam3 = headtail_model_version_tag(HEADTAIL_PREDICTOR_VERSION)
        fallback = headtail_model_version_tag(HEADTAIL_FALLBACK_PREDICTOR_VERSION)

        assert sam3 != fallback
        assert headtail_model_version_tag() == sam3, "default is the current tier"


def test_a_fallback_row_is_permanently_stale():
    """The upgrade queue itself: a fallback-tier row must never read as
    current, whatever `HEADTAIL_PREDICTOR_VERSION` is bumped to."""
    assert HEADTAIL_FALLBACK_PREDICTOR_VERSION != HEADTAIL_PREDICTOR_VERSION
    assert (
        HEADTAIL_FALLBACK_PREDICTOR_VERSION < 0
    ), "negative so it cannot collide with any future forward bump"


def test_the_version_and_crop_are_v1s():
    """A different value would make every migrated prediction stale (or fresh)
    at cutover; these are bumped by hand, in a diff, never by accident."""
    assert HEADTAIL_PREDICTOR_VERSION == 2
    assert (HEADTAIL_CROP_WIDTH, HEADTAIL_CROP_HEIGHT) == (1800, 1350)


def test_the_statuses_a_row_may_carry_are_v1s_and_exclude_the_skip():
    """`skipped_no_upgrade_available` is a statement about the worker, not the
    image: the parent drops it, and persisting it would blank a good row."""
    assert HEADTAIL_STATUSES == (
        "predicted",
        "no_detections",
        "laser_off_all_fish",
        "headtail_failed",
        "decode_failed",
    )
    assert HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE == "skipped_no_upgrade_available"
    assert HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE not in HEADTAIL_STATUSES


class TestNoGpuLeavesExistingRowsAlone:
    """What a GPU-less worker may and may not overwrite, keyed on *whether a
    row exists*, not on which tier produced it."""

    def _payload(self, **kw):
        base = {
            "capture_id": uuid4(),
            "jpeg": JPEG,
            "laser_points": [[10.0, 10.0]],
            "laser_label_ids": [uuid4()],
        }
        base.update(kw)
        return PredictHeadtailImage(**base)

    def test_defaults_mean_first_prediction(self):
        p = self._payload()
        assert p.has_existing_prediction is False
        assert p.existing_laser_superseded is False

    def test_a_superseded_laser_is_worth_redrawing_on_any_backend(self):
        p = self._payload(has_existing_prediction=True, existing_laser_superseded=True)
        assert p.existing_laser_superseded


def test_laser_label_ids_parallel_the_points():
    """`laser_label_ids` names which dot chose the fish; out of step with the
    points, the result would name the wrong label."""
    with pytest.raises(ValidationError, match="parallel"):
        PredictHeadtailImage(
            capture_id=uuid4(),
            jpeg=JPEG,
            laser_points=[[1.0, 2.0], [3.0, 4.0]],
            laser_label_ids=[uuid4()],
        )


def test_the_processor_is_handed_refs_not_folders():
    """v2: the orchestrator resolves where the JPEG is (a migrated frame's v1
    key, or the tenant's); the processor never builds one."""
    image = PreprocessHeadtailImage(
        capture_id=uuid4(), checksum="abc", raw=RAW, jpeg=JPEG
    )
    payload = PreprocessHeadtailImagesInput(
        tenant_id=uuid4(),
        dive_id=uuid4(),
        images=[image],
        camera_matrix=K,
        distortion_coefficients=D,
    )
    per_image = PreprocessHeadtailImageInput(
        raw=RAW, jpeg=JPEG, camera_matrix=K, distortion_coefficients=D
    )
    assert "jpeg_folder" not in PredictHeadtailImagesInput.model_fields
    assert "output_folder" not in PreprocessHeadtailImageInput.model_fields
    assert payload.images[0].jpeg == per_image.jpeg == JPEG


def test_a_result_is_keyed_by_capture_and_names_its_laser_by_uuid():
    laser = uuid4()
    result = HeadtailPredictionResult(
        capture_id=uuid4(), status="predicted", laser_label_id=laser
    )
    assert result.laser_label_id == laser
    assert result.head_x is None and result.predictor_version is None

"""The head/tail predict stage on the processor (no torch needed).

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_predict_headtail_image.py: the `predict_from_jpeg`
kernel behind its `segment(image) -> masks` seam, the mask conversion, the
Mask R-CNN fallback adapter, the no-GPU guard for SAM 3.1 and the fallback
loader. Names, bodies and reasons are v1's. The SAM 3.1 adapter's autocast
tests need torch and live in test_headtail_sam3_adapter.py; the tier and
tag tests moved with the version to the contract package.

What is new here pins the activity itself, which v1 never unit-tested, and
the v2 changes:

* **the weights come through fishsense-core's manifest**: SAM 3.1 has no entry
  in fishsense-core 4.1.0, so the stage builds one from required settings
  (`FISHSENSE_SAM3_SHA256`, `FISHSENSE_SAM3_SIZE`) and fetches through
  `weights.GarageWeightStore`; bytes that aren't the pinned file never load
  (v1's checkpoint cache trusted whatever the key held); **a weights failure
  is final**, one non-retryable type per cause, not a retry per image;
* the JPEG is read from the ref the orchestrator hands over;
* **`core_version` is recorded** (v1 never set it), and `checkpoint` names the
  verified model (`sam3/3.1@<sha256>`), not v1's pod-local cache path;
* ids are UUIDs.
"""

from __future__ import annotations

import hashlib
import sys
import types
import uuid
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path
from typing import List

import boto3
import cv2
import numpy as np
import pytest
from fishsense_core.models import ModelIntegrityError, ModelUnavailable
from moto import mock_aws
from pydantic import ValidationError
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.headtail import (
    HEADTAIL_FALLBACK_PREDICTOR_VERSION,
    HEADTAIL_PREDICTOR_VERSION,
    HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE,
    HeadtailPredictionResult,
    PredictHeadtailImage,
    PredictHeadtailImagesInput,
)
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_processor.headtail_predict import activities as act
from fishsense_services_processor.headtail_predict import predict as sut
from fishsense_services_processor.headtail_predict import weights as sam3_weights
from fishsense_services_processor.headtail_predict.geometry import crop_origin
from fishsense_services_processor.headtail_predict.workflow import (
    PredictHeadtailImagesWorkflow,
)
from fishsense_services_processor.weights import GarageWeightStore

FRAME_W, FRAME_H = 4014, 3016
CROP_W, CROP_H = 1800, 1350
CAPTURE = uuid.uuid4()
JPEG = ObjectRef(
    bucket="labels", key=f"tenants/{uuid.uuid4()}/preprocess_headtail_jpeg/abc.JPG"
)


def _jpeg(width: int = FRAME_W, height: int = FRAME_H) -> bytes:
    frame = np.full((height, width, 3), 40, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", frame)
    assert ok
    return buf.tobytes()


class _Stub:
    """Returns fixed crop-local masks, and records what it was handed."""

    def __init__(self, masks):
        self._masks = masks
        self.seen_shape = None

    def segment(self, image):
        self.seen_shape = image.shape[:2]
        return self._masks


def _fish_mask(cx: int, cy: int, half_len: int = 200, half_h: int = 60):
    """An ellipse, so the head/tail detector has a real principal axis."""
    m = np.zeros((CROP_H, CROP_W), dtype=np.uint8)
    cv2.ellipse(m, (cx, cy), (half_len, half_h), 0, 0, 360, 1, -1)
    return m


def _predict(jpeg, points, stub, **kwargs):
    return sut.predict_from_jpeg(jpeg, points, stub, capture_id=CAPTURE, **kwargs)


# -- the kernel (v1's predict_from_jpeg tests) --------------------------------------


def test_predicts_and_lifts_keypoints_into_frame_coordinates():
    laser = (2000.0, 1500.0)
    ox, oy = crop_origin(*laser, FRAME_W, FRAME_H, CROP_W, CROP_H)
    stub = _Stub([_fish_mask(int(laser[0] - ox), int(laser[1] - oy))])

    result = _predict(_jpeg(), [laser], stub)

    assert result.status == "predicted"
    assert result.capture_id == CAPTURE
    assert stub.seen_shape == (CROP_H, CROP_W), "model must see the crop, not the frame"
    assert (result.crop_x, result.crop_y) == (ox, oy)
    assert result.head_x == pytest.approx(laser[0], abs=260)
    assert result.tail_x == pytest.approx(laser[0], abs=260)
    assert abs(result.head_x - result.tail_x) == pytest.approx(400, abs=40)
    assert result.width == FRAME_W and result.height == FRAME_H


def test_keypoints_are_not_left_in_crop_coordinates():
    """A missing lift puts every keypoint within the crop's own extent, which
    still looks like a fish."""
    laser = (3500.0, 2500.0)
    ox, oy = crop_origin(*laser, FRAME_W, FRAME_H, CROP_W, CROP_H)
    assert ox > 0 and oy > 0, "fixture must use an off-origin crop to be meaningful"
    mask = _fish_mask(int(laser[0] - ox), int(laser[1] - oy))

    result = _predict(_jpeg(), [laser], _Stub([mask]))

    assert result.head_x > CROP_W or result.head_y > CROP_H


def test_laser_on_no_mask_abstains():
    mask = _fish_mask(100, 100, 40, 20)
    result = _predict(_jpeg(), [(2000.0, 1500.0)], _Stub([mask]))
    assert result.status == "laser_off_all_fish"
    assert result.head_x is None


def test_no_masks_abstains_as_no_detections():
    result = _predict(_jpeg(), [(2000.0, 1500.0)], _Stub([]))
    assert result.status == "no_detections"


def test_abstentions_still_carry_the_stage_version():
    """The cohort selects on a version mismatch, so a row without one would be
    re-predicted forever."""
    result = _predict(_jpeg(), [(2000.0, 1500.0)], _Stub([]))
    assert result.predictor_version == HEADTAIL_PREDICTOR_VERSION


def test_gate_picks_the_lasered_fish_not_the_largest():
    laser = (2000.0, 1500.0)
    ox, oy = crop_origin(*laser, FRAME_W, FRAME_H, CROP_W, CROP_H)
    big = _fish_mask(300, 300, 400, 150)
    small = _fish_mask(int(laser[0] - ox), int(laser[1] - oy), 120, 40)

    result = _predict(_jpeg(), [laser], _Stub([big, small]))

    assert result.status == "predicted"
    assert result.mask_area_px == pytest.approx(int(np.count_nonzero(small)), rel=0.01)


def test_records_which_laser_label_chose_the_fish():
    """Provenance: a prediction whose laser is later superseded must be
    selectable as stale."""
    laser_a = (1800.0, 1500.0)
    laser_b = (2300.0, 1500.0)
    ox, oy = crop_origin(*laser_a, FRAME_W, FRAME_H, CROP_W, CROP_H)
    mask = _fish_mask(int(laser_b[0] - ox), int(laser_b[1] - oy), 120, 50)
    assert mask[int(laser_a[1] - oy), int(laser_a[0] - ox)] == 0, "fixture: A must miss"
    assert mask[int(laser_b[1] - oy), int(laser_b[0] - ox)] == 1, "fixture: B must hit"
    a, b = uuid.uuid4(), uuid.uuid4()

    result = _predict(
        _jpeg(), [laser_a, laser_b], _Stub([mask]), laser_label_ids=[a, b]
    )

    assert result.status == "predicted"
    assert result.laser_label_id == b


def test_crop_is_centred_on_the_first_laser_point_only():
    """A second dot outside that window cannot be gated on; in the corpus that
    never happens, which is why cropping on the first point is safe."""
    far = (200.0, 200.0)
    laser = (3800.0, 2800.0)
    ox, oy = crop_origin(far[0], far[1], FRAME_W, FRAME_H, CROP_W, CROP_H)
    assert not (ox <= laser[0] < ox + CROP_W and oy <= laser[1] < oy + CROP_H)

    mask = _fish_mask(CROP_W // 2, CROP_H // 2)
    result = _predict(_jpeg(), [far, laser], _Stub([mask]))

    assert (result.crop_x, result.crop_y) == (ox, oy)


def test_silhouette_ratio_is_recorded():
    laser = (2000.0, 1500.0)
    ox, oy = crop_origin(*laser, FRAME_W, FRAME_H, CROP_W, CROP_H)
    mask = _fish_mask(int(laser[0] - ox), int(laser[1] - oy))

    result = _predict(_jpeg(), [laser], _Stub([mask]))

    assert result.silhouette_ratio is not None
    assert 0.05 < result.silhouette_ratio < 1.0


def test_no_laser_points_abstains():
    result = _predict(_jpeg(), [], _Stub([]))
    assert result.status == "laser_off_all_fish"


def test_undecodable_bytes_abstain_rather_than_raise():
    result = _predict(b"not a jpeg", [(1.0, 1.0)], _Stub([]))
    assert result.status == "decode_failed"


def test_an_unfittable_mask_is_a_headtail_failure_not_an_error(monkeypatch):
    """The detector is a native call with no documented exceptions; one bad
    mask must not fail the whole per-image activity."""

    class _Exploding:
        def find_head_tail_img(self, _mask):
            raise RuntimeError("native detector failed")

    monkeypatch.setitem(
        sys.modules,
        "fishsense_core.fish",
        types.SimpleNamespace(FishHeadTailDetector=_Exploding),
    )
    laser = (2000.0, 1500.0)
    ox, oy = crop_origin(*laser, FRAME_W, FRAME_H, CROP_W, CROP_H)

    result = _predict(
        _jpeg(), [laser], _Stub([_fish_mask(int(laser[0] - ox), int(laser[1] - oy))])
    )

    assert result.status == "headtail_failed"
    assert result.mask_area_px > 0 and result.head_x is None


# -- v2: an abstention names the dot it was made from -------------------------------


class TestAnAbstentionNamesItsDot:
    """v2: v1 left `laser_label_id` NULL on every abstention, so a laser
    correction never made one stale: the frame kept its "no fish" forever
    (the cohort and the GPU-less skip both judge staleness by that dot). A
    kept mask names the dot on it, as a prediction does; with no mask kept,
    the dot the crop was centred on -- the first -- is the one the answer
    came from."""

    LASER = (2000.0, 1500.0)

    def _ids(self):
        return [uuid.uuid4(), uuid.uuid4()]

    def test_no_detections_names_the_crop_centre(self):
        ids = self._ids()
        result = _predict(
            _jpeg(), [self.LASER, (2100.0, 1500.0)], _Stub([]), laser_label_ids=ids
        )
        assert (result.status, result.laser_label_id) == ("no_detections", ids[0])

    def test_laser_off_all_fish_names_the_crop_centre(self):
        ids = self._ids()
        result = _predict(
            _jpeg(),
            [self.LASER, (2100.0, 1500.0)],
            _Stub([_fish_mask(100, 100, 40, 20)]),
            laser_label_ids=ids,
        )
        assert (result.status, result.laser_label_id) == ("laser_off_all_fish", ids[0])

    def test_headtail_failed_names_the_dot_on_the_mask(self, monkeypatch):
        class _Exploding:
            def find_head_tail_img(self, _mask):
                raise RuntimeError("native detector failed")

        monkeypatch.setitem(
            sys.modules,
            "fishsense_core.fish",
            types.SimpleNamespace(FishHeadTailDetector=_Exploding),
        )
        miss, hit = (1800.0, 1500.0), (2300.0, 1500.0)
        ox, oy = crop_origin(*miss, FRAME_W, FRAME_H, CROP_W, CROP_H)
        mask = _fish_mask(int(hit[0] - ox), int(hit[1] - oy), 120, 50)
        ids = self._ids()

        result = _predict(_jpeg(), [miss, hit], _Stub([mask]), laser_label_ids=ids)

        assert (result.status, result.laser_label_id) == ("headtail_failed", ids[1])

    def test_with_no_dot_or_no_frame_there_is_none_to_name(self):
        assert (
            _predict(_jpeg(), [], _Stub([]), laser_label_ids=[]).laser_label_id is None
        )
        assert (
            _predict(
                b"not a jpeg", [self.LASER], _Stub([]), laser_label_ids=self._ids()[:1]
            ).laser_label_id
            is None
        ), "an undecodable JPEG is the file's fault, not the dot's"


class TestTheKeptMasksBox:
    """New in v2 (contract 5): the kept mask's box rides on the result, in
    frame pixels, so the species pre-annotation stage crops exactly the fish
    SAM 3.1 segmented at the dot -- no second SAM pass -- and nothing else."""

    LASER = (3500.0, 2500.0)

    def _mask(self):
        ox, oy = crop_origin(*self.LASER, FRAME_W, FRAME_H, CROP_W, CROP_H)
        assert ox > 0 and oy > 0, "an off-origin crop, or a missing lift passes"
        mask = np.zeros((CROP_H, CROP_W), dtype=np.uint8)
        local_x, local_y = int(self.LASER[0] - ox), int(self.LASER[1] - oy)
        mask[local_y - 40 : local_y + 30, local_x - 200 : local_x + 150] = 1
        box = [local_x - 200 + ox, local_y - 40 + oy, local_x + 150 + ox,
               local_y + 30 + oy]  # fmt: skip
        return mask, box

    def test_a_prediction_carries_its_masks_box_in_frame_pixels(self):
        mask, box = self._mask()

        result = _predict(_jpeg(), [self.LASER], _Stub([mask]))

        assert result.status == "predicted"
        assert result.mask_bbox == box

    def test_the_box_is_the_kept_masks_not_another(self):
        mask, box = self._mask()
        elsewhere = np.zeros_like(mask)
        elsewhere[10:20, 10:20] = 1

        result = _predict(_jpeg(), [self.LASER], _Stub([elsewhere, mask]))

        assert result.mask_bbox == box

    def test_an_unfittable_mask_still_names_its_box(self, monkeypatch):
        """The mask was kept, so the fish is known even when its keypoints
        are not: the species stage can still classify it."""

        class _Exploding:
            def find_head_tail_img(self, _mask):
                raise RuntimeError("native detector failed")

        monkeypatch.setitem(
            sys.modules,
            "fishsense_core.fish",
            types.SimpleNamespace(FishHeadTailDetector=_Exploding),
        )
        mask, box = self._mask()

        result = _predict(_jpeg(), [self.LASER], _Stub([mask]))

        assert (result.status, result.mask_bbox) == ("headtail_failed", box)

    @pytest.mark.parametrize("masks", [[], "off"], ids=["no-masks", "laser-off"])
    def test_no_kept_mask_means_no_box(self, masks):
        if masks == "off":
            masks = [_fish_mask(100, 100, 40, 20)]
        result = _predict(_jpeg(), [self.LASER], _Stub(masks))
        assert result.mask_bbox is None


# -- mask conversion ---------------------------------------------------------------


class TestMaskConversion:
    """SAM3 returns device tensors; `np.asarray` on a CUDA tensor raises."""

    def test_detaches_and_moves_a_device_tensor(self):
        class _DeviceTensor:
            def __init__(self, data):
                self._data = data
                self.detached = False

            def __array__(self, *args, **kwargs):
                raise TypeError("can't convert cuda:0 device type tensor to numpy")

            def detach(self):
                self.detached = True
                return self

            def cpu(self):
                return np.asarray(self._data)

        tensor = _DeviceTensor([[1, 0], [0, 1]])
        out = sut._to_numpy(tensor)  # pylint: disable=protected-access
        assert tensor.detached
        assert out.tolist() == [[1, 0], [0, 1]]

    def test_passes_a_plain_array_through(self):
        assert sut._to_numpy(np.zeros((2, 2))).shape == (
            2,
            2,
        )  # pylint: disable=protected-access


# -- the fallback backend (v1's TestFishialFallbackAdapter) --------------------------


class TestFishialFallbackAdapter:
    class _Recording:
        def __init__(self, labels):
            self._labels = labels
            self.seen = None

        def inference(self, image):
            self.seen = image
            return self._labels

    def _adapter(self, segmentation):
        return sut._FishialAdapter(segmentation)  # pylint: disable=protected-access

    def test_splits_the_instance_label_map_into_binary_masks(self):
        labels = np.zeros((40, 60), dtype=np.int32)
        labels[5:10, 5:15] = 1
        labels[20:25, 30:50] = 7  # ids are arbitrary, not contiguous

        masks = self._adapter(self._Recording(labels)).segment(
            np.zeros((40, 60, 3), dtype=np.uint8)
        )

        assert len(masks) == 2
        assert sorted(int(np.asarray(m).sum()) for m in masks) == [50, 100]
        assert all(np.asarray(m).dtype == bool for m in masks)

    def test_background_is_not_returned_as_a_mask(self):
        labels = np.zeros((20, 40), dtype=np.int32)
        assert (
            self._adapter(self._Recording(labels)).segment(
                np.zeros((20, 40, 3), dtype=np.uint8)
            )
            == []
        )

    def test_the_frame_is_passed_through_as_bgr(self):
        frame = np.zeros((20, 40, 3), dtype=np.uint8)
        frame[:, :, 0] = 255  # blue channel only, in BGR
        recorder = self._Recording(np.zeros((20, 40), dtype=np.int32))

        self._adapter(recorder).segment(frame)

        assert np.array_equal(recorder.seen, frame), "frame was converted"

    def test_a_non_landscape_crop_is_refused_rather_than_silently_empty(self):
        recorder = self._Recording(np.ones((40, 20), dtype=np.int32))

        masks = self._adapter(recorder).segment(np.zeros((40, 20, 3), dtype=np.uint8))

        assert masks == []
        assert recorder.seen is None, "the model should not have been called"

    def test_a_square_crop_is_refused_too(self):
        """`width <= height`: a square is not landscape either."""
        recorder = self._Recording(np.ones((30, 30), dtype=np.int32))

        assert self._adapter(recorder).segment(np.zeros((30, 30, 3), np.uint8)) == []
        assert recorder.seen is None


class TestSam3RequiresAGpu:
    def test_load_segmenter_fails_non_retryably_without_cuda(self, monkeypatch):
        """SAM 3.1 cannot be *built* without a GPU. Left retryable it loops
        holding the pod -- all 356 activities of dive 94 on 2026-09-08."""
        monkeypatch.setattr(sut, "cuda_available", lambda: False)

        with pytest.raises(ApplicationError) as excinfo:
            sut._load_segmenter("/nonexistent.pt")  # pylint: disable=protected-access

        assert excinfo.value.non_retryable is True
        assert excinfo.value.type == "NoGpuForSam3"

    def test_cores_own_gpu_refusal_is_also_non_retryable(self, monkeypatch):
        """v2: the build goes through fishsense-core, whose `Sam3RequiresGpu`
        must not escape as a retryable error either."""
        monkeypatch.setattr(sut, "cuda_available", lambda: True)

        with pytest.raises(ApplicationError) as excinfo:
            sut._load_segmenter("/nonexistent.pt")  # pylint: disable=protected-access

        assert (excinfo.value.type, excinfo.value.non_retryable) == (
            "NoGpuForSam3",
            True,
        )


class TestFallbackSegmenterIsLoaded:
    def test_load_model_is_called_before_the_segmenter_is_published(self, monkeypatch):
        """Unloaded, `inference` raises a retryable ValueError and the
        fallback loops until the child's 6h timeout."""

        class _Segmentation:
            def __init__(self):
                self.loaded = False

            def load_model(self):
                self.loaded = True

            def inference(self, _image):
                if not self.loaded:
                    raise ValueError("model has not been loaded")
                return np.zeros((4, 8), dtype=np.int32)

        built = _Segmentation()
        monkeypatch.setitem(
            sys.modules,
            "fishsense_core.fish",
            types.SimpleNamespace(FishSegmentation=lambda: built),
        )
        monkeypatch.setattr(sut, "_FALLBACK_SEGMENTER", None)

        got = sut.get_fallback_segmenter()

        assert got is built
        assert built.loaded is True, "load_model() was never called"
        assert (
            sut._FishialAdapter(got).segment(
                np.zeros((4, 8, 3), dtype=np.uint8)
            )  # pylint: disable=protected-access
            == []
        )


# -- SAM 3.1's weights through the manifest -----------------------------------------

WEIGHTS = b"pretend these are gigabytes of SAM 3.1"
SAM3_ENV = {
    "FISHSENSE_SAM3_SHA256": hashlib.sha256(WEIGHTS).hexdigest(),
    "FISHSENSE_SAM3_SIZE": str(len(WEIGHTS)),
}


class TestSam3Weights:
    """fishsense-core 4.1.0's manifest has no SAM 3.1 entry, so the stage pins
    one from settings until core does. Nothing unverified loads."""

    @pytest.mark.parametrize("missing", sorted(SAM3_ENV))
    def test_the_hash_and_size_are_required(self, monkeypatch, missing):
        for name, value in SAM3_ENV.items():
            if name != missing:
                monkeypatch.setenv(name, value)
        monkeypatch.delenv(missing, raising=False)

        with pytest.raises(ValidationError):
            sam3_weights.Sam3Settings()

    def test_a_hash_must_look_like_one(self, monkeypatch):
        monkeypatch.setenv("FISHSENSE_SAM3_SHA256", "not-a-hash")
        monkeypatch.setenv("FISHSENSE_SAM3_SIZE", "10")

        with pytest.raises(ValidationError):
            sam3_weights.Sam3Settings()

    def test_the_manifest_pins_v1s_checkpoint(self, monkeypatch):
        """`sam3.1_multiplex.pt`, version 3.1, at v1's
        `model-weights/sam3/3.1/` key -- not `sam3.pt`, which loads just as
        quietly and degrades every mask."""
        for name, value in SAM3_ENV.items():
            monkeypatch.setenv(name, value)

        ref = sam3_weights.sam3_manifest(sam3_weights.Sam3Settings()).resolve("sam3")

        assert (ref.name, ref.version, ref.filename) == (
            "sam3",
            "3.1",
            "sam3.1_multiplex.pt",
        )
        assert (ref.sha256, ref.size) == (hashlib.sha256(WEIGHTS).hexdigest(), 38)

    @pytest.fixture
    def models_bucket(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="model-weights")
            yield s3

    async def test_the_pinned_bytes_are_fetched_and_verified(
        self, monkeypatch, models_bucket, tmp_path
    ):
        for name, value in SAM3_ENV.items():
            monkeypatch.setenv(name, value)
        models_bucket.put_object(
            Bucket="model-weights", Key="sam3/3.1/sam3.1_multiplex.pt", Body=WEIGHTS
        )

        path, model_id = await sam3_weights.fetch_sam3(
            store=GarageWeightStore(models_bucket, "model-weights"),
            cache_dir=tmp_path,
            settings=sam3_weights.Sam3Settings(),
        )

        assert Path(path).read_bytes() == WEIGHTS
        assert model_id.startswith("sam3/3.1@")

    async def test_other_bytes_never_load(self, monkeypatch, models_bucket, tmp_path):
        for name, value in SAM3_ENV.items():
            monkeypatch.setenv(name, value)
        models_bucket.put_object(
            Bucket="model-weights",
            Key="sam3/3.1/sam3.1_multiplex.pt",
            Body=b"tampered " + WEIGHTS[9:],
        )

        with pytest.raises(ModelIntegrityError):
            await sam3_weights.fetch_sam3(
                store=GarageWeightStore(models_bucket, "model-weights"),
                cache_dir=tmp_path,
                settings=sam3_weights.Sam3Settings(),
            )


# -- the activity ---------------------------------------------------------------------


class _FakeStore:
    def __init__(self, jpeg=b""):
        self.jpeg = jpeg
        self.read: list[ObjectRef] = []

    async def download_processed_jpeg(self, ref):
        self.read.append(ref)
        return self.jpeg


def _payload(**overrides):
    values = {
        "capture_id": CAPTURE,
        "jpeg": JPEG,
        "laser_points": [[2000.0, 1500.0]],
        "laser_label_ids": [uuid.uuid4()],
    }
    values.update(overrides)
    return PredictHeadtailImage(**values)


def _activities(store, *, sam3_fetches=None):
    async def fetch():
        if sam3_fetches is not None:
            sam3_fetches.append(1)
        return Path("/cache/sam3/3.1/sam3.1_multiplex.pt"), "sam3/3.1@0123456789ab"

    return act.HeadtailPredictActivities(
        store_factory=lambda: store, sam3_checkpoint=fetch
    )


@pytest.fixture(name="no_gpu")
def _no_gpu(monkeypatch):
    monkeypatch.setattr(sut, "cuda_available", lambda: False)
    monkeypatch.setattr(act, "cuda_available", lambda: False)


@pytest.fixture(name="fallback_stub")
def _fallback_stub(monkeypatch):
    """The fallback backend, stubbed at its loader: no fish found."""
    monkeypatch.setattr(
        act, "get_fallback_segmenter", lambda: types.SimpleNamespace(
            inference=lambda image: np.zeros(image.shape[:2], dtype=np.int32)
        )
    )  # fmt: skip


class TestNoGpuLeavesExistingRowsAlone:
    """A GPU-less worker must not rewrite an existing row (it would be
    identical, or a downgrade) unless its laser was superseded."""

    async def test_an_existing_row_with_a_live_laser_is_left_alone(
        self, no_gpu, fallback_stub
    ):
        store = _FakeStore(_jpeg())

        result = await ActivityEnvironment().run(
            _activities(store).predict_headtail_image,
            _payload(has_existing_prediction=True),
        )

        assert result.status == HEADTAIL_STATUS_NO_UPGRADE_AVAILABLE
        assert result.predictor_version == HEADTAIL_FALLBACK_PREDICTOR_VERSION
        assert result.capture_id == CAPTURE
        assert store.read == [], "nothing to read for a skip"

    async def test_a_superseded_laser_is_redrawn_by_the_fallback(
        self, no_gpu, fallback_stub
    ):
        store = _FakeStore(_jpeg())

        result = await ActivityEnvironment().run(
            _activities(store).predict_headtail_image,
            _payload(has_existing_prediction=True, existing_laser_superseded=True),
        )

        assert result.status == "no_detections"
        assert result.predictor_version == HEADTAIL_FALLBACK_PREDICTOR_VERSION
        assert result.checkpoint == "fishsense_core.fish.FishSegmentation"
        assert store.read == [JPEG], "the JPEG is read from the ref it was handed"

    async def test_a_first_prediction_runs_the_fallback_without_a_gpu(
        self, no_gpu, fallback_stub
    ):
        fetches = []
        result = await ActivityEnvironment().run(
            _activities(
                _FakeStore(_jpeg()), sam3_fetches=fetches
            ).predict_headtail_image,
            _payload(),
        )

        assert result.predictor_version == HEADTAIL_FALLBACK_PREDICTOR_VERSION
        assert not fetches, "no GPU: SAM 3.1's weights are never fetched"


async def test_on_a_gpu_sam3_runs_with_the_verified_checkpoint(monkeypatch):
    monkeypatch.setattr(act, "cuda_available", lambda: True)
    loaded = []

    def fake_get_segmenter(path):
        loaded.append(path)
        return object()

    class _Adapter:
        def __init__(self, processor):
            self.processor = processor

        def segment(self, _image):
            return []

    monkeypatch.setattr(act, "get_segmenter", fake_get_segmenter)
    monkeypatch.setattr(act, "_Sam3Adapter", _Adapter)
    fetches = []

    result = await ActivityEnvironment().run(
        _activities(_FakeStore(_jpeg()), sam3_fetches=fetches).predict_headtail_image,
        _payload(has_existing_prediction=True),
    )

    assert fetches == [1]
    assert loaded == ["/cache/sam3/3.1/sam3.1_multiplex.pt"]
    assert result.predictor_version == HEADTAIL_PREDICTOR_VERSION, "an upgrade"
    assert result.checkpoint == "sam3/3.1@0123456789ab", "the model, not a path"


def _unset_sam3_settings():
    # Raises the ValidationError a pod without FISHSENSE_SAM3_* raises.
    return sam3_weights.Sam3Settings()


class TestAWeightsFailureIsFinal:
    """v2: a SAM 3.1 weights failure is not the image's and does not pass on
    a retry. Left a plain exception, every image of the dive retries without
    limit (the workflow sets no retry policy) until the child's 6 h timeout,
    each attempt re-downloading and re-hashing a multi-GB checkpoint (core
    caches only a verified file). Each is its own type, so the failure says
    which of the four it was."""

    @pytest.mark.parametrize(
        ("failure", "error_type"),
        [
            (_unset_sam3_settings, "Sam3SettingsInvalid"),
            (ModelIntegrityError("sam3/3.1: expected sha256 ..."), "Sam3WeightsCorrupt"),
            (ModelUnavailable("sam3/3.1: not in s3://model-weights"), "Sam3WeightsUnavailable"),
            (KeyError("sam3"), "Sam3NotInManifest"),
        ],
        ids=["settings", "integrity", "unavailable", "manifest"],
    )  # fmt: skip
    async def test_is_non_retryable_and_named(self, monkeypatch, failure, error_type):
        monkeypatch.setattr(act, "cuda_available", lambda: True)
        for name in SAM3_ENV:
            monkeypatch.delenv(name, raising=False)

        async def fetch():
            if callable(failure):
                failure()
            raise failure

        activities = act.HeadtailPredictActivities(
            store_factory=lambda: _FakeStore(_jpeg()), sam3_checkpoint=fetch
        )

        with pytest.raises(ApplicationError) as excinfo:
            await ActivityEnvironment().run(
                activities.predict_headtail_image, _payload()
            )

        assert (excinfo.value.type, excinfo.value.non_retryable) == (error_type, True)


async def test_the_core_version_is_recorded(no_gpu, fallback_stub):
    """v2: v1 never set `core_version`; it answers "why did this frame come
    out that way" months later (PLAN.md §4.3's Prediction provenance)."""
    result = await ActivityEnvironment().run(
        _activities(_FakeStore(_jpeg())).predict_headtail_image, _payload()
    )
    assert result.core_version == version("fishsense-core")


# -- the workflow ---------------------------------------------------------------------


async def test_workflow_fans_out_one_prediction_per_image_and_returns_them():
    """v1's shape: one activity per image, 15 minutes start-to-close (a cold
    pod pays the weight fetch and the SAM load on its first image)."""
    seen: List[tuple] = []

    @activity.defn(name="predict_headtail_image")
    async def stub(image: PredictHeadtailImage) -> HeadtailPredictionResult:
        seen.append((image.capture_id, activity.info().start_to_close_timeout))
        return HeadtailPredictionResult(capture_id=image.capture_id, status="predicted")

    captures = [uuid.uuid4(), uuid.uuid4()]
    payload = PredictHeadtailImagesInput(
        tenant_id=uuid.uuid4(),
        dive_id=uuid.uuid4(),
        images=[_payload(capture_id=c) for c in captures],
    )
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="test-predict-headtail",
            workflows=[PredictHeadtailImagesWorkflow],
            activities=[stub],
        ):
            results = await env.client.execute_workflow(
                PredictHeadtailImagesWorkflow.run,
                payload,
                id=f"test-predict-headtail-{uuid.uuid4()}",
                task_queue="test-predict-headtail",
                result_type=List[HeadtailPredictionResult],
            )

    assert [r.capture_id for r in results] == captures
    assert sorted(seen) == sorted((c, timedelta(minutes=15)) for c in captures)

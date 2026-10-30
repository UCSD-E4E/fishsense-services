# pylint: disable=protected-access
"""The model-assisted laser predict activity (GPU role).

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_predict_laser_image.py and
test_predict_laser_images_workflow.py. Names, bodies and reasons are v1's;
the detector and `LinearRawImage` are faked at the seams, as in v1. v2
changes, pinned here:

* **the weights come from Garage `model-weights`**, through fishsense-core's
  `LaserDetector.from_store` and the processor's `GarageWeightStore`, verified
  against core's manifest (PLAN.md §9.12). v1 baked the checkpoint into its
  image from Hugging Face and read a path;
* the raw frame is handed over as an `ObjectRef` and streamed to a file (the
  processor never builds a key); the result is keyed by capture id;
* the checkpoint recorded is the detector's canonical name, which core
  resolves by content (v1: the basename of the baked path).
"""

from __future__ import annotations

import sys
import threading
import time
import types
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import List

import numpy as np
import pytest
from temporalio import activity
from fishsense_services_contracts.temporal import DATA_CONVERTER
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.laser import (
    LASER_PREDICTOR_VERSION,
    LaserPredictImage,
    LaserPredictionResult,
    PredictLaserImagesInput,
)
from fishsense_services_contracts.laser_region import LASER_REGION_POLYGON
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_processor.laser_predict import activities as sut
from fishsense_services_processor.laser_predict.workflow import (
    PredictLaserImageInput,
    PredictLaserImagesWorkflow,
)

RAW = ObjectRef(bucket="scratch", key=f"tenants/{uuid.uuid4()}/raw/abc123.ORF")
_K = [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, 1.0]]
_D = [0.0, 0.0, 0.0, 0.0, 0.0]


@pytest.fixture(autouse=True)
def _reset_detector_cache():
    """Each test starts with a cold module-level detector cache."""
    sut._DETECTOR = None
    yield
    sut._DETECTOR = None


def _install_fake_linear_raw_image(monkeypatch, captured):
    mod = types.ModuleType("fishsense_core.image.linear_raw_image")

    class _LinearRawImage:  # pylint: disable=too-few-public-methods
        def __init__(self, source, *, bayer_upsample="repeat"):
            captured["source"] = source
            captured["bayer_upsample"] = bayer_upsample
            self.data = np.full((300, 400, 3), 100, dtype=np.uint16)

    mod.LinearRawImage = _LinearRawImage
    monkeypatch.setitem(sys.modules, "fishsense_core.image.linear_raw_image", mod)


# ----------------------------- _predict_from_raw -----------------------------


def test_predict_from_raw_predicts_in_sensor_pixels_and_rectifies_once(monkeypatch):
    """The detector answers in sensor pixels (its default), where the frame
    is; the stored point is rectified once, with core's `rectify_prediction`,
    the call `predict(rectify_output=True)` makes itself (review of #352)."""
    from fishsense_core.laser import (  # pylint: disable=import-outside-toplevel
        rectify_prediction,
    )

    captured: dict = {}
    _install_fake_linear_raw_image(monkeypatch, captured)
    predict_calls: dict = {}

    class _FakeDetector:  # pylint: disable=too-few-public-methods
        checkpoint_name = "run3_epoch_021.pt"

        def predict(self, image, **kwargs):
            predict_calls["image"] = image
            predict_calls["kwargs"] = kwargs
            return SimpleNamespace(x=12.5, y=34.0, confidence=0.9)

    monkeypatch.setattr(sut, "_get_detector", _FakeDetector)

    pred, width, height, color, margin, checkpoint = sut._predict_from_raw(
        Path("/scratch/abc123.ORF"),
        camera_matrix=_K,
        distortion_coefficients=[0.1, -0.2, 0.0, 0.0, 0.0],
        wavelength="red",
    )

    assert (pred.x, pred.y) == rectify_prediction(
        12.5, 34.0, np.array(_K), np.array([0.1, -0.2, 0.0, 0.0, 0.0])
    )
    assert pred.confidence == 0.9
    assert (width, height) == (400, 300)  # from image.data.shape (H, W)
    assert (color, margin) in ((None, None), (None, 0.0))
    assert checkpoint == "run3_epoch_021.pt"
    assert captured["source"] == Path("/scratch/abc123.ORF")
    assert predict_calls["kwargs"] == {"wavelength": "red"}


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_non_finite_dot_is_a_non_detection(monkeypatch, bad):
    """A NaN passes `is not None`: v0.48.0's color code raised on it, and it
    would otherwise be rectified, gated and stored. It is no dot (review of
    #352)."""
    _install_fake_linear_raw_image(monkeypatch, {})
    monkeypatch.setattr(sut, "_get_detector", _detector_at(bad, 34.0))

    pred, _, _, color, margin, _ = sut._predict_from_raw(
        Path("/scratch/abc123.ORF"), _K, _D, None
    )

    assert (pred.x, pred.y, color, margin) == (None, None, None, None)
    assert pred.confidence == 0.9


def test_importing_the_activity_loads_no_detector_module():
    """The color and rectification imports are lazy: importing the stage
    registry must not load core's detector module (cv2, the weights
    manifest) on roles that never predict (review of #352)."""
    import subprocess  # pylint: disable=import-outside-toplevel

    code = (
        "import sys; import fishsense_services_processor.laser_predict.activities; "
        "print('fishsense_core._laser_detector' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"


# ------------------------------- _get_detector -------------------------------


def test_get_detector_loads_once_and_caches(monkeypatch):
    loads: list[int] = []

    def _fake_load():
        loads.append(1)
        return SimpleNamespace(name="detector")

    monkeypatch.setattr(sut, "_load_detector", _fake_load)

    assert sut._get_detector() is sut._get_detector()
    assert loads == [1]


def test_get_detector_loads_once_under_concurrency(monkeypatch):
    """On a cold pod the first batch of activities enters the lazy init
    together; unguarded, each loads its own copy of the checkpoint onto the
    GPU. The second caller is released only once the first is inside the load."""
    loads: list[int] = []
    loading_started = threading.Event()

    def _slow_load():
        loading_started.set()
        time.sleep(0.2)
        loads.append(1)
        return SimpleNamespace(name="detector")

    monkeypatch.setattr(sut, "_load_detector", _slow_load)

    def _late_caller():
        assert loading_started.wait(timeout=5)
        return sut._get_detector()

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(sut._get_detector)
        late = [pool.submit(_late_caller) for _ in range(2)]
        results = [first.result(timeout=10)] + [f.result(timeout=10) for f in late]

    assert loads == [1], f"checkpoint loaded {len(loads)}x, want 1"
    assert all(r is results[0] for r in results)


def test_the_detector_loads_through_the_garage_weight_store(monkeypatch, tmp_path):
    """v2: core's `from_store`, over Garage `model-weights`, into the pod's
    cache -- so every load is checked against core's pinned sha256."""
    monkeypatch.setenv("FISHSENSE_MODEL_WEIGHTS_ENDPOINT_URL", "https://s3.test")
    monkeypatch.setenv("FISHSENSE_MODEL_WEIGHTS_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("FISHSENSE_MODEL_WEIGHTS_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("FISHSENSE_MODEL_WEIGHTS_CACHE_DIR", str(tmp_path))
    seen: dict = {}

    class _FakeDetector:
        @classmethod
        def from_store(cls, store, *, cache_dir, **kwargs):
            seen.update(store=store, cache_dir=cache_dir, kwargs=kwargs)
            return cls()

    fake = types.ModuleType("fishsense_core.laser")
    fake.LaserDetector = _FakeDetector
    monkeypatch.setitem(sys.modules, "fishsense_core.laser", fake)

    detector = sut._load_detector()

    assert isinstance(detector, _FakeDetector)
    assert seen["store"].bucket == "model-weights"
    assert Path(seen["cache_dir"]) == tmp_path


# --------------------------------- activity ----------------------------------


def _payload(**overrides) -> PredictLaserImageInput:
    base = {
        "capture_id": uuid.UUID(int=42),
        "raw": RAW,
        "camera_matrix": _K,
        "distortion_coefficients": _D,
        "wavelength": None,
    }
    base.update(overrides)
    return PredictLaserImageInput(**base)


class _Store:
    def __init__(self):
        self.downloads: list[ObjectRef] = []

    async def download_raw(self, ref, directory):
        self.downloads.append(ref)
        path = Path(directory) / ref.key.rsplit("/", 1)[-1]
        path.write_bytes(b"ORFBYTES")
        return path


@pytest.fixture(name="store")
def _store(monkeypatch):
    store = _Store()
    monkeypatch.setattr(sut, "_object_store", lambda: store)
    return store


def _fixed_prediction(x, y, confidence=0.9, color="red", margin=30.0):
    def _predict(path, *_args, **_kwargs):
        assert Path(path).read_bytes() == b"ORFBYTES"
        return (
            SimpleNamespace(x=x, y=y, confidence=confidence),
            4000,
            3000,
            color,
            margin,
            "run3_epoch_021.pt",
        )

    return _predict


async def test_activity_returns_mapped_prediction(monkeypatch, store):
    monkeypatch.setattr(
        sut, "_predict_from_raw", _fixed_prediction(100.0, 200.0, 0.77, "green", -42.0)
    )

    result = await ActivityEnvironment().run(
        sut.predict_laser_image, _payload(capture_id=uuid.UUID(int=7))
    )

    assert isinstance(result, LaserPredictionResult)
    assert result.capture_id == uuid.UUID(int=7)
    assert (result.x, result.y, result.confidence) == (100.0, 200.0, 0.77)
    assert (result.width, result.height) == (4000, 3000)
    assert (result.color, result.color_margin) == ("green", -42.0)
    assert result.rejected_out_of_region is False
    assert result.predictor_version == LASER_PREDICTOR_VERSION
    assert result.checkpoint == "run3_epoch_021.pt"
    assert store.downloads == [RAW]


async def test_activity_handles_non_detection(monkeypatch, store):
    monkeypatch.setattr(
        sut, "_predict_from_raw", _fixed_prediction(None, None, 0.05, None, None)
    )

    result = await ActivityEnvironment().run(sut.predict_laser_image, _payload())

    assert result.x is None and result.y is None
    assert result.confidence == 0.05


async def test_activity_validates_dict_payload(monkeypatch, store):
    """Temporal can hand the activity a plain dict across the boundary."""
    monkeypatch.setattr(sut, "_predict_from_raw", _fixed_prediction(1.0, 2.0))

    result = await ActivityEnvironment().run(
        sut.predict_laser_image, _payload().model_dump(mode="json")
    )

    assert (result.x, result.y) == (1.0, 2.0)


# --------------------------- expected-region gate ----------------------------

_IN_REGION = (2000.0, 1200.0)
_OUT_OF_REGION = (1600.0, 1800.0)  # inside the bbox, in a corner the polygon cuts


async def test_prediction_inside_the_region_is_kept(monkeypatch, store):
    monkeypatch.setattr(sut, "_predict_from_raw", _fixed_prediction(*_IN_REGION))

    result = await ActivityEnvironment().run(
        sut.predict_laser_image, _payload(laser_region=LASER_REGION_POLYGON)
    )

    assert (result.x, result.y) == _IN_REGION
    assert result.rejected_out_of_region is False


async def test_prediction_outside_the_region_is_dropped(monkeypatch, store):
    monkeypatch.setattr(sut, "_predict_from_raw", _fixed_prediction(*_OUT_OF_REGION))

    result = await ActivityEnvironment().run(
        sut.predict_laser_image, _payload(laser_region=LASER_REGION_POLYGON)
    )

    assert result.x is None and result.y is None
    assert result.rejected_out_of_region is True
    # The model *was* confident, about a point we do not believe.
    assert result.confidence == 0.9


async def test_rejection_is_distinguishable_from_a_non_detection(monkeypatch, store):
    monkeypatch.setattr(
        sut, "_predict_from_raw", _fixed_prediction(None, None, 0.02, None, None)
    )

    result = await ActivityEnvironment().run(
        sut.predict_laser_image,
        _payload(laser_region=[[0, 0], [10, 0], [10, 10], [0, 10]]),
    )

    assert result.x is None and result.rejected_out_of_region is False


async def test_no_region_disables_the_gate(monkeypatch, store):
    monkeypatch.setattr(sut, "_predict_from_raw", _fixed_prediction(*_OUT_OF_REGION))

    result = await ActivityEnvironment().run(
        sut.predict_laser_image, _payload(laser_region=None)
    )

    assert (result.x, result.y) == _OUT_OF_REGION
    assert result.rejected_out_of_region is False


# --------------------------------- workflow ----------------------------------


async def _run_workflow(payload, stub):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(
            env.client,
            task_queue="test-predict",
            workflows=[PredictLaserImagesWorkflow],
            activities=[stub],
        ):
            return await env.client.execute_workflow(
                PredictLaserImagesWorkflow.run,
                payload,
                id=f"test-predict-{uuid.uuid4()}",
                task_queue="test-predict",
            )


async def test_workflow_fans_out_and_returns_one_prediction_per_image():
    calls: List[PredictLaserImageInput] = []

    @activity.defn(name="predict_laser_image")
    async def stub(payload: PredictLaserImageInput) -> LaserPredictionResult:
        calls.append(payload)
        n = float(payload.capture_id.int)
        return LaserPredictionResult(
            capture_id=payload.capture_id,
            x=n,
            y=n * 2,
            confidence=0.9,
            predictor_version=LASER_PREDICTOR_VERSION,
        )

    images = [
        LaserPredictImage(capture_id=uuid.UUID(int=i), raw=RAW) for i in (1, 2, 3)
    ]
    results = await _run_workflow(
        PredictLaserImagesInput(
            mission_id=uuid.uuid4(),
            images=images,
            camera_matrix=_K,
            distortion_coefficients=[-0.1, 0.05, 0.0, 0.0, 0.0],
            wavelength="red",
            laser_region=LASER_REGION_POLYGON,
        ),
        stub,
    )

    assert {c.capture_id for c in calls} == {i.capture_id for i in images}
    for c in calls:
        assert c.camera_matrix == _K
        assert c.wavelength == "red"
        assert c.laser_region == LASER_REGION_POLYGON
    assert {r.capture_id for r in results} == {i.capture_id for i in images}


async def test_workflow_with_no_images_returns_empty_and_calls_nothing():
    calls: list = []

    @activity.defn(name="predict_laser_image")
    async def stub(payload: PredictLaserImageInput) -> LaserPredictionResult:
        calls.append(payload)
        raise AssertionError("no image, no call")

    results = await _run_workflow(
        PredictLaserImagesInput(
            mission_id=uuid.uuid4(),
            images=[],
            camera_matrix=_K,
            distortion_coefficients=_D,
        ),
        stub,
    )

    assert not calls
    assert results == []


# ------------------------- color at the call site ---------------------------
#
# The color rule itself is fishsense-core's (`fishsense_core.laser_color`,
# core #94) and is tested there. What is pinned here is how this activity
# calls it: on the uint16 `LinearRawImage`, at the detector's own sensor
# point, before the point is rectified for storage.

_DISTORTED = [-0.28, 0.12, 0.0005, -0.0003, -0.03]
_K_FULL = [[2833.0, 0.0, 2010.0], [0.0, 2858.0, 1450.0], [0.0, 0.0, 1.0]]


def _install_linear_raw_image_with(monkeypatch, data):
    mod = types.ModuleType("fishsense_core.image.linear_raw_image")

    class _LinearRawImage:  # pylint: disable=too-few-public-methods
        def __init__(self, source, *, bayer_upsample="repeat"):
            self.data = data

    mod.LinearRawImage = _LinearRawImage
    monkeypatch.setitem(sys.modules, "fishsense_core.image.linear_raw_image", mod)


def _detector_at(x, y):
    class _FakeDetector:  # pylint: disable=too-few-public-methods
        checkpoint_name = "run3_epoch_021.pt"

        def predict(self, image, **kwargs):
            return SimpleNamespace(x=x, y=y, confidence=0.9)

    return _FakeDetector


def _paint_dot(img, x, y, bgr, radius):
    ys, xs = np.ogrid[: img.shape[0], : img.shape[1]]
    img[((xs - x) ** 2 + (ys - y) ** 2) <= radius**2] = bgr
    return img


@pytest.mark.parametrize(
    "sensor", [(1580.0, 395.0), (2010.0, 1450.0), (300.0, 200.0), (3700.0, 2700.0)]
)
def test_color_is_read_where_the_detector_found_the_dot(monkeypatch, sensor):
    """Near the laser region rectified and sensor pixels are ~48 px apart, so
    sampling the frame at the rectified point misses the dot and reads the
    blue-green scene. The color is read at the detector's sensor point, with
    no inverse map, and only the stored point is rectified: across the frame,
    corners included."""
    import cv2  # pylint: disable=import-outside-toplevel

    rectified = cv2.undistortPoints(
        np.array([[sensor]], dtype=np.float32),
        np.array(_K_FULL, dtype=np.float32),
        np.array(_DISTORTED, dtype=np.float32),
        P=np.array(_K_FULL, dtype=np.float32),
    ).reshape(2)
    frame = np.full((3000, 4000, 3), (400, 800, 120), dtype=np.uint16)
    _paint_dot(frame, *sensor, (200, 300, 4000), radius=4)
    _install_linear_raw_image_with(monkeypatch, frame)
    monkeypatch.setattr(sut, "_get_detector", _detector_at(*sensor))

    pred, _, _, color, margin, _ = sut._predict_from_raw(
        Path("/scratch/abc123.ORF"), _K_FULL, _DISTORTED, None
    )

    assert color == "red" and margin > 0
    assert (pred.x, pred.y) == (float(rectified[0]), float(rectified[1]))


#: The saturated dot's margin under core 4.3.0's rule (v2's was -3.1).
MARGIN_V3 = -680.0 / 3.0


def test_a_saturated_dot_votes_from_its_halo(monkeypatch):
    """fishsense-core 4.3.0 (#94): a blown-out core no longer sets the margin's
    scale. Before, a 16-bit frame's 65535 core shrank the halo's margin ~70x
    here (-800 levels -> -3.1, under the 4.0 threshold), so a close green dot
    abstained; scaled by the brightest *usable* pixel it votes green."""
    frame = np.full((400, 400, 3), (90, 70, 60), dtype=np.uint16)
    _paint_dot(frame, 200, 200, (100, 900, 100), radius=7)  # green halo
    _paint_dot(frame, 200, 200, (65535, 65535, 65535), radius=3)  # blown core
    _install_linear_raw_image_with(monkeypatch, frame)
    monkeypatch.setattr(sut, "_get_detector", _detector_at(200.0, 200.0))

    _, _, _, color, margin, _ = sut._predict_from_raw(
        Path("/scratch/abc123.ORF"), _K, _D, None
    )

    assert color == "green" and margin < -4.0
    # The magnitude is what `laser_predictions.color_margin` stores, under
    # LASER_PREDICTOR_VERSION 3: a change in core's scaling must show here.
    assert margin == pytest.approx(MARGIN_V3, rel=1e-6)


def test_malformed_intrinsics_fail_before_the_detector_runs(monkeypatch):
    """The dot is rectified after inference, so a camera calibration whose
    distortion cannot rectify failed only after the GPU had run the frame,
    on every retry (review of #361). The same rectification runs on a dummy
    point first."""
    _install_fake_linear_raw_image(monkeypatch, {})
    calls = []

    class _Detector:  # pylint: disable=too-few-public-methods
        checkpoint_name = "run3_epoch_021.pt"

        def predict(self, image, **kwargs):
            calls.append(1)
            return SimpleNamespace(x=10.0, y=10.0, confidence=0.9)

    monkeypatch.setattr(sut, "_get_detector", _Detector)

    with pytest.raises(Exception):
        sut._predict_from_raw(Path("/scratch/abc123.ORF"), _K, [0.1, 0.2, 0.3], None)
    assert calls == []

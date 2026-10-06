"""The slate detector on the processor: the weights, the frame, the activity,
the workflow and the role.

New in v2. The model is 2026-10-03_slate_detector@95a77d95's presence
classifier (EfficientNet-B0 + GeM, runs/final-q1); see
`fishsense_services_contracts.slate_presence`. Built the way the head/tail
and species predict stages are, for the same reasons:

* **the weights come through fishsense-core's manifest**: the detector has no
  entry in fishsense-core 4.1.0's, so the stage pins its own from required
  settings (`FISHSENSE_SLATE_DETECTOR_SHA256`/`_SIZE`) at
  `model-weights/slate-detector/q1/slate_efficientnet_b0.pt`, and fetches
  through `weights.GarageWeightStore`; bytes that aren't the pinned file
  never load;
* **a weights, settings or checkpoint failure is final**: one non-retryable
  type per cause, not a retry per frame;
* **the frame is the one the model was trained on** (the source repo's
  render.py): fishsense-core's `RawImage` with `DecodeConfig.production()`,
  then `RectifiedImage` with the dive's intrinsics, RGB, shrunk to 1600 px on
  the long side and round-tripped through a quality-95 JPEG, as its frame
  cache was. The resize to 1024x768 is the model's (test_slate_detect_model);
* a raw that won't decode is an abstention (`decode_failed`), recorded so the
  cohort moves on; the raw is read from the ref the orchestrator hands over;
* the stage runs on the GPU role, where torch is installed.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import List

import boto3
import numpy as np
import pytest
import rawpy
from fishsense_core.image.decode import DecodeConfig
from fishsense_core.models import ModelIntegrityError, ModelUnavailable
from moto import mock_aws
from PIL import Image
from pydantic import ValidationError
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.slate_presence import (
    SLATE_DETECTOR_VERSION,
    DetectSlateImage,
    DetectSlateImageInput,
    DetectSlateImagesInput,
    SlatePresenceResult,
)
from fishsense_services_processor import registry
from fishsense_services_processor.slate_detect import activities as act
from fishsense_services_processor.slate_detect import frame as frames
from fishsense_services_processor.slate_detect import weights as sut
from fishsense_services_processor.slate_detect.workflow import (
    DetectSlatePresenceWorkflow,
)
from fishsense_services_processor.weights import GarageWeightStore, model_key

RAW = ObjectRef(bucket="scratch", key=f"tenants/{uuid.uuid4()}/raw/abc123.ORF")
K = [[3500.0, 0.0, 2000.0], [0.0, 3500.0, 1500.0], [0.0, 0.0, 1.0]]
D = [-0.1, 0.05, 0.0, 0.0, 0.0]

WEIGHTS = b"pretend these are 16 MB of EfficientNet-B0"
SHA = hashlib.sha256(WEIGHTS).hexdigest()
ENV = {
    "FISHSENSE_SLATE_DETECTOR_SHA256": SHA,
    "FISHSENSE_SLATE_DETECTOR_SIZE": str(len(WEIGHTS)),
}


@pytest.fixture(name="pinned")
def _pinned(monkeypatch):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)


# -- the weights ------------------------------------------------------------------------


class TestTheSettings:
    @pytest.mark.parametrize("missing", sorted(ENV))
    def test_the_hash_and_size_are_required(self, monkeypatch, missing):
        for name, value in ENV.items():
            if name != missing:
                monkeypatch.setenv(name, value)
        monkeypatch.delenv(missing, raising=False)

        with pytest.raises(ValidationError):
            sut.SlateDetectorSettings()

    def test_a_hash_must_look_like_one(self, pinned, monkeypatch):
        monkeypatch.setenv("FISHSENSE_SLATE_DETECTOR_SHA256", "not-a-hash")
        with pytest.raises(ValidationError):
            sut.SlateDetectorSettings()

    def test_a_size_must_be_positive(self, pinned, monkeypatch):
        monkeypatch.setenv("FISHSENSE_SLATE_DETECTOR_SIZE", "0")
        with pytest.raises(ValidationError):
            sut.SlateDetectorSettings()

    def test_the_hash_is_normalised(self, pinned, monkeypatch):
        monkeypatch.setenv("FISHSENSE_SLATE_DETECTOR_SHA256", f" {SHA.upper()} ")
        assert sut.SlateDetectorSettings().sha256 == SHA


def test_the_manifest_pins_the_q1_checkpoint(pinned):
    """`model-weights/slate-detector/q1/slate_efficientnet_b0.pt`: the version
    is in the key, so new weights are a new object."""
    ref = sut.slate_detector_manifest(sut.SlateDetectorSettings()).resolve(
        "slate-detector"
    )

    assert (ref.version, ref.filename, ref.sha256, ref.size) == (
        "q1",
        "slate_efficientnet_b0.pt",
        SHA,
        len(WEIGHTS),
    )
    assert model_key(ref.name, ref.version, ref.filename) == (
        "slate-detector/q1/slate_efficientnet_b0.pt"
    )


@pytest.fixture(name="models_bucket")
def _models_bucket(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="model-weights")
        yield s3


async def _fetch(models_bucket, tmp_path):
    return await sut.fetch_slate_detector(
        store=GarageWeightStore(models_bucket, "model-weights"),
        cache_dir=tmp_path,
        settings=sut.SlateDetectorSettings(),
    )


class TestTheFetch:
    async def test_the_pinned_bytes_and_their_sha256(
        self, pinned, models_bucket, tmp_path
    ):
        models_bucket.put_object(
            Bucket="model-weights",
            Key="slate-detector/q1/slate_efficientnet_b0.pt",
            Body=WEIGHTS,
        )

        path, sha256 = await _fetch(models_bucket, tmp_path)

        assert path.read_bytes() == WEIGHTS
        assert sha256 == SHA

    async def test_other_bytes_never_load(self, pinned, models_bucket, tmp_path):
        models_bucket.put_object(
            Bucket="model-weights",
            Key="slate-detector/q1/slate_efficientnet_b0.pt",
            Body=b"tampered " + WEIGHTS[9:],
        )
        with pytest.raises(ModelIntegrityError):
            await _fetch(models_bucket, tmp_path)

    async def test_missing_weights_are_unavailable(
        self, pinned, models_bucket, tmp_path
    ):
        with pytest.raises(ModelUnavailable):
            await _fetch(models_bucket, tmp_path)


# -- the frame --------------------------------------------------------------------------


@pytest.fixture(name="decoded")
def _decoded(monkeypatch):
    """fishsense-core's decode and rectification, faked at the seams: a BGR
    frame of the camera's 4014x3016 whose left half is blue."""
    seen = {}

    class _RawImage:  # pylint: disable=too-few-public-methods
        def __init__(self, source, *, config=None):
            seen["source"], seen["config"] = source, config

    class _RectifiedImage:  # pylint: disable=too-few-public-methods
        def __init__(self, image, intrinsics):
            seen["image"], seen["intrinsics"] = image, intrinsics
            data = np.zeros((3016, 4014, 3), dtype=np.uint8)
            data[:, :2007, 0] = 255  # blue, in BGR
            self.data = data

    monkeypatch.setattr(frames, "RawImage", _RawImage)
    monkeypatch.setattr(frames, "RectifiedImage", _RectifiedImage)
    return seen


def test_the_frame_is_decoded_as_the_model_was_trained(decoded, tmp_path):
    """The source repo's render.py: `DecodeConfig.production()`, rectified
    with the dive's intrinsics."""
    raw = tmp_path / "frame.ORF"

    frames.render_frame(raw, K, D)

    assert decoded["source"] == raw
    assert decoded["config"] == DecodeConfig.production()
    assert isinstance(decoded["image"], frames.RawImage)
    np.testing.assert_array_equal(decoded["intrinsics"].camera_matrix, K)
    np.testing.assert_array_equal(decoded["intrinsics"].distortion_coefficients, D)


def test_the_frame_is_rgb_at_the_training_caches_size(decoded, tmp_path):
    """1600 px on the long side (Lanczos), through a quality-95 JPEG: the
    frames the model trained on were read back from that cache."""
    image = frames.render_frame(tmp_path / "frame.ORF", K, D)

    assert (image.mode, image.size) == ("RGB", (1600, 1202))
    left, right = image.getpixel((100, 600)), image.getpixel((1500, 600))
    assert left[2] > 240 and left[0] < 15, "blue stays blue: BGR became RGB"
    assert max(right) < 15


def test_the_jpeg_round_trip_is_the_caches():
    rgb = np.random.default_rng(0).integers(0, 255, (300, 400, 3), dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format="JPEG", quality=95)
    expected = np.asarray(Image.open(io.BytesIO(buffer.getvalue())).convert("RGB"))

    image = frames.as_training_frame(np.ascontiguousarray(rgb[:, :, ::-1]))

    np.testing.assert_array_equal(np.asarray(image), expected)


def test_a_libraw_error_is_a_decode_failure():
    assert issubclass(rawpy.LibRawFileUnsupportedError, frames.DECODE_ERRORS)


# -- the activity -----------------------------------------------------------------------


class _FakeStore:
    def __init__(self):
        self.read: list[ObjectRef] = []

    async def download_raw(self, ref, directory):
        self.read.append(ref)
        path = Path(directory) / "abc123.ORF"
        path.write_bytes(b"raw")
        return path


class _Classifier:  # pylint: disable=too-few-public-methods
    def __init__(self, probability=0.93):
        self.p = probability
        self.seen = []

    def probability(self, image):
        self.seen.append(image)
        return self.p


def _activities(store=None, *, classifier=None, fetch=None, render=None, loads=None,
                refuse=None):  # fmt: skip
    classifier = classifier or _Classifier()

    async def weights():
        if fetch is not None:
            return await fetch()
        return Path("/cache/slate-detector/q1/slate_efficientnet_b0.pt"), SHA

    def load(path, sha256):
        if loads is not None:
            loads.append((str(path), sha256))
        if refuse is not None:
            raise act.CheckpointInvalid(refuse)
        return classifier

    def _render(raw, matrix, distortion):
        return Image.new("RGB", (1600, 1202))

    return act.SlateDetectActivities(
        store_factory=lambda: store or _FakeStore(),
        weights=weights,
        load_classifier=load,
        render=render or _render,
    )


def _payload(capture_id=None):
    return DetectSlateImageInput(
        image=DetectSlateImage(capture_id=capture_id or uuid.uuid4(), raw=RAW),
        camera_matrix=K,
        distortion_coefficients=D,
    )


async def _run(activities, payload):
    return await ActivityEnvironment().run(activities.detect_slate_presence, payload)


async def test_a_frame_gets_its_probability_and_provenance():
    store, classifier, rendered = _FakeStore(), _Classifier(0.93), []

    def render(raw, matrix, distortion):
        rendered.append((Path(raw).read_bytes(), matrix, distortion))
        return Image.new("RGB", (1600, 1202))

    payload = _payload()
    result = await _run(
        _activities(store, classifier=classifier, render=render), payload
    )

    assert store.read == [RAW], "the raw is read from the ref it was handed"
    assert rendered == [(b"raw", K, D)]
    assert len(classifier.seen) == 1
    assert (result.capture_id, result.status, result.probability) == (
        payload.image.capture_id,
        "predicted",
        0.93,
    )
    assert (result.model_name, result.model_version, result.weights_sha256) == (
        "slate-detector",
        SLATE_DETECTOR_VERSION,
        SHA,
    )


async def test_a_result_says_how_it_was_made():
    """Publication-grade: fishsense-core's and the processor's installed
    versions, the render as `frame.render_settings()` describes it, and when
    the frame was scored."""
    before = datetime.now(UTC)
    result = await _run(_activities(), _payload())

    assert result.core_version == version("fishsense-core")
    assert result.processor_version == version("fishsense-services-processor")
    assert result.render == frames.render_settings()
    assert before <= result.predicted_at <= datetime.now(UTC)


def test_the_render_settings_are_the_production_decode_at_1024_by_768():
    render = frames.render_settings()

    assert (render.decode_config, render.rectified) == ("production", True)
    assert (render.input_width, render.input_height) == (1024, 768)
    assert (render.cache_long_side, render.jpeg_quality, render.tta) == (
        frames.CACHE_LONG_SIDE,
        frames.JPEG_QUALITY,
        "hflip",
    )
    production = DecodeConfig.production()
    assert render.decode_params["clahe_enabled"] is production.clahe_enabled
    assert render.decode_params["stretch_mode"] == production.stretch_mode
    assert render.decode_params["white_balance"] == production.white_balance.value
    assert json.loads(json.dumps(render.decode_params)) == render.decode_params


async def test_a_raw_that_will_not_decode_is_an_abstention():
    def render(raw, matrix, distortion):
        raise rawpy.LibRawFileUnsupportedError("b'Unsupported file format'")

    classifier = _Classifier()
    result = await _run(_activities(classifier=classifier, render=render), _payload())

    assert (result.status, result.probability) == ("decode_failed", None)
    assert (result.model_version, result.weights_sha256) == (
        SLATE_DETECTOR_VERSION,
        SHA,
    )
    assert classifier.seen == []


async def test_any_other_failure_is_raised_for_a_retry():
    def render(raw, matrix, distortion):
        raise OSError("disk full")

    with pytest.raises(OSError):
        await _run(_activities(render=render), _payload())


async def test_the_model_loads_once_per_process():
    """The lock is load-bearing: a cold pod's first frames arrive together."""
    loads = []
    activities = _activities(loads=loads)

    await asyncio.gather(*(_run(activities, _payload()) for _ in range(4)))
    await _run(activities, _payload())

    assert loads == [("/cache/slate-detector/q1/slate_efficientnet_b0.pt", SHA)]


@pytest.mark.parametrize(
    ("cause", "error_type"),
    [
        (ModelIntegrityError("sha256 mismatch"), "SlateDetectorWeightsCorrupt"),
        (ModelUnavailable("not in s3://model-weights"), "SlateDetectorWeightsUnavailable"),
        (KeyError("slate-detector"), "SlateDetectorNotInManifest"),
    ],
)  # fmt: skip
async def test_a_weights_failure_is_final(cause, error_type):
    async def fetch():
        raise cause

    with pytest.raises(ApplicationError) as raised:
        await _run(_activities(fetch=fetch), _payload())

    assert raised.value.type == error_type
    assert raised.value.non_retryable


async def test_missing_settings_are_final(monkeypatch):
    """Read on first use, inside the fetch, so a pod without the pin fails
    its first frame for good, not every frame forever."""
    for name in ENV:
        monkeypatch.delenv(name, raising=False)

    async def fetch():
        return sut.SlateDetectorSettings()

    with pytest.raises(ApplicationError) as raised:
        await _run(_activities(fetch=fetch), _payload())

    assert raised.value.type == "SlateDetectorSettingsInvalid"
    assert raised.value.non_retryable


async def test_a_checkpoint_that_is_not_the_detector_is_final():
    activities = _activities(refuse="arch 'convnext_tiny' is not 'efficientnet_b0'")

    with pytest.raises(ApplicationError) as raised:
        await _run(activities, _payload())

    assert raised.value.type == "SlateDetectorCheckpointInvalid"
    assert raised.value.non_retryable


# -- the workflow and the role ------------------------------------------------------------


SEEN: list[DetectSlateImageInput] = []


@activity.defn(name="detect_slate_presence")
async def _detect(payload: DetectSlateImageInput) -> SlatePresenceResult:
    SEEN.append(payload)
    return SlatePresenceResult(
        capture_id=payload.image.capture_id,
        status="predicted",
        probability=0.5,
        model_version=SLATE_DETECTOR_VERSION,
        weights_sha256=SHA,
        render=frames.render_settings(),
        predicted_at=datetime.now(UTC),
    )


async def test_the_workflow_scores_every_frame_with_the_dives_intrinsics():
    SEEN.clear()
    images = [DetectSlateImage(capture_id=uuid.uuid4(), raw=RAW) for _ in range(3)]
    payload = DetectSlateImagesInput(
        tenant_id=uuid.uuid4(),
        dive_id=uuid.uuid4(),
        camera_matrix=K,
        distortion_coefficients=D,
        images=images,
    )
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="test-slate-detect",
            workflows=[DetectSlatePresenceWorkflow],
            activities=[_detect],
        ):
            results = await env.client.execute_workflow(
                DetectSlatePresenceWorkflow.run,
                payload,
                id=f"detect-slate-{uuid.uuid4()}",
                task_queue="test-slate-detect",
                result_type=List[SlatePresenceResult],
                execution_timeout=timedelta(minutes=5),
            )

    assert [r.capture_id for r in results] == [i.capture_id for i in images]
    assert {(tuple(map(tuple, p.camera_matrix)), tuple(p.distortion_coefficients))
            for p in SEEN} == {(tuple(map(tuple, K)), tuple(D))}  # fmt: skip


def test_the_stage_is_on_the_gpu_role():
    """torch is only in the GPU image (the processor's `torch` extra); its
    CPU fallback serves the same queue."""
    gpu = registry.registration_for_role(registry.ROLE_GPU)
    names = {a.__temporal_activity_definition.name for a in gpu.activities}

    assert "detect_slate_presence" in names
    assert DetectSlatePresenceWorkflow in gpu.workflows
    for role in (registry.ROLE_PER_IMAGE, registry.ROLE_LIGHT):
        other = registry.registration_for_role(role)
        assert DetectSlatePresenceWorkflow not in other.workflows

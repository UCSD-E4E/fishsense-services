"""BioCLIP species pre-annotation on the processor: the weights, the activity,
the workflow and the role.

New in v2 (no v1 counterpart). Built the way the head/tail predict stage is
(tests/test_headtail_predict.py), for the same reasons:

* **the weights come through fishsense-core's manifest**: BioCLIP has no entry
  in fishsense-core 4.1.0's, so the stage pins its own from required settings
  (`FISHSENSE_BIOCLIP_SHA256`/`_SIZE` and the fallback's) and fetches through
  `weights.GarageWeightStore`; bytes that aren't the pinned file never load;
* **a weights failure is final**: one non-retryable type per cause, not a
  retry, and a re-download of gigabytes, per image;
* coral-gardeners-fish-detector@67c8627's fallback: BioCLIP 2.5 runs out of
  GPU memory -> BioCLIP 2, which stamps a negative version (permanently
  stale), and a worker already on it leaves an existing row alone;
* the JPEG is read from the ref the orchestrator hands over.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
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

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.species_prediction import (
    SPECIES_FALLBACK_MODEL_ID,
    SPECIES_FALLBACK_PREDICTOR_VERSION,
    SPECIES_PREDICTOR_VERSION,
    SPECIES_PRIMARY_MODEL_ID,
    SPECIES_STATUS_NO_UPGRADE_AVAILABLE,
    PredictSpeciesImage,
    PredictSpeciesImageInput,
    PredictSpeciesImagesInput,
    SpeciesCandidate,
    SpeciesPredictionResult,
)
from fishsense_services_processor import registry
from fishsense_services_processor.species_predict import activities as act
from fishsense_services_processor.species_predict import weights as sut
from fishsense_services_processor.species_predict.workflow import (
    PredictSpeciesImagesWorkflow,
)
from fishsense_services_processor.weights import GarageWeightStore

HOGFISH = SpeciesCandidate(
    choice="Fish, Hogfish (Lachnolaimus maximus)",
    scientific_name="Lachnolaimus maximus",
)
JPEG = ObjectRef(
    bucket="labels", key=f"tenants/{uuid.uuid4()}/preprocess_headtail_jpeg/abc.JPG"
)

PRIMARY = b"pretend these are 3.9 GB of BioCLIP 2.5"
FALLBACK = b"and these 1.7 GB of BioCLIP 2"
ENV = {
    "FISHSENSE_BIOCLIP_SHA256": hashlib.sha256(PRIMARY).hexdigest(),
    "FISHSENSE_BIOCLIP_SIZE": str(len(PRIMARY)),
    "FISHSENSE_BIOCLIP_FALLBACK_SHA256": hashlib.sha256(FALLBACK).hexdigest(),
    "FISHSENSE_BIOCLIP_FALLBACK_SIZE": str(len(FALLBACK)),
}


@pytest.fixture(name="pinned")
def _pinned(monkeypatch):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)


# -- the weights ------------------------------------------------------------------------


class TestTheSettings:
    @pytest.mark.parametrize("missing", sorted(ENV))
    def test_every_hash_and_size_is_required(self, monkeypatch, missing):
        for name, value in ENV.items():
            if name != missing:
                monkeypatch.setenv(name, value)
        monkeypatch.delenv(missing, raising=False)

        with pytest.raises(ValidationError):
            sut.BioclipSettings()

    @pytest.mark.parametrize(
        "name", ["FISHSENSE_BIOCLIP_SHA256", "FISHSENSE_BIOCLIP_FALLBACK_SHA256"]
    )
    def test_a_hash_must_look_like_one(self, pinned, monkeypatch, name):
        monkeypatch.setenv(name, "not-a-hash")
        with pytest.raises(ValidationError):
            sut.BioclipSettings()

    def test_a_size_must_be_positive(self, pinned, monkeypatch):
        monkeypatch.setenv("FISHSENSE_BIOCLIP_SIZE", "0")
        with pytest.raises(ValidationError):
            sut.BioclipSettings()


class TestTheManifest:
    def test_it_pins_both_models_at_their_own_keys(self, pinned):
        """`model-weights/bioclip/{2.5-vith14,2}/open_clip_model.safetensors`:
        the version is in the key, so new weights are a new object."""
        manifest = sut.bioclip_manifest(sut.BioclipSettings())

        primary = manifest.resolve("bioclip")
        fallback = manifest.resolve("bioclip", "2")

        assert (primary.version, primary.filename, primary.sha256, primary.size) == (
            "2.5-vith14",
            "open_clip_model.safetensors",
            hashlib.sha256(PRIMARY).hexdigest(),
            len(PRIMARY),
        )
        assert (fallback.version, fallback.sha256, fallback.size) == (
            "2",
            hashlib.sha256(FALLBACK).hexdigest(),
            len(FALLBACK),
        )

    def test_each_model_id_maps_to_its_version(self):
        assert sut.BIOCLIP_VERSIONS == {
            SPECIES_PRIMARY_MODEL_ID: "2.5-vith14",
            SPECIES_FALLBACK_MODEL_ID: "2",
        }

    def test_the_architectures_are_pinned_from_the_hub_repos(self):
        """open_clip's `open_clip_config.json` of each repo at its pinned
        revision: ViT-H/14 (1024-d) for 2.5, ViT-L/14 (768-d) for 2."""
        vith = sut.OPEN_CLIP_CONFIGS["2.5-vith14"]["model_cfg"]
        vitl = sut.OPEN_CLIP_CONFIGS["2"]["model_cfg"]
        assert (vith["embed_dim"], vith["vision_cfg"]["width"]) == (1024, 1280)
        assert (vitl["embed_dim"], vitl["vision_cfg"]["width"]) == (768, 1024)


@pytest.fixture(name="models_bucket")
def _models_bucket(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="model-weights")
        yield s3


async def _fetch(models_bucket, tmp_path, model_id=SPECIES_PRIMARY_MODEL_ID):
    return await sut.fetch_bioclip(
        model_id,
        store=GarageWeightStore(models_bucket, "model-weights"),
        cache_dir=tmp_path,
        settings=sut.BioclipSettings(),
    )


class TestTheFetch:
    async def test_the_pinned_bytes_land_beside_their_config(
        self, pinned, models_bucket, tmp_path
    ):
        """What open_clip's `local-dir:` reads: the weights and the config,
        side by side."""
        models_bucket.put_object(
            Bucket="model-weights",
            Key="bioclip/2.5-vith14/open_clip_model.safetensors",
            Body=PRIMARY,
        )

        directory, model_id = await _fetch(models_bucket, tmp_path)

        assert (directory / "open_clip_model.safetensors").read_bytes() == PRIMARY
        assert json.loads((directory / "open_clip_config.json").read_text()) == (
            sut.OPEN_CLIP_CONFIGS["2.5-vith14"]
        )
        assert (
            model_id == f"bioclip/2.5-vith14@{hashlib.sha256(PRIMARY).hexdigest()[:12]}"
        )

    async def test_the_fallback_is_fetched_by_its_own_pin(
        self, pinned, models_bucket, tmp_path
    ):
        models_bucket.put_object(
            Bucket="model-weights",
            Key="bioclip/2/open_clip_model.safetensors",
            Body=FALLBACK,
        )

        directory, model_id = await _fetch(
            models_bucket, tmp_path, SPECIES_FALLBACK_MODEL_ID
        )

        assert (directory / "open_clip_model.safetensors").read_bytes() == FALLBACK
        assert model_id.startswith("bioclip/2@")

    async def test_other_bytes_never_load(self, pinned, models_bucket, tmp_path):
        models_bucket.put_object(
            Bucket="model-weights",
            Key="bioclip/2.5-vith14/open_clip_model.safetensors",
            Body=b"tampered " + PRIMARY[9:],
        )
        with pytest.raises(ModelIntegrityError):
            await _fetch(models_bucket, tmp_path)

    async def test_the_original_bioclip_is_never_fetched(
        self, pinned, models_bucket, tmp_path
    ):
        with pytest.raises(ValueError, match="forbidden"):
            await _fetch(models_bucket, tmp_path, "hf-hub:imageomics/bioclip")


# -- the activity ---------------------------------------------------------------------


def _jpeg() -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((300, 400, 3), 40, dtype=np.uint8))
    assert ok
    return buf.tobytes()


class _FakeStore:
    def __init__(self, jpeg=b""):
        self.jpeg = jpeg
        self.read: list[ObjectRef] = []

    async def download_processed_jpeg(self, ref):
        self.read.append(ref)
        return self.jpeg


class _Encoder:
    """One axis per species; the image on the first. `oom` raises CUDA's
    out-of-memory on the image encode, as BioCLIP 2.5 does on a small GPU."""

    logit_scale = 100.0

    def __init__(self, oom=False):
        self.oom = oom

    def encode_text(self, prompts):
        return np.stack([np.array([1.0, 0.0])] * len(prompts))

    def encode_image(self, _image):
        if self.oom:
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
        return np.array([1.0, 0.0])


def _payload(**overrides) -> PredictSpeciesImageInput:
    image = {
        "capture_id": uuid.uuid4(),
        "headtail_prediction_id": uuid.uuid4(),
        "jpeg": JPEG,
        "mask_bbox": [100, 100, 200, 160],
    }
    image.update(overrides)
    return PredictSpeciesImageInput(
        image=PredictSpeciesImage(**image), candidates=[HOGFISH]
    )


def _activities(store, *, fetches=None, encoders=None, loads=None):
    """Weights 'fetched' per model id, and an encoder per loaded directory."""
    encoders = encoders or {}

    async def fetch(model_id):
        if fetches is not None:
            fetches.append(model_id)
        version = sut.BIOCLIP_VERSIONS[model_id]
        return Path(f"/cache/bioclip/{version}"), f"bioclip/{version}@0123456789ab"

    def load(directory):
        if loads is not None:
            loads.append(str(directory))
        return encoders.get(Path(directory).name, _Encoder())

    return act.SpeciesPredictActivities(
        store_factory=lambda: store, bioclip_weights=fetch, load_encoder=load
    )


async def _run(activities, payload):
    return await ActivityEnvironment().run(activities.predict_species_image, payload)


async def test_the_primary_model_classifies_the_crop():
    store, fetches, loads = _FakeStore(_jpeg()), [], []
    payload = _payload()

    result = await _run(_activities(store, fetches=fetches, loads=loads), payload)

    assert fetches == [SPECIES_PRIMARY_MODEL_ID]
    assert loads == ["/cache/bioclip/2.5-vith14"]
    assert store.read == [JPEG], "the JPEG is read from the ref it was handed"
    assert (result.status, result.predicted_choice) == ("predicted", HOGFISH.choice)
    assert result.headtail_prediction_id == payload.image.headtail_prediction_id
    assert result.predictor_version == SPECIES_PREDICTOR_VERSION
    assert result.model_id == "bioclip/2.5-vith14@0123456789ab"


async def test_the_model_loads_once_per_process():
    fetches = []
    activities = _activities(_FakeStore(_jpeg()), fetches=fetches)
    for _ in range(3):
        await _run(activities, _payload())
    assert fetches == [SPECIES_PRIMARY_MODEL_ID]


async def test_out_of_memory_falls_back_to_bioclip_2_and_says_so():
    """coral-gardeners' `_fallback_after_oom`: the image is classified by
    BioCLIP 2, and the row stamps the fallback version, so the cohort
    re-predicts it once the primary can run."""
    fetches = []
    activities = _activities(
        _FakeStore(_jpeg()),
        fetches=fetches,
        encoders={"2.5-vith14": _Encoder(oom=True)},
    )

    result = await _run(activities, _payload())

    assert fetches == [SPECIES_PRIMARY_MODEL_ID, SPECIES_FALLBACK_MODEL_ID]
    assert result.status == "predicted"
    assert result.predictor_version == SPECIES_FALLBACK_PREDICTOR_VERSION
    assert result.model_id == "bioclip/2@0123456789ab"


async def test_out_of_memory_at_load_falls_back_too():
    def load(directory):
        if Path(directory).name == "2.5-vith14":
            raise RuntimeError("CUDA error: out of memory")
        return _Encoder()

    async def fetch(model_id):
        version = sut.BIOCLIP_VERSIONS[model_id]
        return Path(f"/cache/bioclip/{version}"), f"bioclip/{version}@x"

    activities = act.SpeciesPredictActivities(
        store_factory=lambda: _FakeStore(_jpeg()), bioclip_weights=fetch,
        load_encoder=load,
    )  # fmt: skip

    result = await _run(activities, _payload())

    assert result.predictor_version == SPECIES_FALLBACK_PREDICTOR_VERSION


async def test_any_other_load_failure_is_not_a_fallback():
    """Only memory is: a broken primary must fail loudly, not quietly
    classify every fish with the lesser model."""

    def load(directory):
        if Path(directory).name == "2.5-vith14":
            raise RuntimeError("safetensors: header too large")
        return _Encoder()

    async def fetch(model_id):
        version = sut.BIOCLIP_VERSIONS[model_id]
        return Path(f"/cache/bioclip/{version}"), f"bioclip/{version}@x"

    activities = act.SpeciesPredictActivities(
        store_factory=lambda: _FakeStore(_jpeg()), bioclip_weights=fetch,
        load_encoder=load,
    )  # fmt: skip

    with pytest.raises(RuntimeError, match="header too large"):
        await _run(activities, _payload())


async def test_a_worker_on_the_fallback_leaves_an_existing_row_alone():
    """Head/tail's rule: rewriting would be identical or a downgrade."""
    store = _FakeStore(_jpeg())
    activities = _activities(store, encoders={"2.5-vith14": _Encoder(oom=True)})
    await _run(activities, _payload())  # falls back
    store.read.clear()

    result = await _run(activities, _payload(has_existing_prediction=True))

    assert result.status == SPECIES_STATUS_NO_UPGRADE_AVAILABLE
    assert result.predictor_version == SPECIES_FALLBACK_PREDICTOR_VERSION
    assert store.read == [], "nothing to read for a skip"


async def test_a_worker_on_the_primary_re_predicts_an_existing_row():
    result = await _run(
        _activities(_FakeStore(_jpeg())), _payload(has_existing_prediction=True)
    )
    assert result.predictor_version == SPECIES_PREDICTOR_VERSION


async def test_an_undecodable_jpeg_is_recorded_not_raised():
    result = await _run(_activities(_FakeStore(b"not a jpeg")), _payload())
    assert (result.status, result.predicted_choice) == ("decode_failed", None)


def _unset_settings():
    # Raises the ValidationError a pod without FISHSENSE_BIOCLIP_* raises.
    return sut.BioclipSettings()


class TestAWeightsFailureIsFinal:
    """Not the image's, and no retry passes it: left plain, every image of the
    dive retries without limit (the workflow sets no retry policy), each
    attempt re-downloading gigabytes. Each is its own type, so the failure
    says which."""

    @pytest.mark.parametrize(
        ("failure", "error_type"),
        [
            (_unset_settings, "BioclipSettingsInvalid"),
            (ModelIntegrityError("bioclip/2.5-vith14: expected sha256 ..."), "BioclipWeightsCorrupt"),
            (ModelUnavailable("bioclip/2.5-vith14: not in s3://model-weights"), "BioclipWeightsUnavailable"),
            (KeyError("bioclip"), "BioclipNotInManifest"),
        ],
        ids=["settings", "integrity", "unavailable", "manifest"],
    )  # fmt: skip
    async def test_is_non_retryable_and_named(self, monkeypatch, failure, error_type):
        for name in ENV:
            monkeypatch.delenv(name, raising=False)

        async def fetch(_model_id):
            if callable(failure):
                failure()
            raise failure

        activities = act.SpeciesPredictActivities(
            store_factory=lambda: _FakeStore(_jpeg()), bioclip_weights=fetch,
            load_encoder=lambda _d: _Encoder(),
        )  # fmt: skip

        with pytest.raises(ApplicationError) as excinfo:
            await _run(activities, _payload())

        assert (excinfo.value.type, excinfo.value.non_retryable) == (error_type, True)


# -- the workflow and the role --------------------------------------------------------


async def test_workflow_fans_out_one_prediction_per_image_with_the_candidates():
    """Head/tail's shape: one activity per image, 15 minutes start-to-close (a
    cold pod pays the weight fetch and the model load on its first image)."""
    seen: List[tuple] = []

    @activity.defn(name="predict_species_image")
    async def stub(payload: PredictSpeciesImageInput) -> SpeciesPredictionResult:
        seen.append(
            (
                payload.image.capture_id,
                tuple(c.choice for c in payload.candidates),
                activity.info().start_to_close_timeout,
            )
        )
        return SpeciesPredictionResult(
            capture_id=payload.image.capture_id,
            headtail_prediction_id=payload.image.headtail_prediction_id,
            status="predicted",
        )

    images = [_payload().image, _payload().image]
    payload = PredictSpeciesImagesInput(
        tenant_id=uuid.uuid4(), dive_id=uuid.uuid4(), candidates=[HOGFISH],
        images=images,
    )  # fmt: skip
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="test-predict-species",
            workflows=[PredictSpeciesImagesWorkflow],
            activities=[stub],
        ):
            results = await env.client.execute_workflow(
                PredictSpeciesImagesWorkflow.run,
                payload,
                id=f"test-predict-species-{uuid.uuid4()}",
                task_queue="test-predict-species",
                result_type=List[SpeciesPredictionResult],
            )

    assert [r.capture_id for r in results] == [i.capture_id for i in images]
    assert sorted(seen) == sorted(
        (i.capture_id, (HOGFISH.choice,), timedelta(minutes=15)) for i in images
    )


def test_species_prediction_is_a_gpu_stage():
    registration = registry.registration_for_role(registry.ROLE_GPU)
    names = {a.__temporal_activity_definition.name for a in registration.activities}

    assert PredictSpeciesImagesWorkflow in registration.workflows
    assert "predict_species_image" in names
    for role in (registry.ROLE_PER_IMAGE, registry.ROLE_LIGHT):
        assert PredictSpeciesImagesWorkflow not in (
            registry.registration_for_role(role).workflows
        )


def test_the_stage_reads_no_settings_at_import(monkeypatch):
    """The registry imports every stage wherever it runs; only a GPU pod ever
    fetches BioCLIP, so a pod without FISHSENSE_BIOCLIP_* must still start."""
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    import importlib

    from fishsense_services_processor.species_predict import stage

    assert importlib.reload(stage).STAGE.name == "species_predict"

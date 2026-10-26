"""A model's weights that cannot load are the worker's failure, never a
frame's (2026-10-24, the owner's ask).

Before, only the head/tail predict and the slate and species stages mapped a
weights failure (bad settings, bytes that aren't the pinned file, an object
missing from model-weights, no manifest entry) to a non-retryable error. The
laser detector (`laser_predict`, and through it the automatic frames' dot)
and the SimCC keypoint model loaded through core with no mapping at all, so
each frame retried the multi-GB download until its tries ran out. And the
workflows that leave a failing frame out (automatic frames, mask backfill,
slate pose, shadow segmentation) or record it ``failed`` (keypoints) knew
only the no-GPU refusal: a weights failure was charged to every frame of the
mission -- for the automatic frames, a frame failure recorded per frame
(0044), terminal ``frame_failed`` after three runs.

Pinned here: one shared mapping (`weights.refuse_unloadable_weights`) at
every load; each weights failure type is a worker refusal
(`fishsense_services_contracts.refusals`); and every workflow that leaves
frames out fails the run on one instead.
"""

from __future__ import annotations

import importlib
import uuid

import pytest
from botocore.exceptions import ClientError
from fishsense_core.models import ModelIntegrityError, ModelUnavailable
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.headtail_keypoints import KeypointFrame
from fishsense_services_contracts.refusals import (
    AUTOMATIC_FRAME_REFUSALS,
    HEADTAIL_KEYPOINTS,
    KEYPOINT_REFUSALS,
    LASER_DETECTOR,
    SAM3,
    SAM3_REFUSALS,
    WORKER_REFUSALS,
    weights_failure_types,
)
from fishsense_services_processor import weights
from fishsense_services_processor.headtail_keypoints.activities import (
    HeadtailKeypointActivities,
)
from fishsense_services_processor.laser_predict import activities as laser
from fishsense_services_processor.weights import ModelWeightsSettings

WEIGHTS_ENV = (
    "FISHSENSE_MODEL_WEIGHTS_ENDPOINT_URL",
    "FISHSENSE_MODEL_WEIGHTS_ACCESS_KEY_ID",
    "FISHSENSE_MODEL_WEIGHTS_SECRET_ACCESS_KEY",
    "FISHSENSE_MODEL_WEIGHTS_CACHE_DIR",
)


def _invalid_settings():
    # The ValidationError a pod without FISHSENSE_MODEL_WEIGHTS_* raises.
    return ModelWeightsSettings()


def _unparseable_settings():
    # pydantic-settings' own error: a value it cannot even parse.
    from pydantic_settings import SettingsError

    raise SettingsError('error parsing value for field "cache_dir" from source')


def _not_in_manifest():
    # The KeyError core's manifest raises for an entry it has not got.
    from fishsense_core.models import builtin_manifest

    builtin_manifest().resolve("no-such-model", "1")


def _a_model_that_does_not_validate():
    # A ValidationError that is not the pod's settings': a manifest entry or a
    # checkpoint description that stops validating after an upgrade.
    from pydantic import BaseModel

    class Entry(BaseModel):
        size: int

    Entry(size="not a size")


CAUSES = [
    (_invalid_settings, "SettingsInvalid"),
    (_unparseable_settings, "SettingsInvalid"),
    (ModelIntegrityError("m/1: expected sha256 ..."), "WeightsCorrupt"),
    (ModelUnavailable("m/1: not in s3://model-weights/m/1/f"), "WeightsUnavailable"),
    (_not_in_manifest, "NotInManifest"),
    (_a_model_that_does_not_validate, "LoadInvalid"),
]
CAUSE_IDS = [
    "settings", "settings-unparseable", "integrity", "unavailable", "manifest",
    "load-invalid",
]  # fmt: skip


def _raise(failure):
    if callable(failure):
        failure()
    raise failure


@pytest.fixture
def no_weights_env(monkeypatch):
    for name in WEIGHTS_ENV:
        monkeypatch.delenv(name, raising=False)


# -- the shared mapping -------------------------------------------------------------


@pytest.mark.parametrize(("failure", "kind"), CAUSES, ids=CAUSE_IDS)
def test_each_cause_is_its_own_non_retryable_type(no_weights_env, failure, kind):
    with pytest.raises(ApplicationError) as e:
        with weights.refuse_unloadable_weights(LASER_DETECTOR, "the laser detector"):
            _raise(failure)
    assert (e.value.type, e.value.non_retryable) == (f"LaserDetector{kind}", True)
    assert e.value.type in weights_failure_types(LASER_DETECTOR)
    assert e.value.message.startswith("the laser detector's weights cannot load")


def test_a_load_that_does_not_validate_is_not_blamed_on_the_settings(no_weights_env):
    """Deterministic, so not retried; but named for what failed, not sent to
    the Secret (review of #358, and of #364: propagated, it was retryable,
    and charged every frame of the mission)."""
    with pytest.raises(ApplicationError) as e:
        with weights.refuse_unloadable_weights(LASER_DETECTOR, "the laser detector"):
            _a_model_that_does_not_validate()
    assert e.value.type == "LaserDetectorLoadInvalid"
    assert "FISHSENSE_MODEL_WEIGHTS" not in e.value.message


def test_a_key_error_that_is_not_the_manifests_is_not_called_one():
    """Only core's "unknown model" KeyError is a missing manifest entry
    (review of #364): one from a checkpoint's state dict, or an environment
    lookup, inside the load is something else and propagates as itself."""
    with pytest.raises(KeyError) as e:
        with weights.refuse_unloadable_weights(LASER_DETECTOR, "the laser detector"):
            raise KeyError("model")
    assert e.value.args == ("model",)


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("CUDA out of memory"),
        ClientError({"Error": {"Code": "500"}}, "HeadObject"),
        ClientError({"Error": {"Code": "403"}}, "HeadObject"),
    ],
    ids=["runtime", "s3-500", "s3-403"],
)
def test_anything_else_stays_retryable(failure):
    """A Garage outage or a 5xx may pass on a retry: only a missing key is
    `ModelUnavailable` (`GarageWeightStore.download_to`)."""
    with pytest.raises(type(failure)) as e:
        with weights.refuse_unloadable_weights(SAM3, "SAM 3.1"):
            raise failure
    assert e.value is failure


def test_every_weights_failure_is_a_worker_refusal():
    for model in (SAM3, LASER_DETECTOR, HEADTAIL_KEYPOINTS):
        assert set(weights_failure_types(model)) <= WORKER_REFUSALS
    assert set(SAM3_REFUSALS) == {"NoGpuForSam3", *weights_failure_types(SAM3)}
    assert set(AUTOMATIC_FRAME_REFUSALS) == {
        *SAM3_REFUSALS,
        *weights_failure_types(LASER_DETECTOR),
    }
    assert set(KEYPOINT_REFUSALS) == {
        "NoGpuForKeypoints",
        *weights_failure_types(HEADTAIL_KEYPOINTS),
    }


# -- the laser detector (laser predict, and the automatic frames' dot) --------------


@pytest.fixture
def fresh_detector(monkeypatch):
    monkeypatch.setattr(laser, "_DETECTOR", None)


@pytest.mark.parametrize(("failure", "kind"), CAUSES, ids=CAUSE_IDS)
def test_a_laser_detector_weights_failure_is_final(
    monkeypatch, no_weights_env, fresh_detector, failure, kind
):
    monkeypatch.setattr(laser, "_load_detector", lambda: _raise(failure))
    with pytest.raises(ApplicationError) as e:
        laser._get_detector()  # pylint: disable=protected-access
    assert (e.value.type, e.value.non_retryable) == (f"LaserDetector{kind}", True)
    assert laser._DETECTOR is None, "nothing is cached on a failure"


def test_a_pod_without_weights_settings_refuses_the_laser_detector(
    no_weights_env, fresh_detector
):
    """The real load: the settings are read first, inside the mapping."""
    with pytest.raises(ApplicationError) as e:
        laser._get_detector()  # pylint: disable=protected-access
    assert (e.value.type, e.value.non_retryable) == (
        "LaserDetectorSettingsInvalid",
        True,
    )


# -- the SimCC keypoint model -------------------------------------------------------


@pytest.mark.parametrize(("failure", "kind"), CAUSES, ids=CAUSE_IDS)
async def test_a_keypoint_model_weights_failure_is_final(no_weights_env, failure, kind):
    """And refuses before reading the frame's render (review of #364): on a
    pod whose weights cannot load, every frame of a chunk otherwise read its
    render from Garage first."""
    reads = []

    class _Store:
        async def download_image(self, url):
            reads.append(url)
            return b"jpeg"

    acts = HeadtailKeypointActivities(
        store_factory=_Store,
        detector=lambda: _raise(failure),
        checkpoint="headtail-keypoints/simcc-sam-e12",
        cuda_available=lambda: True,
    )
    frame = KeypointFrame(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                          render_url="https://g/r", mask_bbox=[1, 1, 5, 5])  # fmt: skip
    with pytest.raises(ApplicationError) as e:
        await ActivityEnvironment().run(acts.predict_headtail_keypoints, frame)
    assert (e.value.type, e.value.non_retryable) == (f"HeadtailKeypoints{kind}", True)
    assert reads == []


# -- every workflow that leaves frames out knows the refusals -----------------------


@pytest.mark.parametrize(
    ("module", "refusals"),
    [
        ("automatic_frames.workflow", AUTOMATIC_FRAME_REFUSALS),
        ("mask_backfill.workflow", SAM3_REFUSALS),
        ("slate_pose.workflow", SAM3_REFUSALS),
        ("shadow_calibration.workflow", SAM3_REFUSALS),
        ("headtail_keypoints.workflow", KEYPOINT_REFUSALS),
    ],
)
def test_a_workflow_that_leaves_frames_out_fails_on_a_refusal(module, refusals):
    """Its refusals are the worker's every one, not just the no-GPU one: a
    weights failure missing here is charged to every frame of the run."""
    workflow_module = importlib.import_module(f"fishsense_services_processor.{module}")
    # pylint: disable-next=protected-access
    assert set(refusals) <= set(workflow_module._WORKER_REFUSALS)

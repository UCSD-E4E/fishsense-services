"""BioCLIP with fish-model classes (read-only replay): one image encoding
ranked against the species set alone -- what production answers -- and with
the "Fish Model" leaves added by their own prompts; P(model) is those
leaves' summed probability. A stand-in encoder: text on one axis per
species and one for anything "plastic"; the image placed by the test."""

from __future__ import annotations

import io
import uuid

import numpy as np
import pytest
from PIL import Image
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from fishsense_services_contracts.headtail_keypoints import KeypointFrame
from fishsense_services_contracts.species_models import (
    ModelCandidate,
    SpeciesModelsInput,
    SpeciesModelsResult,
)
from fishsense_services_contracts.species_prediction import SpeciesCandidate
from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_processor.species_models.activities import (
    SpeciesModelsActivities,
)
from fishsense_services_processor.species_models.score import score_frame
from fishsense_services_processor.species_models.workflow import (
    ScoreSpeciesModelsWorkflow,
)
from fishsense_services_processor.species_predict.classifier import BioclipClassifier

SPECIES = [SpeciesCandidate(choice="Fish, Hogfish (Lachnolaimus maximus)",
                            scientific_name="Lachnolaimus maximus"),
           SpeciesCandidate(choice="Fish, Mutton Snapper (Lutjanus analis)",
                            scientific_name="Lutjanus analis")]  # fmt: skip
MODELS = [ModelCandidate(choice="Fish Model, Snook", prompts=["a plastic model of a snook"]),
          ModelCandidate(choice="Fish Model, Shark", prompts=["a plastic toy shark"])]  # fmt: skip
AXES = {"Lachnolaimus": 0, "Lutjanus": 1, "plastic": 2}


class _Encoder:
    logit_scale = 20.0

    def __init__(self, image_at):
        self.image_at, self.images = image_at, 0

    def encode_text(self, prompts):
        out = []
        for p in prompts:
            v = np.full(3, 0.01)
            for word, axis in AXES.items():
                if word in p:
                    v[axis] = 1.0
            out.append(v)
        return np.stack(out)

    def encode_image(self, _image):
        self.images += 1
        return np.asarray(self.image_at, float)


def _classifier(image_at):
    return BioclipClassifier(encoder=_Encoder(image_at), model_id="bioclip/2.5@abc",
                             predictor_version=1)  # fmt: skip


def _jpeg(w=400, h=300):
    buf = io.BytesIO()
    Image.fromarray(np.full((h, w, 3), 120, np.uint8)).save(buf, format="JPEG")
    return buf.getvalue()


def test_a_fish_looks_like_its_species_and_not_a_model():
    c = _classifier([1.0, 0.0, 0.0])
    out = score_frame(_jpeg(), [100, 100, 200, 160], SPECIES, MODELS, c)
    assert out["status"] == "scored" and out["model_id"] == "bioclip/2.5@abc"
    assert out["species_top5"][0].choice == SPECIES[0].choice
    assert out["with_models_top5"][0].choice == SPECIES[0].choice
    assert out["p_model"] < 0.01
    assert c._encoder.images == 1  # one image encoding for both rankings


def test_a_model_is_named_a_species_alone_and_a_model_with_the_classes():
    c = _classifier([0.0, 0.3, 1.0])
    out = score_frame(_jpeg(), [100, 100, 200, 160], SPECIES, MODELS, c)
    assert out["species_top5"][0].choice == SPECIES[1].choice  # what production says
    assert out["with_models_top5"][0].choice.startswith("Fish Model")
    assert out["p_model"] > 0.9
    assert sum(s.probability for s in out["species_top5"]) == pytest.approx(1.0)


def test_the_control_ranking_is_what_production_answers():
    c = _classifier([0.2, 1.0, 0.5])
    out = score_frame(_jpeg(), [100, 100, 200, 160], SPECIES, MODELS, c)
    prod = _classifier([0.2, 1.0, 0.5]).classify(Image.new("RGB", (8, 8)), SPECIES)
    assert [(s.choice, round(s.probability, 9)) for s in out["species_top5"]] == [
        (ch, round(p, 9)) for ch, p in prod.top5
    ]


def test_an_unreadable_render_is_the_frames_status():
    out = score_frame(
        b"not a jpeg", [1, 1, 5, 5], SPECIES, MODELS, _classifier([1, 0, 0])
    )
    assert out["status"] == "decode_failed" and out.get("p_model") is None


class _Store:
    def __init__(self, data, error=None):
        self.data, self.error, self.read = data, error, []

    async def download_image(self, ref=None, url=None):
        self.read.append(url)
        if self.error:
            raise self.error
        return self.data


def _request(**kw):
    return SpeciesModelsInput(
        tenant_id=uuid.uuid4(), mission_id=uuid.uuid4(), species=SPECIES, models=MODELS,
        frames=[KeypointFrame(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                              render_url=kw.get("url", "https://g/r"), mask_bbox=[100, 100, 200, 160])],
    )  # fmt: skip


def _acts(store, image_at=(1.0, 0.0, 0.0)):
    c = _classifier(list(image_at))

    async def loaded():
        return c

    return SpeciesModelsActivities(store_factory=lambda: store, classifier=loaded)


async def test_a_frame_is_read_by_url_and_scored():
    store = _Store(_jpeg())
    req = _request()
    r = await ActivityEnvironment().run(_acts(store).score_species_models, req)
    assert store.read == ["https://g/r"] and r.status == "scored"
    assert (
        r.prediction_id == req.frames[0].prediction_id
        and r.model_id == "bioclip/2.5@abc"
    )


async def test_a_render_not_there_is_the_frames_status():
    r = await ActivityEnvironment().run(
        _acts(_Store(None, RuntimeError("404"))).score_species_models, _request()
    )
    assert r.status == "render_unavailable" and r.species_top5 == []


FAIL: set = set()


@activity.defn(name="score_species_models")
async def _score(req: SpeciesModelsInput) -> SpeciesModelsResult:
    (f,) = req.frames
    if f.capture_id in FAIL:
        raise ApplicationError("boom")
    return SpeciesModelsResult(prediction_id=f.prediction_id, capture_id=f.capture_id,
                               status="scored")  # fmt: skip


async def _run(ids):
    req = _request().model_copy(update={"frames": [
        KeypointFrame(prediction_id=uuid.uuid4(), capture_id=c, render_url="u",
                      mask_bbox=[1, 1, 5, 5]) for c in ids]})  # fmt: skip
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue="q", workflows=[ScoreSpeciesModelsWorkflow],
                          activities=[_score], workflow_runner=UnsandboxedWorkflowRunner()):  # fmt: skip
            return await env.client.execute_workflow(
                ScoreSpeciesModelsWorkflow.run,
                req,
                id=f"t-{uuid.uuid4()}",
                task_queue="q",
            )


async def test_each_frame_is_scored_and_one_failing_every_try_is_recorded():
    bad, good = uuid.uuid4(), uuid.uuid4()
    FAIL.add(bad)
    try:
        out = await _run([bad, good])
    finally:
        FAIL.clear()
    assert [(r.capture_id, r.status) for r in out] == [
        (bad, "failed"),
        (good, "scored"),
    ]

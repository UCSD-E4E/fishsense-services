"""BioCLIP with fish-model classes, read-only (v20): each frame scored against
the species set alone and with the model classes added."""

import uuid

from fishsense_services_contracts.headtail_keypoints import KeypointFrame
from fishsense_services_contracts.species_models import (
    ModelCandidate,
    SpeciesModelsInput,
    SpeciesModelsResult,
)
from fishsense_services_contracts.species_prediction import (
    SpeciesCandidate,
    SpeciesScore,
)


def test_a_request_carries_both_candidate_sets_and_the_frames():
    m = uuid.uuid4()
    r = SpeciesModelsInput(
        tenant_id=uuid.uuid4(), mission_id=m,
        species=[SpeciesCandidate(choice="Fish, Hogfish (Lachnolaimus maximus)",
                                  scientific_name="Lachnolaimus maximus")],
        models=[ModelCandidate(choice="Fish Model, Snook", prompts=["a plastic model of a snook"])],
        frames=[KeypointFrame(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                              render_url="u", mask_bbox=[1, 2, 3, 4])],
    )  # fmt: skip
    wire = r.model_dump(mode="json", by_alias=True)
    assert wire["dive_id"] == str(m)
    assert SpeciesModelsInput.model_validate(wire).models[0].prompts == [
        "a plastic model of a snook"
    ]


def test_a_result_holds_both_rankings_and_the_model_probability():
    r = SpeciesModelsResult(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                            status="scored", model_id="bioclip/2.5@x",
                            species_top5=[SpeciesScore(choice="a", probability=0.9)],
                            with_models_top5=[SpeciesScore(choice="m", probability=0.6)],
                            p_model=0.6)  # fmt: skip
    assert SpeciesModelsResult.model_validate(r.model_dump(mode="json")).p_model == 0.6

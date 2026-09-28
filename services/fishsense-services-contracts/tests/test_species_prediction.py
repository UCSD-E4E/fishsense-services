"""The BioCLIP species pre-annotation stage's contract.

New in v2: v1 has no species model, so there is no v1 test to port. The
classifier's choices are ported from coral-gardeners-fish-detector@67c8627
(src/coral_fish_pipeline/classification/bioclip_classifier.py and
species_prompts.py, configs/default.yaml's `crop:` block), and pinned here
because both sides read them: the processor builds the prompts and crops, and
the orchestrator's cohort selects on a mismatch with the version.

Also here: the head/tail result's `mask_bbox`, the additive field the species
stage crops by.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from fishsense_services_contracts.headtail import HeadtailPredictionResult
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.species_prediction import (
    SPECIES_CROP_MIN_SIZE,
    SPECIES_CROP_PADDING,
    SPECIES_FALLBACK_MODEL_ID,
    SPECIES_FALLBACK_PREDICTOR_VERSION,
    SPECIES_PREDICTOR_VERSION,
    SPECIES_PRIMARY_MODEL_ID,
    SPECIES_PROMPT_TEMPLATES,
    SPECIES_STATUS_NO_UPGRADE_AVAILABLE,
    SPECIES_STATUSES,
    SPECIES_TOP_K,
    PredictSpeciesImage,
    PredictSpeciesImageInput,
    PredictSpeciesImagesInput,
    SpeciesCandidate,
    SpeciesPredictionResult,
    SpeciesScore,
    species_prompts,
)

JPEG = ObjectRef(
    bucket="labels", key=f"tenants/{uuid4()}/preprocess_headtail_jpeg/abc.JPG"
)
HOGFISH = SpeciesCandidate(
    choice="Fish, Hogfish (Lachnolaimus maximus)",
    scientific_name="Lachnolaimus maximus",
)
SNAPPER = SpeciesCandidate(
    choice="Fish, Grey Snapper (Lutjanus griseus)",
    scientific_name="Lutjanus griseus",
)


def _image(**overrides) -> PredictSpeciesImage:
    values = {
        "capture_id": uuid4(),
        "headtail_prediction_id": uuid4(),
        "jpeg": JPEG,
        "mask_bbox": [100, 200, 300, 260],
    }
    values.update(overrides)
    return PredictSpeciesImage(**values)


class TestTheVersionNamesTheBehaviour:
    def test_the_current_version_is_a_real_one(self):
        assert isinstance(SPECIES_PREDICTOR_VERSION, int)
        assert SPECIES_PREDICTOR_VERSION >= 1

    def test_the_fallback_can_never_equal_a_real_version(self):
        """A fallback row is stale by construction: the cohort keys on a
        mismatch, so it is re-predicted once the primary loads (head/tail's
        rule, `HEADTAIL_FALLBACK_PREDICTOR_VERSION`)."""
        assert SPECIES_FALLBACK_PREDICTOR_VERSION < 0

    def test_the_models_are_bioclip_2_5_then_bioclip_2(self):
        """coral-gardeners' pair: BioCLIP 2.5 (ViT-H/14) first, BioCLIP 2 when
        it cannot run."""
        assert SPECIES_PRIMARY_MODEL_ID == "hf-hub:imageomics/bioclip-2.5-vith14"
        assert SPECIES_FALLBACK_MODEL_ID == "hf-hub:imageomics/bioclip-2"

    def test_the_original_bioclip_is_neither(self):
        for model_id in (SPECIES_PRIMARY_MODEL_ID, SPECIES_FALLBACK_MODEL_ID):
            assert not model_id.endswith("imageomics/bioclip")


class TestThePromptSet:
    def test_four_templates_per_species_as_coral_gardeners_wrote_them(self):
        assert species_prompts("Lachnolaimus maximus") == [
            "a photo of a Lachnolaimus maximus",
            "an underwater photo of a Lachnolaimus maximus",
            "a reef fish species Lachnolaimus maximus",
            "Lachnolaimus maximus, a reef fish",
        ]

    def test_the_templates_are_a_constant(self):
        """Part of what the version names: editing one is a bump."""
        assert len(SPECIES_PROMPT_TEMPLATES) == 4
        assert isinstance(SPECIES_PROMPT_TEMPLATES, tuple)


def test_the_crop_is_padded_20_percent_with_a_64px_floor():
    """coral-gardeners' configs/default.yaml `crop:` block (the cropper's
    own default is 0.30; the pipeline runs 0.20)."""
    assert (SPECIES_CROP_PADDING, SPECIES_CROP_MIN_SIZE) == (0.20, 64)


def test_top_five_is_recorded():
    assert SPECIES_TOP_K == 5


def test_statuses_are_a_prediction_or_an_undecodable_frame():
    assert SPECIES_STATUSES == ("predicted", "decode_failed")
    assert SPECIES_STATUS_NO_UPGRADE_AVAILABLE not in SPECIES_STATUSES


class TestTheMaskBox:
    """[x_min, y_min, x_max, y_max) in rectified-frame pixels, max exclusive."""

    @pytest.mark.parametrize(
        "box",
        [[1, 2, 3], [1, 2, 3, 4, 5], [10, 0, 10, 5], [0, 10, 5, 10], [5, 0, 4, 5],
         [-1, 0, 4, 5]],
        ids=["short", "long", "zero-width", "zero-height", "inverted", "negative"],
    )  # fmt: skip
    def test_a_malformed_box_is_refused(self, box):
        with pytest.raises(ValidationError):
            _image(mask_bbox=box)
        with pytest.raises(ValidationError):
            HeadtailPredictionResult(capture_id=uuid4(), status="x", mask_bbox=box)

    def test_head_tail_results_carry_none_by_default(self):
        """Additive: a result without one (an abstention, or an older
        processor) still parses."""
        assert (
            HeadtailPredictionResult(capture_id=uuid4(), status="x").mask_bbox is None
        )

    def test_a_well_formed_box_round_trips(self):
        image = _image()
        assert PredictSpeciesImage.model_validate_json(image.model_dump_json()) == image


class TestTheCandidates:
    def test_the_workflow_input_carries_them_once(self):
        payload = PredictSpeciesImagesInput(
            tenant_id=uuid4(),
            dive_id=uuid4(),
            candidates=[HOGFISH, SNAPPER],
            images=[_image()],
        )
        assert payload.candidates == [HOGFISH, SNAPPER]

    def test_none_is_refused(self):
        with pytest.raises(ValidationError):
            PredictSpeciesImageInput(image=_image(), candidates=[])

    def test_a_repeated_choice_is_refused(self):
        """Two scores for one choice would split its probability."""
        with pytest.raises(ValidationError):
            PredictSpeciesImageInput(image=_image(), candidates=[HOGFISH, HOGFISH])


def test_a_result_names_its_crop_source_and_its_scores():
    capture, source = uuid4(), uuid4()
    result = SpeciesPredictionResult(
        capture_id=capture,
        headtail_prediction_id=source,
        status="predicted",
        predicted_choice=HOGFISH.choice,
        top1_probability=0.9,
        margin=0.85,
        top5=[SpeciesScore(choice=HOGFISH.choice, probability=0.9)],
        predictor_version=SPECIES_PREDICTOR_VERSION,
        model_id="bioclip/2.5-vith14@0123456789ab",
    )
    assert SpeciesPredictionResult.model_validate_json(result.model_dump_json()) == (
        result
    )

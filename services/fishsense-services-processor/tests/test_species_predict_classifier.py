"""BioCLIP species pre-annotation on the processor: the crop, the model rule,
the scoring (no torch, no weights, no network).

New in v2: v1 has no species model. Ported from
coral-gardeners-fish-detector@67c8627:

* tests/test_bioclip_classifier.py -- the model rule (BioCLIP 2.5 then 2; the
  original BioCLIP forbidden) and `test_predict_image_applies_openclip_logit_
  scale`, here over numpy, since the scoring is pure arithmetic and the only
  torch left is the encoder behind a seam;
* tests/test_cropper.py `test_expand_box_clips`, and the padding rule of
  src/coral_fish_pipeline/utils/boxes.py (`expand_box_xyxy`, `clip_box_xyxy`).

v2 changes, each pinned below:

* **the original BioCLIP has no opt-in.** coral-gardeners allowed it behind
  `allow_original_bioclip=True`; here it is refused, full stop;
* the fish is cropped from the head/tail stage's JPEG by its kept mask's box,
  in memory (coral-gardeners wrote the crop to disk as a quality-95 JPEG and
  read it back);
* the classifier builds from a local, verified directory (`local-dir:`), never
  from the hub: nothing here can download.
"""

from __future__ import annotations

import sys
import types
import uuid

import cv2
import numpy as np
import pytest
from PIL import Image

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.species_prediction import (
    SPECIES_FALLBACK_MODEL_ID,
    SPECIES_PREDICTOR_VERSION,
    SPECIES_PRIMARY_MODEL_ID,
    PredictSpeciesImage,
    SpeciesCandidate,
)
from fishsense_services_processor.species_predict import classifier as sut
from fishsense_services_processor.species_predict.crop import crop_fish, expand_box
from fishsense_services_processor.species_predict.predict import predict_species

HOGFISH = SpeciesCandidate(
    choice="Fish, Hogfish (Lachnolaimus maximus)",
    scientific_name="Lachnolaimus maximus",
)
SNAPPER = SpeciesCandidate(
    choice="Fish, Grey Snapper (Lutjanus griseus)",
    scientific_name="Lutjanus griseus",
)


# -- the crop (coral-gardeners' boxes.py and cropper) ---------------------------------


def test_expand_box_clips():
    """coral-gardeners' `test_expand_box_clips`, verbatim in substance."""
    box = expand_box([5, 5, 10, 10], 0.5, width=12, height=12, min_size=1)
    assert box[0] >= 0 and box[1] >= 0
    assert box[2] <= 12 and box[3] <= 12
    assert box[2] > box[0] and box[3] > box[1]


def test_the_box_grows_by_the_padding_on_every_side():
    assert expand_box([100, 100, 200, 150], 0.2, width=1000, height=1000,
                      min_size=1) == [80.0, 90.0, 220.0, 160.0]  # fmt: skip


def test_a_small_box_is_grown_to_the_floor_about_its_centre():
    assert expand_box([100, 100, 110, 110], 0.2, width=1000, height=1000,
                      min_size=64) == [73.0, 73.0, 137.0, 137.0]  # fmt: skip


def _frame_jpeg(width=400, height=300) -> bytes:
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 2] = 255  # red, in BGR
    frame[100:200, 150:250] = (255, 0, 0)  # a blue fish
    ok, buf = cv2.imencode(".png", frame)  # lossless, so colours are exact
    assert ok
    return buf.tobytes()


def test_the_crop_is_the_padded_box_in_rgb():
    crop = crop_fish(_frame_jpeg(), [150, 100, 250, 200])

    assert isinstance(crop, Image.Image) and crop.mode == "RGB"
    assert crop.size == (140, 140), "100 px + 20% a side"
    assert crop.getpixel((70, 70)) == (0, 0, 255), "the fish, in RGB"
    assert crop.getpixel((2, 2)) == (255, 0, 0), "the padding, in RGB"


def test_a_box_past_the_frame_is_clipped_to_it():
    crop = crop_fish(_frame_jpeg(), [350, 250, 400, 300])
    assert crop.size == (400 - 340, 300 - 240)


def test_an_undecodable_jpeg_has_no_crop():
    assert crop_fish(b"not a jpeg", [0, 0, 10, 10]) is None


# -- the model rule (coral-gardeners' constructor tests) ------------------------------


def test_the_default_models_are_bioclip_2_5_then_2():
    assert sut.validate_model_id(SPECIES_PRIMARY_MODEL_ID) == (
        "hf-hub:imageomics/bioclip-2.5-vith14"
    )
    assert sut.validate_model_id(SPECIES_FALLBACK_MODEL_ID) == (
        "hf-hub:imageomics/bioclip-2"
    )


@pytest.mark.parametrize(
    "model_id",
    [
        "hf-hub:imageomics/bioclip",
        "imageomics/bioclip",
        "hf-hub:imageomics/bioclip@main",
    ],
)
def test_original_bioclip_is_rejected(model_id):
    with pytest.raises(ValueError, match="Original BioCLIP is forbidden"):
        sut.validate_model_id(model_id)


def test_original_bioclip_has_no_opt_in():
    """v2: coral-gardeners let `allow_original_bioclip=True` through; the
    stage has no such switch."""
    with pytest.raises(TypeError):
        sut.validate_model_id(  # pylint: disable=unexpected-keyword-arg
            "hf-hub:imageomics/bioclip", allow_original_bioclip=True
        )


@pytest.mark.parametrize(
    "model_id", ["hf-hub:imageomics/bioclip-2", "hf-hub:imageomics/bioclip-2.5-vith14"]
)
def test_bioclip_2_models_are_allowed(model_id):
    assert sut.validate_model_id(model_id) == model_id


@pytest.mark.parametrize(
    "model_id", ["hf-hub:laion/CLIP-ViT-H-14", "hf-hub:imageomics/bioclip-2-typo"]
)
def test_any_other_model_is_unsupported(model_id):
    with pytest.raises(ValueError, match="Unsupported BioCLIP model"):
        sut.validate_model_id(model_id)


# -- the scoring ------------------------------------------------------------------------


def test_predict_image_applies_openclip_logit_scale():
    """coral-gardeners' test: with CLIP's logit scale (100), an image on one
    text embedding is ~certain; unscaled cosine similarities would give ~0.73."""
    ranking = sut.rank(
        np.array([1.0, 0.0]),
        np.array([[1.0, 0.0], [0.0, 1.0]]),
        logit_scale=100.0,
        choices=["target fish", "other fish"],
    )

    assert ranking.top5[0][0] == "target fish"
    assert ranking.top1_probability > 0.99
    assert ranking.margin > 0.99


def test_the_scores_are_a_softmax_over_the_closed_set():
    ranking = sut.rank(
        np.array([3.0, 4.0]),  # not unit length: normalised first
        np.array([[1.0, 0.0], [0.0, 2.0], [0.6, 0.8]]),
        logit_scale=1.0,
        choices=["a", "b", "c"],
    )
    logits = np.array([0.6, 0.8, 1.0])
    expected = np.exp(logits) / np.exp(logits).sum()

    assert [choice for choice, _ in ranking.top5] == ["c", "b", "a"]
    assert [p for _, p in ranking.top5] == pytest.approx(sorted(expected, reverse=True))
    assert ranking.margin == pytest.approx(expected[2] - expected[1])


def test_top_five_of_many_and_all_of_few():
    many = sut.rank(np.eye(8)[0], np.eye(8), 10.0, [str(i) for i in range(8)])
    one = sut.rank(np.array([1.0]), np.array([[1.0]]), 10.0, ["only"])

    assert len(many.top5) == 5
    assert (one.top5, one.margin) == (
        [("only", pytest.approx(1.0))],
        pytest.approx(1.0),
    )


def test_a_species_embedding_is_the_normalised_mean_of_its_prompts():
    """coral-gardeners' `_get_text_features`: each prompt normalised, then
    their mean normalised."""
    prompts = np.array([[2.0, 0.0], [0.0, 3.0]])
    assert sut.mean_text_embedding(prompts) == pytest.approx(
        np.array([1.0, 1.0]) / np.sqrt(2)
    )


# -- the classifier, over a fake encoder -------------------------------------------------


class _FakeEncoder:
    """The seam torch sits behind: one axis per species, the image on an axis
    chosen by the test."""

    def __init__(self, image_axis=0):
        self.image_axis = image_axis
        self.prompts_seen: list[list[str]] = []
        self.logit_scale = 100.0

    def encode_text(self, prompts):
        self.prompts_seen.append(list(prompts))
        axis = 0 if "Lachnolaimus" in prompts[0] else 1
        return np.stack([np.eye(2)[axis]] * len(prompts))

    def encode_image(self, _image):
        return np.eye(2)[self.image_axis]


def _classifier(encoder, **overrides):
    values = {
        "encoder": encoder,
        "model_id": "bioclip/2.5-vith14@0123456789ab",
        "predictor_version": SPECIES_PREDICTOR_VERSION,
    }
    values.update(overrides)
    return sut.BioclipClassifier(**values)


def test_each_species_is_prompted_with_its_four_templates():
    encoder = _FakeEncoder()
    _classifier(encoder).classify(Image.new("RGB", (8, 8)), [HOGFISH, SNAPPER])

    assert encoder.prompts_seen == [
        [
            "a photo of a Lachnolaimus maximus",
            "an underwater photo of a Lachnolaimus maximus",
            "a reef fish species Lachnolaimus maximus",
            "Lachnolaimus maximus, a reef fish",
        ],
        [
            "a photo of a Lutjanus griseus",
            "an underwater photo of a Lutjanus griseus",
            "a reef fish species Lutjanus griseus",
            "Lutjanus griseus, a reef fish",
        ],
    ]


def test_the_ranking_names_the_taxonomy_choice_not_the_scientific_name():
    ranking = _classifier(_FakeEncoder(image_axis=1)).classify(
        Image.new("RGB", (8, 8)), [HOGFISH, SNAPPER]
    )
    assert ranking.top5[0][0] == SNAPPER.choice


def test_text_embeddings_are_computed_once_per_candidate_set():
    encoder = _FakeEncoder()
    classifier = _classifier(encoder)
    for _ in range(3):
        classifier.classify(Image.new("RGB", (8, 8)), [HOGFISH, SNAPPER])
    classifier.classify(Image.new("RGB", (8, 8)), [SNAPPER, HOGFISH])

    assert len(encoder.prompts_seen) == 4, "two species, two distinct sets"


# -- the encoder builds from the verified directory -------------------------------------


def test_the_encoder_loads_from_the_local_directory_never_the_hub(
    monkeypatch, tmp_path
):
    """Nothing here downloads: open_clip is handed `local-dir:` for both the
    model and its tokenizer."""
    built = {}

    class _Model:
        logit_scale = None

        def eval(self):
            built["eval"] = True
            return self

        def to(self, device):
            built["device"] = device
            return self

        def half(self):
            built["half"] = True
            return self

    def create_model_and_transforms(name, **kwargs):
        built["model"] = (name, kwargs)
        return _Model(), None, "preprocess"

    def get_tokenizer(name, **kwargs):
        built["tokenizer"] = name
        return "tokenizer"

    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False),
        device=lambda kind: f"device:{kind}",
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(
        sys.modules,
        "open_clip",
        types.SimpleNamespace(
            create_model_and_transforms=create_model_and_transforms,
            get_tokenizer=get_tokenizer,
        ),
    )

    encoder = sut.OpenClipEncoder.load(tmp_path)

    assert built["model"] == (f"local-dir:{tmp_path}", {})
    assert built["tokenizer"] == f"local-dir:{tmp_path}"
    assert (built["eval"], built["device"]) == (True, "device:cpu")
    assert "half" not in built, "fp16 only on a GPU"
    assert encoder.device == "device:cpu"


# -- one prediction ---------------------------------------------------------------------


def _image(box=(150, 100, 250, 200)) -> PredictSpeciesImage:
    return PredictSpeciesImage(
        capture_id=uuid.uuid4(),
        headtail_prediction_id=uuid.uuid4(),
        jpeg=ObjectRef(bucket="labels", key="tenants/x/preprocess_headtail_jpeg/a.JPG"),
        mask_bbox=list(box),
    )


def test_a_prediction_records_top1_margin_top5_and_provenance():
    image = _image()
    result = predict_species(
        _frame_jpeg(), image, [HOGFISH, SNAPPER], _classifier(_FakeEncoder())
    )

    assert (result.capture_id, result.headtail_prediction_id) == (
        image.capture_id,
        image.headtail_prediction_id,
    )
    assert (result.status, result.predicted_choice) == ("predicted", HOGFISH.choice)
    assert result.top1_probability > 0.99 and result.margin > 0.99
    assert [s.choice for s in result.top5] == [HOGFISH.choice, SNAPPER.choice]
    assert result.predictor_version == SPECIES_PREDICTOR_VERSION
    assert result.model_id == "bioclip/2.5-vith14@0123456789ab"


def test_an_undecodable_frame_is_an_abstention_with_provenance():
    """Recorded, not raised: the cohort selects on a row's absence."""
    result = predict_species(
        b"not a jpeg", _image(), [HOGFISH], _classifier(_FakeEncoder())
    )

    assert result.status == "decode_failed"
    assert result.predicted_choice is None and result.top5 == []
    assert result.predictor_version == SPECIES_PREDICTOR_VERSION


def test_the_fallback_stamps_its_own_version():
    result = predict_species(
        _frame_jpeg(), _image(), [HOGFISH],
        _classifier(_FakeEncoder(), predictor_version=-1, model_id="bioclip/2@x"),
    )  # fmt: skip
    assert (result.predictor_version, result.model_id) == (-1, "bioclip/2@x")

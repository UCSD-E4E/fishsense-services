"""BioCLIP species pre-annotation, orchestrator side: the candidates, the
setting that keeps it off, and the Label Studio prediction it becomes.

New in v2 (no v1 counterpart). The classifier is ported from
coral-gardeners-fish-detector@67c8627; what is pinned here is FishSense's own
use of it:

* **the candidates are the species labeling config's "Fish" leaves**, each
  given to BioCLIP by its scientific name, less "Unidentifiable (Cannot see)"
  and "Other (Identifiable but Nontarget)". Cross-checked against
  coral-gardeners' resources/top25.yaml `little_cayman` list: REEF's 13
  target species;
* **it ships disabled** (`FISHSENSE_SPECIES_PREDICTION_ENABLED`, default
  false) until an accuracy evaluation on FishSense frames says otherwise;
* **open set**: below a threshold (a named setting, to be tuned by that
  evaluation) the suggestion is "Other (Identifiable but Nontarget)";
* **pre-annotation only**: the suggestion is a Label Studio *prediction* on
  the species Taxonomy, which the sync never reads -- a human confirms every
  label.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from fishsense_services_api.species_prediction_store import (
    CurrentSpeciesPrediction,
    LiveSpeciesTask,
)
from fishsense_services_contracts.species_prediction import SPECIES_PREDICTOR_VERSION
from fishsense_services_orchestrator.labels.label_studio import LabelStudioTask
from fishsense_services_orchestrator.species.labeling import (
    SPECIES_LABELING_CONFIG_XML,
)
from fishsense_services_orchestrator.species.parsing import (
    parse_results,
    species_sync_from_task,
)
from fishsense_services_orchestrator.species_predict import labeling as sut
from fishsense_services_orchestrator.species_predict.candidates import (
    OTHER_CHOICE,
    UNIDENTIFIABLE_CHOICE,
    species_candidates,
)
from fishsense_services_orchestrator.species_predict.settings import (
    DEFAULT_OTHER_THRESHOLD,
    SpeciesPredictionSettings,
)

#: coral-gardeners-fish-detector@67c8627 resources/top25.yaml `little_cayman`
#: ("Reference: REEF species of interest"), in its order.
LITTLE_CAYMAN = [
    "Lachnolaimus maximus",
    "Mycteroperca bonaci",
    "Epinephelus itajara",
    "Epinephelus striatus",
    "Epinephelus morio",
    "Ocyurus chrysurus",
    "Lutjanus griseus",
    "Lutjanus analis",
    "Scarus coeruleus",
    "Scarus coelestinus",
    "Scarus guacamaia",
    "Sparisoma viride",
    "Mycteroperca interstitialis",
]
HOGFISH = "Fish, Hogfish (Lachnolaimus maximus)"


# -- the candidates ----------------------------------------------------------------------


def test_the_candidates_are_coral_gardeners_little_cayman_list():
    assert [c.scientific_name for c in species_candidates()] == LITTLE_CAYMAN


def test_each_candidate_is_the_full_taxonomy_value_a_labeler_would_pick():
    first = species_candidates()[0]
    assert (first.choice, first.scientific_name) == (HOGFISH, "Lachnolaimus maximus")


def test_the_two_non_species_leaves_are_not_candidates():
    choices = {c.choice for c in species_candidates()}
    assert UNIDENTIFIABLE_CHOICE not in choices and OTHER_CHOICE not in choices


def test_the_non_species_leaves_are_the_configs_own():
    """Spelled as the XML spells them, or a suggestion of "Other" would name
    nothing selectable."""
    assert OTHER_CHOICE == "Fish, Other (Identifiable but Nontarget)"
    assert UNIDENTIFIABLE_CHOICE == "Fish, Unidentifiable (Cannot see)"
    for leaf in (OTHER_CHOICE, UNIDENTIFIABLE_CHOICE):
        assert (
            f'<Choice value="{leaf.split(", ", 1)[1]}"/>' in SPECIES_LABELING_CONFIG_XML
        )


def test_a_new_fish_leaf_becomes_a_candidate():
    """Derived, not listed: adding a species to the config adds it here."""
    xml = SPECIES_LABELING_CONFIG_XML.replace(
        '<Choice value="Unidentifiable (Cannot see)"/>',
        '<Choice value="Bar Jack (Caranx ruber)"/>\n'
        '      <Choice value="Unidentifiable (Cannot see)"/>',
    )
    assert species_candidates(xml)[-1].choice == "Fish, Bar Jack (Caranx ruber)"


def test_only_the_fish_branch_is_read():
    """Fish models and calibration targets are not species."""
    names = {c.scientific_name for c in species_candidates()}
    assert not {"Weasly Fish", "Ruler", "Laser on slate"} & names


# -- ships disabled -----------------------------------------------------------------------


def test_it_ships_disabled(monkeypatch):
    """Enabled only after the accuracy evaluation on FishSense frames."""
    monkeypatch.delenv("FISHSENSE_SPECIES_PREDICTION_ENABLED", raising=False)
    assert SpeciesPredictionSettings().enabled is False


def test_it_is_enabled_from_the_environment(monkeypatch):
    monkeypatch.setenv("FISHSENSE_SPECIES_PREDICTION_ENABLED", "true")
    monkeypatch.setenv("FISHSENSE_SPECIES_PREDICTION_OTHER_THRESHOLD", "0.7")
    settings = SpeciesPredictionSettings()
    assert (settings.enabled, settings.other_threshold) == (True, 0.7)


def test_the_default_threshold_is_conservative(monkeypatch):
    """At least an even chance before a target species is named."""
    monkeypatch.delenv("FISHSENSE_SPECIES_PREDICTION_OTHER_THRESHOLD", raising=False)
    assert SpeciesPredictionSettings().other_threshold == DEFAULT_OTHER_THRESHOLD
    assert DEFAULT_OTHER_THRESHOLD >= 0.5


@pytest.mark.parametrize("value", ["0", "-0.1", "1.5"])
def test_a_threshold_is_a_probability(monkeypatch, value):
    monkeypatch.setenv("FISHSENSE_SPECIES_PREDICTION_OTHER_THRESHOLD", value)
    with pytest.raises(ValidationError):
        SpeciesPredictionSettings()


# -- the suggestion ---------------------------------------------------------------------


def _prediction(n=1, *, choice=HOGFISH, p=0.9, status="predicted",
                version=SPECIES_PREDICTOR_VERSION) -> CurrentSpeciesPrediction:  # fmt: skip
    scored = status == "predicted"
    return CurrentSpeciesPrediction(
        id=uuid.uuid4(), capture_id=uuid.UUID(int=n), status=status,
        predicted_choice=choice if scored else None,
        top1_probability=p if scored else None, margin=0.5 if scored else None,
        top5=[{"choice": choice, "probability": p}] if scored else [],
        predictor_version=version, model_id="bioclip/2.5-vith14@x",
    )  # fmt: skip


def test_a_confident_top1_is_suggested():
    assert sut.suggested_choice(_prediction(p=0.8), 0.5) == HOGFISH


def test_below_the_threshold_the_suggestion_is_other():
    assert sut.suggested_choice(_prediction(p=0.49), 0.5) == OTHER_CHOICE


def test_at_the_threshold_the_top1_stands():
    assert sut.suggested_choice(_prediction(p=0.5), 0.5) == HOGFISH


def test_an_abstention_suggests_nothing():
    assert sut.suggested_choice(_prediction(status="decode_failed"), 0.5) is None
    assert sut.prediction_annotations(_prediction(status="decode_failed"), 0.5) == []
    assert sut.prediction_annotations(None, 0.5) == []


def test_the_prediction_is_a_taxonomy_result_the_sync_parser_reads_back():
    """The species control is a `<Taxonomy name="species">`, and a result's
    value is a path. `parse_results` is the definition of record for what a
    result means (the sentinel pre-annotation's rule, test_species_
    preannotation.py): read back, it must name the suggested choice."""
    (body,) = sut.prediction_annotations(_prediction(), 0.5)

    assert body["result"] == [
        {
            "from_name": "species",
            "to_name": "image",
            "type": "taxonomy",
            "value": {"taxonomy": [["Fish", "Hogfish (Lachnolaimus maximus)"]]},
        }
    ]
    assert parse_results(body)["content_of_image"] == HOGFISH


def test_other_round_trips_too():
    (body,) = sut.prediction_annotations(_prediction(p=0.1), 0.5)
    assert parse_results(body)["content_of_image"] == OTHER_CHOICE


def test_the_controls_are_the_configs():
    """A mismatched name makes Label Studio drop the prediction silently."""
    assert '<Taxonomy name="species" toName="image"' in SPECIES_LABELING_CONFIG_XML
    assert '<Image name="image"' in SPECIES_LABELING_CONFIG_XML


class TestTheTagIsAnIdempotencyKey:
    """The backfill dedupes on (task, model_version), and the project shows
    the version its `model_version` names."""

    def test_it_names_the_rows_version_and_the_threshold(self):
        assert sut.species_model_version_tag(1, 0.5) == "bioclip-v1 other<0.5"

    def test_a_fallback_row_is_tagged_as_the_fallback(self):
        (body,) = sut.prediction_annotations(_prediction(version=-1), 0.5)
        assert body["model_version"] == sut.species_model_version_tag(-1, 0.5)

    def test_a_new_threshold_is_a_new_suggestion(self):
        """The threshold is applied at seed time, so retuning it changes what
        a task is shown: a new tag, so the backfill attaches it."""
        assert sut.species_model_version_tag(1, 0.5) != sut.species_model_version_tag(
            1, 0.6
        )


# -- pre-annotation only ------------------------------------------------------------------


def test_a_task_carrying_only_a_prediction_syncs_as_unlabelled():
    """The sync reads annotations, never predictions: a suggestion nobody
    confirmed writes nothing, and completes nothing."""
    (body,) = sut.prediction_annotations(_prediction(), 0.5)
    task = LabelStudioTask(id=901, payload={"predictions": [body]})

    sync = species_sync_from_task(task)

    assert sync.completed is False
    assert sync.content_of_image is None


# -- which tasks the backfill attaches to --------------------------------------------------


def _task(n, *, task=None, project=70, completed=False):
    return LiveSpeciesTask(
        capture_id=uuid.UUID(int=n), ls_project_id=project,
        ls_task_id=task or 900 + n, completed=completed,
    )  # fmt: skip


def test_attaches_a_placeable_prediction_to_an_incomplete_task():
    targets = sut.select_attach_targets(
        [_prediction(1), _prediction(2, status="decode_failed"), _prediction(3)],
        [_task(1), _task(2), _task(3, completed=True)],
        judged=set(),
    )
    assert targets == {uuid.UUID(int=1): (901, 70)}


def test_a_human_judgement_is_never_replaced_by_a_models():
    """A sentinel's imported judgement is already the task's prediction."""
    targets = sut.select_attach_targets(
        [_prediction(1)], [_task(1)], judged={uuid.UUID(int=1)}
    )
    assert targets == {}


def test_the_first_task_of_a_capture_wins():
    targets = sut.select_attach_targets(
        [_prediction(1)], [_task(1, task=11), _task(1, task=12, project=71)], set()
    )
    assert targets == {uuid.UUID(int=1): (11, 70)}

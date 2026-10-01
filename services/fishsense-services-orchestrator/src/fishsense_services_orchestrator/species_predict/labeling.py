"""A species prediction in Label Studio: the suggestion, its tag, its tasks.

New in v2 (no v1 counterpart). **Pre-annotation only**: BioCLIP's answer
becomes a Label Studio *prediction* on the species task -- shown to the
labeler as a suggestion, which they confirm or change -- never an annotation
and never a species label. The sync (`species.parsing`) reads annotations
only, so a suggestion nobody confirmed writes nothing.

The prediction is a result on the species `<Taxonomy name="species"
toName="image">`, whose value is a path: `["Fish", "Hogfish (Lachnolaimus
maximus)"]` (the shape `species.preannotation` seeds a sentinel's judgement
in, and `species.parsing.parse_results` reads back).

**Open set**: BioCLIP scores a closed list, so a fish outside it still gets a
top-1. Below `SpeciesPredictionSettings.other_threshold` the suggestion is
"Other (Identifiable but Nontarget)" instead. It is applied here, at seed
time, not by the processor, so it can be retuned against rows already
collected -- and it is part of the tag, so a new value is attached as a new
suggestion.
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any, Iterable, Mapping

from fishsense_services_api.species_prediction_store import (
    CurrentSpeciesPrediction,
    LiveSpeciesTask,
    SpeciesPredictionState,
)
from fishsense_services_orchestrator.species.preannotation import (
    PREANNOTATION_MODEL_VERSION,
)
from fishsense_services_orchestrator.species_predict.candidates import (
    OTHER_CHOICE,
    SPECIES_CONTROL,
)

__all__ = [
    "prediction_annotations",
    "project_tags",
    "select_attach_targets",
    "species_model_version_tag",
    "suggested_choice",
    "tags_by_project",
]

_IMAGE_OBJECT = "image"


def species_model_version_tag(predictor_version: int, other_threshold: float) -> str:
    """The Label Studio `model_version` of a suggestion: the row's own
    version (a fallback row is tagged as one) and the threshold. It is the
    backfill's idempotency key, so it names the behaviour and nothing else."""
    return f"bioclip-v{predictor_version} other<{other_threshold:g}"


def suggested_choice(
    prediction: CurrentSpeciesPrediction, other_threshold: float
) -> str | None:
    """What a labeler is shown: the top-1, or "Other" below the threshold;
    None for an abstention."""
    if prediction.status != "predicted" or prediction.predicted_choice is None:
        return None
    if (prediction.top1_probability or 0.0) < other_threshold:
        return OTHER_CHOICE
    return prediction.predicted_choice


def prediction_annotations(
    prediction: CurrentSpeciesPrediction | None, other_threshold: float
) -> list[dict[str, Any]]:
    """The task's Label Studio `predictions`: one suggestion on the species
    Taxonomy, or [] when there is nothing to suggest."""
    choice = (
        None if prediction is None else suggested_choice(prediction, other_threshold)
    )
    if choice is None:
        return []
    return [
        {
            "model_version": species_model_version_tag(
                prediction.predictor_version, other_threshold
            ),
            "result": [
                {
                    "from_name": SPECIES_CONTROL,
                    "to_name": _IMAGE_OBJECT,
                    "type": "taxonomy",
                    "value": {"taxonomy": [choice.split(", ")]},
                }
            ],
        }
    ]


def select_attach_targets(
    predictions: Iterable[CurrentSpeciesPrediction],
    tasks: Iterable[LiveSpeciesTask],
    judged: set[uuid.UUID],
) -> dict[uuid.UUID, tuple[int, int]]:
    """`capture -> (task, project)` for the backfill: a prediction with
    something to suggest, and an incomplete live task. A capture whose task
    already carries a human's imported judgement (`judged`, the sentinel
    pre-annotation) is left to it. The first such task per capture wins."""
    placeable = {p.capture_id for p in predictions if p.status == "predicted"}
    targets: dict[uuid.UUID, tuple[int, int]] = {}
    for task in tasks:
        if task.completed or task.capture_id in judged:
            continue
        if task.capture_id not in placeable or task.capture_id in targets:
            continue
        targets[task.capture_id] = (int(task.ls_task_id), int(task.ls_project_id))
    return targets


def tags_by_project(
    tasks: Iterable[LiveSpeciesTask],
    tag_of: Mapping[uuid.UUID, str],
) -> dict[int, Counter]:
    """How many of each project's incomplete live tasks carry each tag, for
    `ensure_project_shows_predictions`: `tag_of` maps a capture to the tag of
    the prediction its task carries (a model's, or a sentinel's)."""
    counts: dict[int, Counter] = {}
    for task in tasks:
        tag = tag_of.get(task.capture_id)
        if task.completed or tag is None:
            continue
        counts.setdefault(int(task.ls_project_id), Counter())[tag] += 1
    return counts


def project_tags(
    state: SpeciesPredictionState,
    judged: set[uuid.UUID],
    other_threshold: float,
) -> dict[int, Counter]:
    """Per project, how many incomplete tasks carry each tag: a human's
    imported judgement where there is one, else the model's suggestion."""
    tag_of = {capture_id: PREANNOTATION_MODEL_VERSION for capture_id in judged}
    for prediction in state.predictions:
        if prediction.capture_id in tag_of:
            continue
        annotations = prediction_annotations(prediction, other_threshold)
        if annotations:
            tag_of[prediction.capture_id] = annotations[0]["model_version"]
    return tags_by_project(state.tasks, tag_of)

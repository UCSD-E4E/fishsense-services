"""Which frames species populate tasks, and what a task carries.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/
populate_species_label_studio_project_activity.py (`_select_target_images`,
`_sentinel_judgements`, `_build_task`). Behaviour is v1's:

* a frame is a target when it carries a valid laser (the catalog's query), has
  no completed species row (in any project, sentinels included -- so a
  completed sentinel takes its frame out for good, which is why such a
  sentinel is refused as a judgement), and has no live row in THIS project;
* a sentinel's judgement becomes the task's *prediction*, never an
  annotation: an annotation reads as completed human work, and the sync would
  write it back as a labeler's answer.

v2 changes: frames are captures; a task's image is the located JPEG
(`labels.populate.TaskImage`), and its `image_id` the capture's `number`.
"""

from __future__ import annotations

import uuid

from temporalio import activity

from fishsense_services_api.species_store import SpeciesCapture, SpeciesLabelRow
from fishsense_services_orchestrator.labels.populate import TaskImage, build_task_data
from fishsense_services_orchestrator.species.preannotation import build_prediction

__all__ = ["build_species_task", "select_target_captures", "sentinel_judgements"]


def select_target_captures(
    candidates: list[SpeciesCapture],
    species_labels: list[SpeciesLabelRow],
    project_id: int,
) -> list[SpeciesCapture]:
    """The laser-valid frames that need a fresh species task in `project_id`.

    Idempotent, so safe to schedule: a frame whose species label is already
    completed is done, and one with a live row in THIS project already has
    its task. A frame whose only row is superseded, or in another (stale)
    project, is still selected: that's the migrate-onto-the-current-project
    path.
    """
    completed = {label.capture_id for label in species_labels if label.completed}
    in_project = {
        label.capture_id
        for label in species_labels
        if label.ls_project_id == project_id and not label.superseded
    }
    return [
        capture
        for capture in candidates
        if capture.capture_id not in completed and capture.capture_id not in in_project
    ]


def sentinel_judgements(
    species_labels: list[SpeciesLabelRow],
) -> dict[uuid.UUID, SpeciesLabelRow]:
    """Capture -> the sentinel row carrying a pre-existing species judgement.

    Sentinels are rows with no Label Studio project -- what every preprocess
    cohort reads as "no label", which lets an import of judgements sit in the
    database without taking its dives out of the labelling flow. One that
    says nothing contributes nothing.
    """
    out: dict[uuid.UUID, SpeciesLabelRow] = {}
    for label in species_labels:
        if label.ls_project_id is not None or label.capture_id in out:
            continue
        if label.completed or label.superseded:
            # A completed sentinel takes its frame out of population for good
            # (the completed set has no project filter); treating it as "not a
            # judgement" keeps that from looking like a successful import.
            activity.logger.warning(
                "capture %s has a %s judgement sentinel; ignoring it as a "
                "pre-annotation source (a completed sentinel removes the image "
                "from population)",
                label.capture_id,
                "completed" if label.completed else "superseded",
            )
            continue
        if build_prediction(label) is not None:
            out[label.capture_id] = label
    return out


def build_species_task(
    image: TaskImage, judgement: SpeciesLabelRow | None = None
) -> dict:
    """A Label Studio task: the image under both `image` and `img` (legacy
    configs use either), and a judgement, if any, as its prediction."""
    prediction = build_prediction(judgement) if judgement is not None else None
    return {
        "data": build_task_data(image),
        "predictions": [prediction] if prediction is not None else [],
        "annotations": [],
    }

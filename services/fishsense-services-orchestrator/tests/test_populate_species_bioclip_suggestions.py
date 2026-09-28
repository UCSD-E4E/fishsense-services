"""Species populate seeds BioCLIP's suggestion -- only when enabled.

New in v2 (no v1 counterpart). v1's species populate
(test_populate_species_label_studio_project.py) seeds a task's prediction
from a sentinel's imported judgement only. Pinned here:

* **ships disabled**: with `FISHSENSE_SPECIES_PREDICTION_ENABLED` unset,
  populate never reads a species prediction, and a task carries none;
* enabled, a task is seeded with the capture's current BioCLIP suggestion
  as a *prediction* -- never an annotation, so a human confirms every label;
* a human's imported judgement wins over the model's: it stays the task's
  only prediction;
* below the threshold the suggestion is "Other (Identifiable but
  Nontarget)";
* the project's `model_version` is pointed at the tag most of its tasks
  carry, or Label Studio shows none of them.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import List
from unittest.mock import MagicMock

from temporalio.testing import ActivityEnvironment

from fishsense_services_api.species_prediction_store import (
    CurrentSpeciesPrediction,
    LiveSpeciesTask,
    SpeciesPredictionState,
)
from fishsense_services_api.species_store import SpeciesPopulationFacts
from fishsense_services_orchestrator.labels.label_studio import LabelStudioClient
from fishsense_services_orchestrator.species.activities import SpeciesActivities
from fishsense_services_orchestrator.species.contracts import SpeciesTarget
from fishsense_services_orchestrator.species.preannotation import (
    PREANNOTATION_MODEL_VERSION,
)
from fishsense_services_orchestrator.species_predict.settings import (
    SpeciesPredictionSettings,
)

from ._species import (
    DIVE,
    TENANT,
    FakeSpeciesCatalog,
    FakeStore,
    capture,
    image,
    species,
)

TARGET = SpeciesTarget(TENANT, DIVE)
PROJECT = 70
HOGFISH = "Fish, Hogfish (Lachnolaimus maximus)"
TAG = "bioclip-v1 other<0.5"


class FakeSdk:
    """Label Studio: an import creates listable tasks; projects are gettable
    (for `ensure_project_shows_predictions`) and updatable."""

    def __init__(self, task_ids: List[int], *, model_version=None):
        self._ids = iter(task_ids)
        self.listed: list = []
        self.imported: List[dict] = []
        self.updates: List[dict] = []
        project = SimpleNamespace(
            id=PROJECT, title="Reef dive #42 - Species Labeling",
            model_version=model_version,
        )  # fmt: skip
        self.projects = MagicMock(
            import_tasks=MagicMock(side_effect=self._import),
            update=MagicMock(side_effect=lambda id, **f: self.updates.append(f)),
            get=MagicMock(return_value=project),
        )
        self.tasks = MagicMock(list=MagicMock(side_effect=lambda project: self.listed))

    def _import(self, project_id, request, return_task_ids=False):
        # pylint: disable=unused-argument
        for task in request:
            self.imported.append(task)
            self.listed.append(SimpleNamespace(id=next(self._ids), data=task["data"]))


class FakePredictions:
    """`SpeciesPredictionCatalog.species_prediction_state`, recording calls.
    After the import, the tasks it read are the ones populate recorded."""

    def __init__(self, predictions, species_catalog):
        self.predictions = list(predictions)
        self.species_catalog = species_catalog
        self.calls = 0

    async def species_prediction_state(self, tenant_id, dive_id):
        self.calls += 1
        tasks = [
            LiveSpeciesTask(capture_id, project, task, False)
            for capture_id, project, task, _ in self.species_catalog.recorded
        ]
        return SpeciesPredictionState(42, self.predictions, tasks)


def _prediction(n, *, p=0.9):
    return CurrentSpeciesPrediction(
        id=uuid.uuid4(), capture_id=capture(n), status="predicted",
        predicted_choice=HOGFISH, top1_probability=p, margin=0.5,
        top5=[{"choice": HOGFISH, "probability": p}], predictor_version=1,
        model_id="bioclip/2.5-vith14@x",
    )  # fmt: skip


async def _populate(captures, labels, predictions, *, enabled, task_ids,
                    model_version=None):  # fmt: skip
    catalog = FakeSpeciesCatalog(
        population=SpeciesPopulationFacts(
            candidates=list(captures), species_labels=list(labels)
        )
    )
    fake_predictions = FakePredictions(predictions, catalog)
    sdk = FakeSdk(task_ids, model_version=model_version)
    kwargs = {}
    if enabled is not None:
        kwargs["prediction_settings"] = SpeciesPredictionSettings(
            enabled=enabled, other_threshold=0.5
        )
    activities = SpeciesActivities(
        catalog=catalog,
        store=FakeStore(),
        label_studio_factory=lambda: LabelStudioClient(sdk),
        predictions=fake_predictions,
        **kwargs,
    )
    await ActivityEnvironment().run(
        activities.populate_species_label_studio_project, TARGET, PROJECT
    )
    return sdk, fake_predictions


def _suggestion(task):
    return [
        (p["model_version"], p["result"][0]["value"]["taxonomy"])
        for p in task["predictions"]
    ]


async def test_by_default_no_model_prediction_is_read_or_seeded():
    sdk, predictions = await _populate(
        [image(1, "a")], [], [_prediction(1)], enabled=None, task_ids=[501]
    )

    assert predictions.calls == 0
    assert sdk.imported[0]["predictions"] == []
    assert not [u for u in sdk.updates if "model_version" in u]


async def test_disabled_is_the_same():
    sdk, predictions = await _populate(
        [image(1, "a")], [], [_prediction(1)], enabled=False, task_ids=[501]
    )
    assert predictions.calls == 0
    assert sdk.imported[0]["predictions"] == []


async def test_enabled_a_task_carries_the_suggestion_and_the_project_shows_it():
    sdk, _ = await _populate(
        [image(1, "a"), image(2, "b")], [], [_prediction(1), _prediction(2, p=0.1)],
        enabled=True, task_ids=[501, 502],
    )  # fmt: skip

    first, second = sdk.imported
    assert _suggestion(first) == [(TAG, [["Fish", "Hogfish (Lachnolaimus maximus)"]])]
    assert _suggestion(second) == [
        (TAG, [["Fish", "Other (Identifiable but Nontarget)"]])
    ], "below the threshold"
    assert all(not t["annotations"] for t in sdk.imported), "never an annotation"
    assert {"model_version": TAG} in sdk.updates


async def test_a_capture_with_no_prediction_is_seeded_without_one():
    sdk, _ = await _populate([image(1, "a")], [], [], enabled=True, task_ids=[501])
    assert sdk.imported[0]["predictions"] == []


async def test_a_human_judgement_wins_over_the_models():
    judgement = species(1, project=None, content_of_image=HOGFISH)

    sdk, _ = await _populate(
        [image(1, "a")], [judgement], [_prediction(1)], enabled=True, task_ids=[501]
    )

    (task,) = sdk.imported
    assert [p["model_version"] for p in task["predictions"]] == [
        PREANNOTATION_MODEL_VERSION
    ]
    assert {"model_version": PREANNOTATION_MODEL_VERSION} in sdk.updates

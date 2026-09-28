"""BioCLIP species pre-annotation, the orchestrator's activities: select,
resolve, persist, and the backfill onto existing species tasks.

New in v2 (no v1 counterpart), built as head/tail prediction's are
(test_headtail_activities.py, test_headtail_populate_activity.py), and pinned
here:

* the selector takes the best dive across every tenant served: never-predicted
  work first, then the oldest;
* the resolver hands the processor each fish's head/tail JPEG (where the
  object store found it; a JPEG not yet written is deferred), its kept mask's
  box, and the candidates from the species labeling config;
* a refusal of the processor's output is final (non-retryable): an unknown
  status, a choice outside the candidates, a missing provenance, or a store
  refusal;
* **the backfill attaches only when enabled**, onto incomplete tasks without a
  human's imported judgement, deduped on (task, tag), and points each dive-owned
  project's `model_version` at the tag most of its tasks carry;
* **pre-annotation only**: a suggestion is attached as a prediction, never an
  annotation (the store's test pins that persisting writes no label).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.species_prediction_store import (
    CurrentSpeciesPrediction,
    ForeignCapture,
    LiveSpeciesTask,
    SpeciesPredictCapture,
    SpeciesPredictionCandidate,
    SpeciesPredictionState,
)
from fishsense_services_api.species_store import SpeciesPopulationFacts
from fishsense_services_contracts.object_store import HEADTAIL_JPEG_FOLDER
from fishsense_services_contracts.species_prediction import (
    SPECIES_PREDICTOR_VERSION,
    SPECIES_STATUS_NO_UPGRADE_AVAILABLE,
    SpeciesPredictionResult,
    SpeciesScore,
)
from fishsense_services_orchestrator.labels.label_studio import LabelStudioClient
from fishsense_services_orchestrator.species.preannotation import (
    PREANNOTATION_MODEL_VERSION,
)
from fishsense_services_orchestrator.species_predict.activities import (
    SpeciesPredictActivities,
    SpeciesPredictTarget,
)
from fishsense_services_orchestrator.species_predict.candidates import (
    species_candidates,
)
from fishsense_services_orchestrator.species_predict.settings import (
    SpeciesPredictionSettings,
)

from ._species import LAYOUT, FakeSpeciesCatalog, species

T0 = datetime(2026, 9, 1, tzinfo=UTC)
LAB, REEF = uuid.uuid4(), uuid.uuid4()
DIVE = uuid.uuid4()
TARGET = SpeciesPredictTarget(LAB, DIVE)
HOGFISH = "Fish, Hogfish (Lachnolaimus maximus)"
TAG = "bioclip-v1 other<0.5"
PROJECT = 70
BOX = [100, 200, 300, 260]


def _cid(n):
    return uuid.UUID(int=n)


# -- the fakes ------------------------------------------------------------------------


class FakeCatalog:
    """`SpeciesPredictionCatalog`, in memory."""

    def __init__(self, *, candidates=None, captures=(), predictions=(), tasks=(),
                 number=42, refuse=None):  # fmt: skip
        self.candidates = candidates or {}
        self.captures = list(captures)
        self.state = SpeciesPredictionState(number, list(predictions), list(tasks))
        self.persisted = []
        self.refuse = refuse
        self.versions = []

    async def member_tenants(self):
        return [LAB, REEF]

    async def next_dive_for_species_prediction(self, tenant_id, *, predictor_version):
        self.versions.append(predictor_version)
        return self.candidates.get(tenant_id)

    async def species_predict_captures(self, tenant_id, dive_id, *, predictor_version):
        self.versions.append(predictor_version)
        return self.captures

    async def persist_species_predictions(self, tenant_id, dive_id, rows):
        if self.refuse:
            raise self.refuse
        self.persisted.extend(rows)
        return len(rows)

    async def species_prediction_state(self, tenant_id, dive_id):
        return self.state


class FakeStore:
    """Where the head/tail JPEGs are: checksum -> ref, or missing."""

    def __init__(self, present):
        self.present = present
        self.located = []

    async def locate_processed_jpeg(self, tenant_id, folder, checksum, *, from_v1):
        self.located.append((tenant_id, folder, checksum, from_v1))
        return self.present.get(checksum)


class FakeSdk:
    def __init__(self, *, title="Reef dive #42 - Species Labeling", model_version=None,
                 predictions=()):  # fmt: skip
        self.created = []
        self.updates = []
        self.listed = []
        self._predictions = list(predictions)
        project = SimpleNamespace(id=PROJECT, title=title, model_version=model_version)
        self.projects = SimpleNamespace(
            get=lambda id: project,  # pylint: disable=redefined-builtin
            update=lambda id, **fields: self.updates.append({"id": id, **fields}),
        )
        self.predictions = SimpleNamespace(list=self._list, create=self._create)

    def _list(self, project):
        self.listed.append(project)
        return self._predictions

    def _create(self, task, model_version, result):
        self.created.append({"task": task, "model_version": model_version,
                             "result": result})  # fmt: skip


def _activities(catalog, *, store=None, sdk=None, enabled=True, species_facts=()):
    species_catalog = FakeSpeciesCatalog(
        population=SpeciesPopulationFacts(
            candidates=[], species_labels=list(species_facts)
        )
    )
    return SpeciesPredictActivities(
        catalog=catalog,
        species_catalog=species_catalog,
        store=store or FakeStore({}),
        settings=SpeciesPredictionSettings(enabled=enabled, other_threshold=0.5),
        label_studio_factory=lambda: LabelStudioClient(sdk or FakeSdk()),
    )


# -- select ---------------------------------------------------------------------------


async def test_never_predicted_work_first_then_the_oldest_across_tenants():
    catalog = FakeCatalog(
        candidates={
            LAB: SpeciesPredictionCandidate(_cid(1), T0, never_predicted=False),
            REEF: SpeciesPredictionCandidate(_cid(2), T0 + timedelta(days=1), True),
        }
    )
    picked = await ActivityEnvironment().run(
        _activities(catalog).select_next_dive_for_species_prediction
    )
    assert picked == SpeciesPredictTarget(REEF, _cid(2))
    assert set(catalog.versions) == {SPECIES_PREDICTOR_VERSION}


async def test_nothing_to_predict_is_none():
    picked = await ActivityEnvironment().run(
        _activities(FakeCatalog()).select_next_dive_for_species_prediction
    )
    assert picked is None


# -- resolve --------------------------------------------------------------------------


def _capture(n, *, from_v1=False, existing=False):
    return SpeciesPredictCapture(
        capture_id=_cid(n), checksum=f"{n:032x}", from_v1=from_v1,
        headtail_prediction_id=_cid(100 + n), mask_bbox=BOX,
        has_existing_prediction=existing,
    )  # fmt: skip


async def test_resolves_each_fish_with_its_jpeg_box_and_the_candidates():
    ref = LAYOUT.legacy_processed_jpeg(HEADTAIL_JPEG_FOLDER, f"{1:032x}")
    store = FakeStore({f"{1:032x}": ref})
    catalog = FakeCatalog(captures=[_capture(1, from_v1=True, existing=True),
                                    _capture(2)])  # fmt: skip

    inputs = await ActivityEnvironment().run(
        _activities(catalog, store=store).resolve_species_predict_inputs, TARGET
    )

    assert (inputs.tenant_id, inputs.dive_id) == (LAB, DIVE)
    assert inputs.candidates == species_candidates()
    (image,) = inputs.images
    assert (image.capture_id, image.headtail_prediction_id, image.jpeg) == (
        _cid(1),
        _cid(101),
        ref,
    )
    assert (image.mask_bbox, image.has_existing_prediction) == (BOX, True)
    assert store.located == [
        (LAB, HEADTAIL_JPEG_FOLDER, f"{1:032x}", True),
        (LAB, HEADTAIL_JPEG_FOLDER, f"{2:032x}", False),
    ], "the head/tail stage's JPEG, where it is; the second is deferred"


# -- persist --------------------------------------------------------------------------


def _result(n=1, **overrides):
    values = {
        "capture_id": _cid(n), "headtail_prediction_id": _cid(100 + n),
        "status": "predicted", "predicted_choice": HOGFISH,
        "top1_probability": 0.9, "margin": 0.8,
        "top5": [SpeciesScore(choice=HOGFISH, probability=0.9)],
        "predictor_version": SPECIES_PREDICTOR_VERSION,
        "model_id": "bioclip/2.5-vith14@x",
    }  # fmt: skip
    values.update(overrides)
    return SpeciesPredictionResult(**values)


async def test_persist_appends_each_result_as_a_row():
    catalog = FakeCatalog()

    written = await ActivityEnvironment().run(
        _activities(catalog).persist_species_predictions,
        TARGET,
        [_result(1), _result(2, status="decode_failed", predicted_choice=None,
                             top1_probability=None, margin=None, top5=[])],
    )  # fmt: skip

    assert written == 2
    first = catalog.persisted[0]
    assert (first.capture_id, first.headtail_prediction_id, first.predicted_choice) == (
        _cid(1),
        _cid(101),
        HOGFISH,
    )
    assert first.top5 == [{"choice": HOGFISH, "probability": 0.9}]
    assert first.model_id == "bioclip/2.5-vith14@x"


@pytest.mark.parametrize(
    "bad",
    [
        {"status": SPECIES_STATUS_NO_UPGRADE_AVAILABLE},
        {"status": "confident"},
        {"predicted_choice": "Fish, Clownfish (Amphiprion ocellaris)"},
        {"top5": [SpeciesScore(choice="Fish, Nemo", probability=0.9)]},
        {"predictor_version": None},
        {"model_id": None},
    ],
    ids=["skip", "unknown-status", "foreign-choice", "foreign-top5", "no-version",
         "no-model"],
)  # fmt: skip
async def test_a_result_that_is_not_a_prediction_is_refused_for_good(bad):
    """PLAN.md §9.11: the processor's output is checked before it is a row,
    and a refusal does not pass on a retry."""
    catalog = FakeCatalog()
    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(
            _activities(catalog).persist_species_predictions,
            TARGET,
            [_result(**bad)],
        )
    assert (excinfo.value.type, excinfo.value.non_retryable) == (
        "InvalidPredictions",
        True,
    )
    assert not catalog.persisted


async def test_a_store_refusal_is_final_too():
    catalog = FakeCatalog(refuse=ForeignCapture("not the dive's"))
    with pytest.raises(ApplicationError) as excinfo:
        await ActivityEnvironment().run(
            _activities(catalog).persist_species_predictions, TARGET, [_result()]
        )
    assert excinfo.value.non_retryable is True


# -- the backfill ---------------------------------------------------------------------


def _prediction(n, *, p=0.9, status="predicted", version=SPECIES_PREDICTOR_VERSION):
    scored = status == "predicted"
    return CurrentSpeciesPrediction(
        id=uuid.uuid4(), capture_id=_cid(n), status=status,
        predicted_choice=HOGFISH if scored else None,
        top1_probability=p if scored else None, margin=0.5 if scored else None,
        top5=[], predictor_version=version, model_id="bioclip/2.5-vith14@x",
    )  # fmt: skip


def _task(n, *, completed=False):
    return LiveSpeciesTask(_cid(n), PROJECT, 900 + n, completed)


async def _backfill(catalog, sdk, **kwargs):
    return await ActivityEnvironment().run(
        _activities(catalog, sdk=sdk, **kwargs).backfill_species_predictions_for_dive,
        TARGET,
    )


async def test_disabled_attaches_nothing_and_touches_no_label_studio():
    """Ships disabled: predictions may be stored (an evaluation's run), but no
    labeler is shown one."""
    catalog = FakeCatalog(predictions=[_prediction(1)], tasks=[_task(1)])
    sdk = FakeSdk()

    assert await _backfill(catalog, sdk, enabled=False) == 0
    assert not sdk.created and not sdk.listed and not sdk.updates


async def test_attaches_suggestions_to_incomplete_tasks_and_shows_them():
    catalog = FakeCatalog(
        predictions=[_prediction(1), _prediction(2, p=0.2), _prediction(3),
                     _prediction(4, status="decode_failed")],
        tasks=[_task(1), _task(2), _task(3, completed=True), _task(4)],
    )  # fmt: skip
    sdk = FakeSdk()

    assert await _backfill(catalog, sdk) == 2

    assert [(c["task"], c["model_version"]) for c in sdk.created] == [
        (901, TAG),
        (902, TAG),
    ]
    assert sdk.created[0]["result"][0]["value"] == {
        "taxonomy": [["Fish", "Hogfish (Lachnolaimus maximus)"]]
    }
    assert sdk.created[1]["result"][0]["value"] == {
        "taxonomy": [["Fish", "Other (Identifiable but Nontarget)"]]
    }, "below the threshold, the suggestion is Other"
    assert sdk.updates == [{"id": PROJECT, "model_version": TAG}]


async def test_is_idempotent_on_task_and_tag():
    catalog = FakeCatalog(predictions=[_prediction(1), _prediction(2)],
                          tasks=[_task(1), _task(2)])  # fmt: skip
    sdk = FakeSdk(
        model_version=TAG,
        predictions=[SimpleNamespace(task=901, model_version=TAG)],
    )

    assert await _backfill(catalog, sdk) == 1
    assert [c["task"] for c in sdk.created] == [902]
    assert sdk.listed == [PROJECT], "one listing per project"
    assert not sdk.updates, "already showing it"


async def test_a_human_judgement_keeps_its_task_and_its_projects_majority():
    """A sentinel's imported judgement is the task's prediction already: the
    model's is not attached over it, and a project mostly showing judgements
    keeps showing them."""
    judged = [species(n, project=None, content_of_image=HOGFISH) for n in (1, 2)]
    catalog = FakeCatalog(
        predictions=[_prediction(n) for n in (1, 2, 3)],
        tasks=[_task(n) for n in (1, 2, 3)],
    )
    sdk = FakeSdk(model_version=PREANNOTATION_MODEL_VERSION)

    assert await _backfill(catalog, sdk, species_facts=judged) == 1
    assert [c["task"] for c in sdk.created] == [903]
    assert not sdk.updates, "2 judgements to 1 suggestion: the judgements stay shown"


async def test_leaves_a_shared_project_alone():
    catalog = FakeCatalog(predictions=[_prediction(1)], tasks=[_task(1)])
    sdk = FakeSdk(title="Species canonical #420")

    await _backfill(catalog, sdk)

    assert not sdk.updates


async def test_nothing_attachable_touches_no_label_studio():
    catalog = FakeCatalog(predictions=[_prediction(1, status="decode_failed")],
                          tasks=[_task(1)])  # fmt: skip
    sdk = FakeSdk()

    assert await _backfill(catalog, sdk) == 0
    assert not sdk.listed and not sdk.created

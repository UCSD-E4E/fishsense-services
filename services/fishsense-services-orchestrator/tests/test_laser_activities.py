"""The laser slice's orchestrator activities.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/ (test_select_next_high_priority_dive_for_laser_preprocessing_activity.py,
test_resolve_laser_preprocess_inputs_activity.py,
test_resolve_laser_predict_inputs_activity.py,
test_persist_laser_predictions_activity.py,
test_select_dives_needing_laser_population_activity.py,
test_create_laser_label_studio_project_activity.py,
test_populate_laser_label_studio_project_activity.py (the activity half),
test_backfill_laser_predictions_activity.py,
test_apply_laser_auto_accept_activity.py) and, for the writes the v1
data-worker made itself, the data-worker's gate, validator and remediation
activity tests. Names and reasons are v1's where the behaviour is.

v2 changes, pinned here:

* a selector takes the oldest candidate across every tenant the orchestrator
  serves (the clustering pattern);
* the resolvers hand the processor `ObjectRef`s: the staged raw frame, and
  where the JPEG goes -- v1's key for a migrated frame, so a redraw overwrites
  in place;
* the gate, the validator and remediation read and write here, around the
  processor's pure decision; a write the store refuses (a row outside the
  dive) is final, not retried;
* populate records `source` (`human`, or `auto_accept` for a frame imported
  already annotated), and the auto-accept apply marks the rows it annotated;
* remediation's apply refuses, writing nothing, if the dive changed since the
  plan it applies was made.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.laser_store import (
    DiveCamera,
    GateInputs,
    GateRow,
    LabelPopulation,
    LabelRow,
    LaserPopulation,
    PopulateItem,
    TaskTarget,
    TaskTargets,
)
from fishsense_services_api.laser_store import LASER_PREDICTOR_VERSION as STORE_VERSION
from fishsense_services_contracts.laser import (
    LASER_PREDICTOR_VERSION,
    DivePlan,
    LaserAutoAcceptResult,
    LaserAutoAcceptSummary,
    LaserFrameVerdict,
    LaserLineFit,
    LaserPredictionResult,
    LaserSupersede,
    LaserValidationResult,
    PlanLaserRemediationInput,
    SupersedeReason,
    laser_model_version_tag,
)
from fishsense_services_contracts.laser_region import (
    DEFAULT_LASER_BBOX,
    LASER_REGION_POLYGON,
)
from fishsense_services_orchestrator.labels.label_studio import LabelStudioPrediction
from fishsense_services_orchestrator.laser.activities import LaserActivities
from fishsense_services_orchestrator.laser.annotations import (
    LASER_LABELING_CONFIG_XML,
    LASER_PROJECT_TITLE_SUFFIX,
)
from fishsense_services_orchestrator.laser.contracts import (
    ClearReprocessFlags,
    DiveRemediationRequest,
    LaserTarget,
    RemediationTarget,
    ReviveLabels,
)

from ._laser_fakes import (
    D,
    K,
    FakeCatalog,
    FakeLabelProjects,
    FakeLabelStudio,
    FakeStore,
    ForeignRows,
    PopulationChanged,
    candidate,
    capture,
)

LAB, OTHER = uuid.UUID(int=1), uuid.UUID(int=2)
DIVE = uuid.UUID(int=10)
TARGET = LaserTarget(tenant_id=LAB, dive_id=DIVE)


def _activities(catalog=None, *, store=None, ls=None, projects=None, bot=0):
    return LaserActivities(
        catalog=catalog or FakeCatalog(tenants=[LAB]),
        store=store or FakeStore(),
        label_studio=ls or FakeLabelStudio(),
        label_projects=projects or FakeLabelProjects(),
        bot_user_id=bot,
    )


async def _run(fn, *args):
    return await ActivityEnvironment().run(fn, *args)


def test_the_store_and_the_contract_agree_on_the_stage_version():
    """The API package does not depend on the contract, so it repeats the
    number its cohorts compare against; they must never drift."""
    assert STORE_VERSION == LASER_PREDICTOR_VERSION


# -- selectors ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("selector", "key"),
    [
        ("select_next_dive_for_laser_preprocessing", "preprocess"),
        ("select_next_dive_for_laser_prediction", "predict"),
        ("select_next_dive_for_laser_auto_accept", "gate"),
    ],
)
async def test_a_selector_takes_the_oldest_candidate_across_tenants(selector, key):
    lab_dive, other_dive = uuid.UUID(int=11), uuid.UUID(int=12)
    catalog = FakeCatalog(
        tenants=[LAB, OTHER],
        next_dive={
            (key, LAB): candidate(lab_dive, age_days=1),
            (key, OTHER): candidate(other_dive, age_days=5),
        },
    )

    target = await _run(getattr(_activities(catalog), selector))

    assert target == LaserTarget(tenant_id=OTHER, dive_id=other_dive)


async def test_a_selector_returns_none_when_no_tenant_has_a_candidate():
    catalog = FakeCatalog(tenants=[LAB, OTHER])

    assert (
        await _run(_activities(catalog).select_next_dive_for_laser_prediction) is None
    )


async def test_the_populate_cohort_is_every_dive_of_every_tenant_oldest_first():
    a, b, c = (uuid.UUID(int=i) for i in (21, 22, 23))
    catalog = FakeCatalog(
        tenants=[LAB, OTHER],
        dive_lists={
            ("populate", LAB): [candidate(a, age_days=2), candidate(b, age_days=0)],
            ("populate", OTHER): [candidate(c, age_days=1)],
        },
    )

    targets = await _run(_activities(catalog).select_dives_needing_laser_population)

    assert [t.dive_id for t in targets] == [a, c, b]


# -- stage 0.1 ------------------------------------------------------------------


async def test_the_preprocess_input_names_the_staged_frame_and_the_jpeg_target():
    """A migrated frame's JPEG is written over v1's (a redraw keeps the URL
    Label Studio's task holds); a new frame's under the tenant."""
    migrated = capture(1, checksum="a" * 32, from_v1=True)
    new = capture(2, checksum="b" * 32)
    catalog = FakeCatalog(
        tenants=[LAB], captures=[migrated, new], camera=DiveCamera(K, D)
    )
    store = FakeStore(present={"a" * 32})

    inputs = await _run(
        _activities(catalog, store=store).resolve_laser_preprocess_inputs, TARGET
    )

    by_capture = {i.capture_id: i for i in inputs.images}
    assert by_capture[migrated.capture_id].jpeg.key == (
        "fishsense-lite/preprocess_jpeg/" + "a" * 32 + ".JPG"
    )
    assert by_capture[new.capture_id].jpeg.key == (
        f"tenants/{LAB}/preprocess_jpeg/" + "b" * 32 + ".JPG"
    )
    assert by_capture[new.capture_id].raw.key == f"tenants/{LAB}/raw/{'b' * 32}.ORF"
    assert inputs.camera_matrix == K and inputs.distortion_coefficients == D
    assert inputs.bbox == DEFAULT_LASER_BBOX
    assert inputs.laser_region == LASER_REGION_POLYGON


async def test_the_bbox_is_the_region_polygons_bounding_box():
    xs = [v[0] for v in LASER_REGION_POLYGON]
    ys = [v[1] for v in LASER_REGION_POLYGON]
    assert DEFAULT_LASER_BBOX == [min(xs), min(ys), max(xs), max(ys)]


async def test_a_dive_without_intrinsics_is_refused():
    """v1 raised; the dive stays in the cohort (PLAN.md §9.16 is the fix for
    that wedge, not this port)."""
    catalog = FakeCatalog(tenants=[LAB], captures=[capture(1)], camera=None)

    with pytest.raises(ApplicationError) as raised:
        await _run(_activities(catalog).resolve_laser_preprocess_inputs, TARGET)
    assert raised.value.type == "DiveHasNoCamera"


async def test_no_work_needs_no_camera():
    """An empty resolution must still come back, so the parent lowers the
    flags: the flag is the one cohort term that never goes false by itself."""
    catalog = FakeCatalog(tenants=[LAB], captures=[], camera=None)

    inputs = await _run(_activities(catalog).resolve_laser_preprocess_inputs, TARGET)

    assert inputs.images == []


async def test_clearing_flags_passes_the_scope_through():
    catalog = FakeCatalog(tenants=[LAB])
    activities = _activities(catalog)

    whole = await _run(
        activities.clear_laser_reprocess_flags, ClearReprocessFlags(target=TARGET)
    )
    none = await _run(
        activities.clear_laser_reprocess_flags,
        ClearReprocessFlags(target=TARGET, capture_ids=[]),
    )

    assert (whole, none) == (3, 0)
    assert [c[3] for c in catalog.calls] == [None, []]


# -- laser prediction -------------------------------------------------------------


async def test_the_predict_input_carries_the_staged_frames_and_the_region():
    catalog = FakeCatalog(tenants=[LAB], captures=[capture(1)], camera=DiveCamera(K, D))

    inputs = await _run(_activities(catalog).resolve_laser_predict_inputs, TARGET)

    (image,) = inputs.images
    assert image.raw.key == f"tenants/{LAB}/raw/{capture(1).checksum}.ORF"
    assert inputs.wavelength is None  # the detector's unknown-wavelength channel
    assert inputs.laser_region == LASER_REGION_POLYGON


async def test_persists_each_prediction():
    catalog = FakeCatalog(tenants=[LAB])
    results = [
        LaserPredictionResult(capture_id=capture(1).capture_id, x=1.0, y=2.0,
                              confidence=0.9, width=4000, height=3000, color="red",
                              color_margin=30.0, predictor_version=2,
                              checkpoint="run3_epoch_021.pt", core_version="4.1.0"),
        LaserPredictionResult(capture_id=capture(2).capture_id, confidence=0.1,
                              rejected_out_of_region=True, predictor_version=2),
    ]  # fmt: skip

    written = await _run(
        _activities(catalog).persist_laser_predictions, TARGET, results
    )

    _, _, _, persisted = catalog.calls[0]
    assert written == 2
    assert [(p.capture_id, p.x, p.color, p.rejected_out_of_region) for p in persisted] == [
        (capture(1).capture_id, 1.0, "red", False),
        (capture(2).capture_id, None, None, True),
    ]  # fmt: skip


async def test_a_prediction_for_a_foreign_capture_is_refused_for_good():
    catalog = FakeCatalog(tenants=[LAB], raise_on_write=ForeignRows("not the dive's"))

    with pytest.raises(ApplicationError) as raised:
        await _run(
            _activities(catalog).persist_laser_predictions,
            TARGET,
            [LaserPredictionResult(capture_id=uuid.uuid4(), confidence=0.1,
                                   predictor_version=2)],
        )  # fmt: skip
    assert raised.value.non_retryable and raised.value.type == "ForeignRows"


# -- the gate ----------------------------------------------------------------------


async def test_the_gate_input_is_the_dives_current_predictions_by_number():
    prediction = uuid.uuid4()
    catalog = FakeCatalog(
        tenants=[LAB],
        gate=GateInputs(442, [GateRow(prediction, 132158, 1.0, 2.0, 2)]),
    )

    payload = await _run(_activities(catalog).resolve_laser_gate_inputs, TARGET)

    assert payload.dive_id == DIVE and payload.dive_number == 442
    (row,) = payload.predictions
    assert (row.prediction_id, row.capture_number, row.predictor_version) == (
        prediction, 132158, 2)  # fmt: skip


async def test_recording_the_verdicts_reports_how_many_were_written():
    catalog = FakeCatalog(tenants=[LAB])
    frame = LaserFrameVerdict(prediction_id=uuid.uuid4(), auto_accept=True,
                              gate_verdict="auto_accepted", line_offset_px=0.5,
                              line_position_z=0.1)  # fmt: skip
    result = LaserAutoAcceptResult(
        summary=LaserAutoAcceptSummary(dive_id=DIVE, eligible=True, auto_accepted=1),
        frames=[frame],
    )

    summary = await _run(
        _activities(catalog).record_laser_gate_verdicts, TARGET, result
    )

    _, _, _, verdicts = catalog.calls[0]
    assert summary.written == 1 and summary.auto_accepted == 1
    assert (verdicts[0].prediction_id, verdicts[0].gate_verdict) == (
        frame.prediction_id, "auto_accepted")  # fmt: skip


# -- Label Studio: create, populate ------------------------------------------------


async def test_create_is_the_dives_laser_project_with_v1s_title_and_config():
    projects = FakeLabelProjects(project_id=274728)

    project = await _run(
        _activities(projects=projects).create_laser_label_studio_project, TARGET
    )

    assert project == 274728
    assert projects.calls == [
        (LAB, DIVE, "laser", LASER_PROJECT_TITLE_SUFFIX, LASER_LABELING_CONFIG_XML)
    ]


def _population(*items, colors=("red",)):
    return LaserPopulation(dive_number=31, items=list(items), colors=list(colors))


def _item(number, *, auto_accept=False, from_v1=False, x=2000.0, y=1500.0):
    return PopulateItem(capture(number, from_v1=from_v1), x, y, 4000, 3000, auto_accept)


async def test_imports_tasks_and_writes_one_label_per_incomplete_image():
    items = [_item(1), _item(2)]
    catalog = FakeCatalog(tenants=[LAB], population=_population(*items))
    store = FakeStore(present={i.capture.checksum for i in items})
    ls = FakeLabelStudio()

    recorded = await _run(
        _activities(catalog, store=store, ls=ls).populate_laser_label_studio_project,
        TARGET,
        500,
    )

    assert recorded == 2
    (imported,) = ls.imports
    assert [t["data"]["image_id"] for t in imported] == [1, 2]
    rows = [c for c in catalog.calls if c[0] == "record"]
    labels = [label for call in rows for label in call[2]]
    assert {(r.capture_id, r.ls_project_id, r.source) for r in labels} == {
        (items[0].capture.capture_id, 500, "human"),
        (items[1].capture.capture_id, 500, "human"),
    }
    assert ls.updates == [(500, {"is_published": True})]


async def test_defers_image_whose_laser_jpeg_is_not_written():
    """A task for a missing JPEG is a NoSuchKey task (v1's JPEG gate)."""
    items = [_item(1), _item(2)]
    catalog = FakeCatalog(tenants=[LAB], population=_population(*items))
    ls = FakeLabelStudio()

    await _run(
        _activities(
            catalog, store=FakeStore(present={items[0].capture.checksum}), ls=ls
        ).populate_laser_label_studio_project,
        TARGET,
        500,
    )

    assert [t["data"]["image_id"] for t in ls.imports[0]] == [1]


async def test_a_migrated_frames_task_points_at_v1s_jpeg():
    item = _item(1, from_v1=True)
    catalog = FakeCatalog(tenants=[LAB], population=_population(item))
    ls = FakeLabelStudio()

    await _run(
        _activities(
            catalog, store=FakeStore(present={item.capture.checksum}), ls=ls
        ).populate_laser_label_studio_project,
        TARGET,
        500,
    )

    assert ls.imports[0][0]["data"]["image"] == (
        f"s3://labels/fishsense-lite/preprocess_jpeg/{item.capture.checksum}.JPG"
    )


async def test_an_auto_accepted_frame_is_imported_annotated_and_recorded_as_such():
    item = _item(1, auto_accept=True)
    catalog = FakeCatalog(tenants=[LAB], population=_population(item))
    ls = FakeLabelStudio()

    await _run(
        _activities(
            catalog, store=FakeStore(present={item.capture.checksum}), ls=ls, bot=77
        ).populate_laser_label_studio_project,
        TARGET,
        500,
    )

    (task,) = ls.imports[0]
    assert task["predictions"] == [] and task["annotations"][0]["completed_by"] == 77
    (record,) = [c for c in catalog.calls if c[0] == "record"]
    assert record[2][0].source == "auto_accept"


async def test_rerun_does_not_reimport_existing_tasks():
    item = _item(1)
    catalog = FakeCatalog(tenants=[LAB], population=_population(item))
    url = f"s3://labels/tenants/{LAB}/preprocess_jpeg/{item.capture.checksum}.JPG"
    ls = FakeLabelStudio(tasks={9001: url})

    recorded = await _run(
        _activities(
            catalog, store=FakeStore(present={item.capture.checksum}), ls=ls
        ).populate_laser_label_studio_project,
        TARGET,
        500,
    )

    assert ls.imports == [] and recorded == 1
    (record,) = [c for c in catalog.calls if c[0] == "record"]
    assert record[2][0].ls_task_id == 9001


async def test_nothing_to_import_publishes_a_project_that_has_tasks():
    catalog = FakeCatalog(tenants=[LAB], population=_population(),
                          has_labels_in_project=True)  # fmt: skip
    ls = FakeLabelStudio()

    assert await _run(
        _activities(catalog, ls=ls).populate_laser_label_studio_project, TARGET, 500
    ) == 0  # fmt: skip
    assert ls.updates == [(500, {"is_published": True})]


async def test_does_not_publish_an_empty_project():
    catalog = FakeCatalog(tenants=[LAB], population=_population())
    ls = FakeLabelStudio()

    await _run(
        _activities(catalog, ls=ls).populate_laser_label_studio_project, TARGET, 500
    )

    assert ls.updates == []


async def test_the_whole_dive_gets_one_colour():
    items = [_item(1), _item(2)]
    catalog = FakeCatalog(
        tenants=[LAB], population=_population(*items, colors=["green"] * 9 + ["red"])
    )
    ls = FakeLabelStudio()

    await _run(
        _activities(
            catalog, store=FakeStore(present={i.capture.checksum for i in items}), ls=ls
        ).populate_laser_label_studio_project,
        TARGET,
        500,
    )

    labels = {
        t["predictions"][0]["result"][0]["value"]["keypointlabels"][0]
        for t in ls.imports[0]
    }
    assert labels == {"Green Laser"}


# -- backfill and the auto-accept apply ---------------------------------------------


def _targets(*task_ids, colors=("red",)):
    return TaskTargets(
        dive_number=94,
        colors=list(colors),
        targets=[
            TaskTarget(uuid.uuid4(), task, 500, 2000.0, 1500.0, 4000, 3000)
            for task in task_ids
        ],
    )


async def test_backfill_attaches_to_an_open_task_and_shows_the_version():
    catalog = FakeCatalog(tenants=[LAB], targets={False: _targets(1)})
    ls = FakeLabelStudio(title="Dive #94 - Laser Calibration Labeling")

    attached = await _run(
        _activities(catalog, ls=ls).backfill_laser_predictions_for_dive, TARGET
    )

    assert attached == 1
    ((task, version, _result),) = ls.created_predictions
    assert (task, version) == (1, laser_model_version_tag())
    # Attaching is not showing: the project's model_version is pointed at it.
    assert ls.updates == [(500, {"model_version": laser_model_version_tag()})]


async def test_backfill_is_idempotent_against_the_current_version():
    catalog = FakeCatalog(tenants=[LAB], targets={False: _targets(1, 2)})
    ls = FakeLabelStudio(predictions={500: [
        LabelStudioPrediction(task_id=1, model_version=laser_model_version_tag()),
        LabelStudioPrediction(task_id=2, model_version="laser-detector-v1"),
    ]})  # fmt: skip

    attached = await _run(
        _activities(catalog, ls=ls).backfill_laser_predictions_for_dive, TARGET
    )

    assert attached == 1  # task 2 was seeded by an older version: re-attached
    assert [c[0] for c in ls.created_predictions] == [2]


async def test_backfill_with_nothing_eligible_never_touches_label_studio():
    ls = FakeLabelStudio()

    assert (
        await _run(_activities(ls=ls).backfill_laser_predictions_for_dive, TARGET) == 0
    )
    assert ls.created_predictions == [] and ls.updates == []


async def test_apply_annotates_only_tasks_nobody_has_started():
    catalog = FakeCatalog(tenants=[LAB], targets={True: _targets(1, 2, 3)})
    ls = FakeLabelStudio(untouched={500: {1, 3}})

    applied = await _run(
        _activities(catalog, ls=ls).apply_laser_auto_accept_for_dive, TARGET
    )

    assert applied == 2
    assert [(a[0], a[1], a[3]) for a in ls.annotations] == [
        (1, 500, False), (3, 500, False)]  # fmt: skip
    # The rows the gate confirmed say so.
    assert [c for c in catalog.calls if c[0] == "mark"] == [("mark", LAB, [1, 3])]


async def test_an_auto_accepted_annotation_is_not_marked_ground_truth():
    catalog = FakeCatalog(tenants=[LAB], targets={True: _targets(1)})
    ls = FakeLabelStudio(untouched={500: {1}})

    await _run(_activities(catalog, ls=ls).apply_laser_auto_accept_for_dive, TARGET)

    ((_, _, result, ground_truth),) = ls.annotations
    assert ground_truth is False and result[0]["origin"] == "prediction"


async def test_a_task_missing_from_label_studio_is_skipped_not_fatal():
    catalog = FakeCatalog(tenants=[LAB], targets={True: _targets(1)})
    ls = FakeLabelStudio(untouched={500: set()})

    assert await _run(
        _activities(catalog, ls=ls).apply_laser_auto_accept_for_dive, TARGET
    ) == 0  # fmt: skip


# -- validation ---------------------------------------------------------------------


async def test_the_complete_dives_of_every_tenant_are_validated():
    a, b = uuid.UUID(int=31), uuid.UUID(int=32)
    catalog = FakeCatalog(
        tenants=[LAB, OTHER],
        dive_lists={("complete", LAB): [candidate(a)],
                    ("complete", OTHER): [candidate(b)]},
    )  # fmt: skip

    targets = await _run(_activities(catalog).laser_dives_with_complete_labeling)

    assert {(t.tenant_id, t.dive_id) for t in targets} == {(LAB, a), (OTHER, b)}


def _rows():
    return [
        LabelRow(uuid.uuid4(), 3, 20, 1.0, 1.0, False, True),
        LabelRow(uuid.uuid4(), 1, 20, 2.0, 2.0, True, True),
    ]


async def test_the_validation_input_is_the_full_population_and_the_slate_frames():
    rows = _rows()
    catalog = FakeCatalog(tenants=[LAB], labels=LabelPopulation(rows, [20], "f"))

    payload = await _run(_activities(catalog).resolve_laser_validation_inputs, TARGET)

    assert [(r.number, r.superseded) for r in payload.labels] == [(3, False), (1, True)]
    assert payload.calibration_capture_numbers == [20]


async def test_applying_a_judgement_writes_its_supersedes_and_line():
    catalog = FakeCatalog(tenants=[LAB])
    label = uuid.uuid4()
    line = LaserLineFit(a=0.0, b=1.0, c=-5.0, n_points=10, inlier_count=9,
                        inlier_fraction=0.9, residual_std=1.0, label_noise_mad=1.0,
                        line_confidence=10.0)  # fmt: skip
    result = LaserValidationResult(
        dive_id=DIVE, status="flagged", positives=10, flagged=1, line=line,
        supersede=[LaserSupersede(label_id=label,
                                  reason=SupersedeReason.VALIDATOR_3SIGMA)],
    )  # fmt: skip

    superseded = await _run(_activities(catalog).apply_laser_validation, TARGET, result)

    _, _, _, supersedes, written_line = catalog.calls[0]
    assert superseded == 1
    assert supersedes == [(label, "validator_3sigma")]
    assert written_line.noise_estimator == "signed_residual_mad"


# -- remediation --------------------------------------------------------------------


async def test_remediation_resolves_dive_numbers_across_tenants():
    a, b = uuid.UUID(int=41), uuid.UUID(int=42)
    catalog = FakeCatalog(tenants=[LAB, OTHER], numbers={(LAB, 7): a, (OTHER, 9): b})

    targets = await _run(_activities(catalog).resolve_laser_remediation_dives, [9, 7])

    assert [(t.number, t.tenant_id, t.dive_id) for t in targets] == [
        (7, LAB, a), (9, OTHER, b)]  # fmt: skip


async def test_no_numbers_means_every_dive():
    catalog = FakeCatalog(tenants=[LAB], numbers={(LAB, 7): uuid.uuid4(),
                                                  (LAB, 3): uuid.uuid4()})  # fmt: skip

    targets = await _run(_activities(catalog).resolve_laser_remediation_dives, None)

    assert [t.number for t in targets] == [3, 7]


async def test_an_unknown_dive_number_is_refused():
    with pytest.raises(ApplicationError) as raised:
        await _run(_activities().resolve_laser_remediation_dives, [404])
    assert raised.value.non_retryable


REMEDIATION = RemediationTarget(tenant_id=LAB, dive_id=DIVE, number=7)


async def test_the_plan_input_carries_the_rows_and_the_exclusions():
    catalog = FakeCatalog(tenants=[LAB], labels=LabelPopulation(_rows(), [20], "fp"))

    inputs = await _run(
        _activities(catalog).resolve_laser_remediation_inputs,
        DiveRemediationRequest(target=REMEDIATION, excluded_label_ids=[3],
                               dive_excluded=True),
    )  # fmt: skip

    assert isinstance(inputs.plan_input, PlanLaserRemediationInput)
    assert inputs.plan_input.dive_id == 7
    assert inputs.plan_input.excluded_label_ids == [3]
    assert inputs.plan_input.dive_excluded is True
    assert inputs.fingerprint == "fp"


async def test_apply_revives_exactly_the_reviewed_ids_and_logs_each(caplog):
    catalog = FakeCatalog(tenants=[LAB])

    with caplog.at_level(logging.INFO):
        written = await _run(
            _activities(catalog).apply_laser_remediation,
            ReviveLabels(target=REMEDIATION, pending=[41, 42], planned=[41, 42, 43],
                         fingerprint="fp"),
        )  # fmt: skip

    assert written == 2
    assert catalog.calls == [("revive", LAB, DIVE, [41, 42], "fp")]
    assert (
        sum("REVIVED laser_label_number=" in r.getMessage() for r in caplog.records)
        == 2
    )


async def test_apply_refuses_an_id_the_current_plan_does_not_contain():
    catalog = FakeCatalog(tenants=[LAB])

    with pytest.raises(ApplicationError) as raised:
        await _run(
            _activities(catalog).apply_laser_remediation,
            ReviveLabels(target=REMEDIATION, pending=[41, 66], planned=[41],
                         fingerprint="fp"),
        )  # fmt: skip

    assert raised.value.type == "RemediationPlanMismatch"
    assert raised.value.non_retryable and catalog.calls == []


async def test_apply_refuses_if_the_dive_changed_since_the_plan():
    catalog = FakeCatalog(tenants=[LAB], revive_raises=PopulationChanged("edited"))

    with pytest.raises(ApplicationError) as raised:
        await _run(
            _activities(catalog).apply_laser_remediation,
            ReviveLabels(target=REMEDIATION, pending=[41], planned=[41],
                         fingerprint="fp"),
        )  # fmt: skip

    assert raised.value.type == "RemediationPlanMismatch" and raised.value.non_retryable


async def test_nothing_pending_is_a_clean_no_op():
    catalog = FakeCatalog(tenants=[LAB])

    written = await _run(
        _activities(catalog).apply_laser_remediation,
        ReviveLabels(target=REMEDIATION, pending=[], planned=[], fingerprint="fp"),
    )

    assert written == 0 and catalog.calls == []


def test_the_plan_row_names_numbers():
    """The operator's report reads as v1's: dives and labels by number."""
    plan = DivePlan(dive_id=7, status="flagged", positives=3, superseded_now=1,
                    superseded_after=0, revive_ids=[41])  # fmt: skip
    assert plan.to_dict()["revive_ids"] == [41]

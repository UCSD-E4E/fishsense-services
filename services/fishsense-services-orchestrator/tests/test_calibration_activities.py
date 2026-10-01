"""The calibration stages' orchestrator activities: select, resolve, record.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_select_next_high_priority_dive_for_laser_calibration_activity.py
(the selectors' thin wrappers) and test_checkerboard_calibration_resolver.py
(its payload assembly; the frame selection itself is the API store's, tested
on Postgres). The recording is new: v1's data-worker wrote through the SDK.

v2 changes, each pinned here:

* selectors take the oldest candidate across tenants, re-entry (implausible
  stored fit) candidates last -- v1's `_reentry_last`, across tenants;
* resolvers return the processor's payload and the provenance the result is
  recorded with; each frame is a raw `ObjectRef` under the tenant;
* the result is appended with its provenance, the trim count and the refusal
  type folded into its gate verdicts;
* the lattice study resolves its tenant by slug and its dives by number.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.laser_calibration_store import (
    BoardFrame,
    CalibrationCandidate,
    CalibrationInputsUnavailable,
    CheckerboardInputs,
    SlateCalibrationInputs,
    SlateObservationRow,
)
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_contracts.slate_calibration import LaserCalibrationResult
from fishsense_services_orchestrator.calibration.activities import (
    LaserCalibrationActivities,
)
from fishsense_services_orchestrator.calibration.contracts import (
    CalibrationProvenance,
    LatticeDive,
    RecordCalibration,
)
from fishsense_services_orchestrator.object_store.contracts import StagingTarget
from fishsense_services_orchestrator.object_store.layout import ObjectLayout
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)

LAB, REEF = uuid.UUID(int=1), uuid.UUID(int=2)
DIVE = uuid.UUID(int=488)
CAMERA = uuid.UUID(int=9)
BOARD = uuid.UUID(int=4)
SLATE = uuid.UUID(int=7)
T0 = datetime(2026, 9, 1, tzinfo=UTC)
K = [[1800.0, 0.0, 640.0], [0.0, 1800.0, 480.0], [0.0, 0.0, 1.0]]
BUCKET, LABELS = "fishsense-test", "labels-fishsense-test"


class FakeCatalog:
    def __init__(self, **answers):
        self.answers = answers
        self.recorded = []

    async def member_tenants(self):
        return [LAB, REEF]

    async def resolve_tenant(self, slug):
        return {"lab": LAB}.get(slug)

    async def next_dive_for_laser_calibration(self, tenant_id):
        return self.answers.get("stage13", {}).get(tenant_id)

    async def next_dive_for_checkerboard_calibration(self, tenant_id):
        return self.answers.get("board", {}).get(tenant_id)

    async def slate_calibration_inputs(self, tenant_id, dive_id):
        return self._answer("slate_inputs")

    async def checkerboard_calibration_inputs(self, tenant_id, dive_id):
        return self._answer("board_inputs")

    async def dive_for_number(self, tenant_id, number):
        return {488: DIVE}.get(number)

    async def record_laser_calibration(self, tenant_id, dive_id, record):
        self.recorded.append((tenant_id, dive_id, record))
        return uuid.UUID(int=77)

    def _answer(self, name):
        answer = self.answers.get(name)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture(name="store")
def store_fixture():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        s3.create_bucket(Bucket=LABELS)
        settings = ObjectStoreConnection(
            endpoint_url="http://garage.example.com",
            region="garage",
            access_key_id="k",
            secret_access_key="s",
            bucket=BUCKET,
            labels_bucket=LABELS,
            legacy_labels_prefix="fishsense-lite",
        )
        yield OrchestratorObjectStore(s3, ObjectLayout(settings))


def _activities(catalog, store) -> LaserCalibrationActivities:
    return LaserCalibrationActivities(catalog=catalog, store=store)


async def _run(fn, *args):
    return await ActivityEnvironment().run(fn, *args)


def _candidate(n, hours, reentry=False):
    return CalibrationCandidate(uuid.UUID(int=n), T0 + timedelta(hours=hours), reentry)


# ---------- the selectors ----------


async def test_returns_the_oldest_candidate_across_tenants(store):
    catalog = FakeCatalog(stage13={LAB: _candidate(10, 2), REEF: _candidate(20, 0)})

    target = await _run(
        _activities(catalog, store).select_next_dive_for_laser_calibration
    )

    assert target == StagingTarget(tenant_id=REEF, dive_id=uuid.UUID(int=20))


async def test_a_re_entry_candidate_comes_after_every_fresh_one(store):
    """v1's `_reentry_last`, across tenants: a dive refitting a known-bad
    calibration must not head-of-line block a healthy dive in another tenant,
    however old it is."""
    catalog = FakeCatalog(
        board={LAB: _candidate(10, 0, reentry=True), REEF: _candidate(20, 9)}
    )

    target = await _run(
        _activities(catalog, store).select_next_dive_for_checkerboard_calibration
    )

    assert target == StagingTarget(tenant_id=REEF, dive_id=uuid.UUID(int=20))


async def test_returns_none_when_selector_finds_no_dive(store):
    catalog = FakeCatalog()

    assert (
        await _run(_activities(catalog, store).select_next_dive_for_laser_calibration)
        is None
    )
    assert (
        await _run(
            _activities(catalog, store).select_next_dive_for_checkerboard_calibration
        )
        is None
    )


# ---------- the resolvers ----------


def _slate_inputs() -> SlateCalibrationInputs:
    return SlateCalibrationInputs(
        dive_id=DIVE,
        slate_template_id=SLATE,
        camera_calibration_id=CAMERA,
        camera_matrix=K,
        template_points=[(0.0, 0.0), (2400.0, 0.0)],
        dpi=300,
        observations=[
            SlateObservationRow(
                capture_id=uuid.UUID(int=100),
                reference_points=[(1.0, 2.0), (3.0, 4.0)],
                skipped_points=None,
                laser_x=600.0,
                laser_y=500.0,
            )
        ],
        dive_dots=[(600.0, 500.0)],
        inputs_as_of=T0,
    )


async def test_stage_13_resolves_its_payload_and_provenance(store):
    catalog = FakeCatalog(slate_inputs=_slate_inputs())

    plan = await _run(
        _activities(catalog, store).resolve_slate_calibration_inputs,
        StagingTarget(tenant_id=LAB, dive_id=DIVE),
    )

    assert plan.payload.dive_id == DIVE
    assert plan.payload.template_points == [(0.0, 0.0), (2400.0, 0.0)]
    assert plan.payload.dpi == 300
    (observation,) = plan.payload.observations
    assert observation.reference_points == [(1.0, 2.0), (3.0, 4.0)]
    assert plan.payload.dive_dots == [(600.0, 500.0)]
    assert plan.provenance == CalibrationProvenance(
        producer="slate",
        camera_calibration_id=CAMERA,
        slate_template_id=SLATE,
        inputs_as_of=T0,
    )


async def test_stage_13_with_nothing_to_fit_resolves_to_none(store):
    plan = await _run(
        _activities(
            FakeCatalog(slate_inputs=None), store
        ).resolve_slate_calibration_inputs,
        StagingTarget(tenant_id=LAB, dive_id=DIVE),
    )

    assert plan is None


def _board_inputs(**overrides) -> CheckerboardInputs:
    values = {
        "dive_id": DIVE,
        "calibration_target_id": BOARD,
        "camera_calibration_id": CAMERA,
        "camera_matrix": K,
        "distortion_coefficients": [0.0] * 5,
        "rows": 10,
        "cols": 14,
        "pitch_x_m": 0.04223,
        "pitch_y_m": 0.04211,
        "frames": [
            BoardFrame(uuid.UUID(int=101), f"{101:032x}", False, 600.0, 500.0),
            BoardFrame(uuid.UUID(int=102), f"{102:032x}", True, 610.0, 505.0),
        ],
        "dive_dots": [(600.0, 500.0)],
        "inputs_as_of": T0,
    }
    values.update(overrides)
    return CheckerboardInputs(**values)


async def test_resolver_carries_the_board_geometry(store):
    """The measured pitch reaches the child in the payload, read from the
    row, so a replayed child cannot pick up a different one. v2: per axis,
    and the frames are raw refs under the tenant."""
    catalog = FakeCatalog(board_inputs=_board_inputs())

    plan = await _run(
        _activities(catalog, store).resolve_checkerboard_calibration_inputs,
        StagingTarget(tenant_id=LAB, dive_id=DIVE),
    )

    target = plan.payload.target
    assert (target.rows, target.cols) == (10, 14)
    assert (target.pitch_x_m, target.pitch_y_m) == (0.04223, 0.04211)
    assert [image.capture_id for image in plan.payload.images] == [
        uuid.UUID(int=101),
        uuid.UUID(int=102),
    ]
    assert [image.raw.key for image in plan.payload.images] == [
        f"tenants/{LAB}/raw/{101:032x}.ORF",
        f"tenants/{LAB}/raw/{102:032x}.ORF",
    ]
    assert plan.payload.images[1].laser_x == 610.0
    assert plan.provenance == CalibrationProvenance(
        producer="checkerboard",
        camera_calibration_id=CAMERA,
        calibration_target_id=BOARD,
        inputs_as_of=T0,
    )


async def test_resolver_refuses_an_unlinked_dive_for_good(store):
    catalog = FakeCatalog(
        board_inputs=CalibrationInputsUnavailable("dive has no calibration target")
    )

    with pytest.raises(ApplicationError, match="no calibration target") as excinfo:
        await _run(
            _activities(catalog, store).resolve_checkerboard_calibration_inputs,
            StagingTarget(tenant_id=LAB, dive_id=DIVE),
        )

    assert excinfo.value.non_retryable
    assert excinfo.value.type == "CalibrationInputsUnavailable"


# ---------- recording ----------


def _result(outcome="accepted", **overrides) -> LaserCalibrationResult:
    values = {
        "outcome": outcome,
        "observation_count": 8,
        "observations_trimmed": 2,
        "lever_arm_m": 1.1,
        "gate_verdicts": {"observation_geometry": "passed"},
        "core_version": "4.1.0",
    }
    if outcome == "accepted":
        values.update(laser_position=[0.0624, 0.0832, 0.0], laser_axis=[0.0, 0.0, 1.0])
    else:
        values.update(
            refusal_type="CalibrationImplausibleError",
            refusal_reason="fitted laser baseline 2.35 cm is outside",
        )
    values.update(overrides)
    return LaserCalibrationResult(**values)


async def test_an_accepted_fit_is_appended_with_its_provenance(store):
    catalog = FakeCatalog()
    provenance = CalibrationProvenance(
        producer="checkerboard",
        camera_calibration_id=CAMERA,
        calibration_target_id=BOARD,
        inputs_as_of=T0,
    )

    written = await _run(
        _activities(catalog, store).record_laser_calibration,
        RecordCalibration(
            tenant_id=LAB, dive_id=DIVE, result=_result(), provenance=provenance
        ),
    )

    assert written == str(uuid.UUID(int=77))
    ((tenant, dive, record),) = catalog.recorded
    assert (tenant, dive) == (LAB, DIVE)
    assert (record.producer, record.outcome) == ("checkerboard", "accepted")
    assert record.laser_position == [0.0624, 0.0832, 0.0]
    assert record.refusal_reason is None
    assert (record.camera_calibration_id, record.calibration_target_id) == (
        CAMERA,
        BOARD,
    )
    assert record.slate_template_id is None
    assert (record.lever_arm_m, record.observation_count) == (1.1, 8)
    assert record.inputs_as_of == T0
    assert record.core_version == "4.1.0"
    assert record.gate_verdicts == {
        "observation_geometry": "passed",
        "observations_trimmed": 2,
    }


async def test_a_refusal_is_appended_with_its_reason_and_type(store):
    """The reason is what an operator reads back; the type says which gate --
    v1 carried it only on the raised error."""
    catalog = FakeCatalog()

    await _run(
        _activities(catalog, store).record_laser_calibration,
        RecordCalibration(
            tenant_id=LAB,
            dive_id=DIVE,
            result=_result("refused"),
            provenance=CalibrationProvenance(
                producer="slate", camera_calibration_id=CAMERA, slate_template_id=SLATE
            ),
        ),
    )

    ((_, _, record),) = catalog.recorded
    assert record.outcome == "refused"
    assert record.laser_position is None and record.laser_axis is None
    assert record.refusal_reason == "fitted laser baseline 2.35 cm is outside"
    assert record.gate_verdicts["refusal_type"] == "CalibrationImplausibleError"
    assert record.slate_template_id == SLATE


async def _record_in_run(catalog, store, run_id, producer="slate"):
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, workflow_run_id=run_id)
    await env.run(
        _activities(catalog, store).record_laser_calibration,
        RecordCalibration(
            tenant_id=LAB,
            dive_id=DIVE,
            result=_result(),
            provenance=CalibrationProvenance(
                producer=producer, camera_calibration_id=CAMERA
            ),
        ),
    )
    return catalog.recorded[-1][2].id


async def test_a_retried_record_names_the_same_attempt(store):
    """The parent retries the record up to three times; a retry after a lost
    reply must not append the attempt again (it would move the dive's
    current calibration). The attempt is named by the parent's run and the
    producer, so every retry of it carries the one name."""
    catalog = FakeCatalog()

    first = await _record_in_run(catalog, store, "run-1")
    again = await _record_in_run(catalog, store, "run-1")
    other_run = await _record_in_run(catalog, store, "run-2")
    other_producer = await _record_in_run(catalog, store, "run-1", "checkerboard")

    assert first is not None and first == again
    assert len({first, other_run, other_producer}) == 3


# ---------- the lattice study ----------


async def test_the_study_resolves_its_tenant_by_slug(store):
    activities = _activities(FakeCatalog(), store)

    assert await _run(activities.resolve_lattice_tenant, "lab") == LAB
    with pytest.raises(ApplicationError, match="reef") as excinfo:
        await _run(activities.resolve_lattice_tenant, "reef")
    assert excinfo.value.non_retryable


async def test_the_study_resolves_a_dive_by_number_to_its_frames(store):
    """The calibration's own frames -- the population the fit consumed --
    each with the raw key it is staged to and the key its render goes to (its
    own folder: a render keyed like the stage-0.1 JPEG would overwrite what a
    laser labeler is looking at)."""
    catalog = FakeCatalog(board_inputs=_board_inputs())

    plan = await _run(
        _activities(catalog, store).resolve_lattice_inputs,
        LatticeDive(tenant_id=LAB, number=488, sample_limit=5),
    )

    assert plan.target == StagingTarget(tenant_id=LAB, dive_id=DIVE)
    assert plan.payload.sample_limit == 5
    assert (plan.payload.target.pitch_x_m, plan.payload.target.pitch_y_m) == (
        0.04223,
        0.04211,
    )
    first = plan.payload.images[0]
    assert first.raw.key == f"tenants/{LAB}/raw/{101:032x}.ORF"
    assert (first.render.bucket, first.render.key) == (
        LABELS,
        f"tenants/{LAB}/checkerboard_lattice_jpeg/{101:032x}.JPG",
    )


async def test_an_unknown_dive_number_is_refused_for_good(store):
    with pytest.raises(ApplicationError, match="999") as excinfo:
        await _run(
            _activities(FakeCatalog(), store).resolve_lattice_inputs,
            LatticeDive(tenant_id=LAB, number=999),
        )

    assert excinfo.value.non_retryable

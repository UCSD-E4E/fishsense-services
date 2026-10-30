"""The laser-color backfill (contract v25, the owner, 2026-10-26).

A page of a mission's automatic dots, each with the render its prediction was
made on; one whose render is gone is counted and left out. Each page is read
on the light processor and its readings recorded: compared with the
labelers' and the live kernel's colors, and written only with ``apply``.
The run goes on past a mission that fails, and its result is the report:
every mission's counts, and their totals."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import List, Optional

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker


from fishsense_services_api.laser_colour_backfill_store import (
    READING_SOURCE,
    READING_VERSION,
)
from fishsense_services_contracts import PROCESSOR_LIGHT_TASK_QUEUE
from fishsense_services_contracts.laser_colour_backfill import (
    COLOUR_READING_SOURCE,
    COLOUR_READING_VERSION,
    ColourFramesInput,
    ColourReading,
)
from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_orchestrator.laser_colour_backfill.activities import (
    ColourBackfillActivities,
    ColourPage,
    ColourTarget,
)
from fishsense_services_orchestrator.laser_colour_backfill.workflow import (
    BackfillLaserColoursForMissionWorkflow,
    BackfillLaserColoursWorkflow,
    ColourBackfillRequest,
    ColourMissionRequest,
    merge_counts,
)

TENANT = uuid.uuid4()


def test_the_api_spells_the_contracts_source_and_version():
    """The API image has no contracts: the store repeats them, pinned here."""
    assert (READING_SOURCE, READING_VERSION) == (
        COLOUR_READING_SOURCE,
        COLOUR_READING_VERSION,
    )


# -- the activities ---------------------------------------------------------------------


class _Catalog:
    def __init__(self, rows):
        self.rows, self.recorded = rows, []

    async def member_tenants(self):
        return [TENANT]

    async def tenant_slug(self, t):
        return "lab"

    async def colour_backfill_missions(self, t):
        return [
            SimpleNamespace(mission_id=uuid.uuid4(), number=7, frames=len(self.rows))
        ]

    async def colour_backfill_frames(self, t, m, *, after_seq=0, limit=200):
        return [r for r in self.rows if r.seq > after_seq][:limit]

    async def record_colour_page(self, t, m, readings, *, apply):
        self.recorded.append((readings, apply))
        return {"written": len(readings) if apply else 0}


class _Renders:
    def __init__(self, rendered, bucket=True):
        self.rendered, self.bucket = rendered, bucket

    async def objects(self, t):
        return object() if self.bucket else None

    async def at(self, t, ids, *, render_version, camera_calibration_id):
        return {c: SimpleNamespace(status="rendered", key=f"renders/{c}.JPG")
                for c in ids if c in self.rendered}  # fmt: skip

    async def presigner(self, t):
        return lambda key: f"https://g/get/{key}"


def _row(seq):
    return SimpleNamespace(prediction_id=uuid.uuid4(), seq=seq, capture_id=uuid.uuid4(),
                           x=2000.0 + seq, y=1500.0, kernel_color=None,
                           render_version=1, camera_calibration_id=uuid.UUID(int=9))  # fmt: skip


async def test_a_page_carries_each_dot_and_its_own_render():
    a, gone = _row(1), _row(2)
    acts = ColourBackfillActivities(catalog=_Catalog([a, gone]),
                                    renders=_Renders({a.capture_id}))  # fmt: skip

    page = await ActivityEnvironment().run(acts.resolve_colour_page,
                                           ColourTarget(TENANT, uuid.uuid4()), 0)  # fmt: skip

    assert (page.last_seq, page.unavailable) == (2, 1)
    (frame,) = page.payload.frames
    assert (frame.prediction_id, frame.x, frame.y) == (a.prediction_id, 2001.0, 1500.0)
    assert frame.render_url == f"https://g/get/renders/{a.capture_id}.JPG"


async def test_the_end_of_a_mission_and_a_tenant_without_a_bucket():
    acts = ColourBackfillActivities(
        catalog=_Catalog([_row(1)]), renders=_Renders(set())
    )
    end = await ActivityEnvironment().run(acts.resolve_colour_page,
                                          ColourTarget(TENANT, uuid.uuid4()), 1)  # fmt: skip
    assert (end.payload, end.last_seq) == (None, None)
    acts = ColourBackfillActivities(catalog=_Catalog([_row(1)]),
                                    renders=_Renders(set(), bucket=False))  # fmt: skip
    page = await ActivityEnvironment().run(acts.resolve_colour_page,
                                           ColourTarget(TENANT, uuid.uuid4()), 0)  # fmt: skip
    assert (page.payload, page.last_seq, page.unavailable) == (None, 1, 1)


async def test_a_page_is_recorded_written_only_with_apply():
    catalog = _Catalog([])
    acts = ColourBackfillActivities(catalog=catalog, renders=None)
    reading = ColourReading(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                            status="read", color="red", margin=150.0)  # fmt: skip

    dry = await ActivityEnvironment().run(acts.record_colour_page,
                                          ColourTarget(TENANT, uuid.uuid4()), [reading],
                                          False)  # fmt: skip
    wet = await ActivityEnvironment().run(acts.record_colour_page,
                                          ColourTarget(TENANT, uuid.uuid4()), [reading],
                                          True)  # fmt: skip

    assert (dry["written"], wet["written"]) == (0, 1)
    (rows, apply), _ = catalog.recorded
    assert apply is False and (rows[0].color, rows[0].margin) == ("red", 150.0)


async def test_the_plan_is_the_tenants_missions_with_dots():
    acts = ColourBackfillActivities(catalog=_Catalog([_row(1), _row(2)]), renders=None)
    plan = await ActivityEnvironment().run(acts.plan_colour_backfill, "lab", None)
    assert plan["tenant_id"] == str(TENANT)
    assert [(m["number"], m["frames"]) for m in plan["missions"]] == [(7, 2)]


def test_counts_merge_by_key_at_every_depth():
    assert merge_counts({"read": {"red": 1}, "written": 2, "unavailable": 1},
                        {"read": {"red": 2, "green": 1}, "written": 1}) == {
        "read": {"red": 3, "green": 1}, "written": 3, "unavailable": 1}  # fmt: skip


# -- the workflows ----------------------------------------------------------------------

QUEUE = "test-colour"
EVENTS: list = []
IDS = {n: uuid.uuid5(uuid.NAMESPACE_OID, f"c{n}") for n in range(1, 5)}


def _stubs(*, pages=((2, 0),), broken=()):
    @activity.defn(name="plan_colour_backfill")
    async def plan(tenant: str, numbers: Optional[List[int]] = None) -> dict:
        return {"tenant_id": str(TENANT),
                "missions": [{"mission_id": str(IDS[n]), "number": n, "frames": 2}
                             for n in (numbers or [1, 2, 3])]}  # fmt: skip

    @activity.defn(name="resolve_colour_page")
    async def resolve(t: ColourTarget, after: int = 0) -> ColourPage:
        if t.mission_id in {IDS[n] for n in broken}:
            raise ApplicationError("boom", type="Boom", non_retryable=True)
        EVENTS.append(("resolve", after))
        if after >= len(pages):
            return ColourPage(payload=None)
        n, gone = pages[after]
        from fishsense_services_contracts.laser_colour_backfill import ColourFrame

        frames = [ColourFrame(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                              render_url="u", x=1.0, y=1.0) for _ in range(n)]  # fmt: skip
        return ColourPage(
            payload=ColourFramesInput(tenant_id=t.tenant_id, mission_id=t.mission_id,
                                      frames=frames) if n else None,
            last_seq=after + 1, unavailable=gone)  # fmt: skip

    @activity.defn(name="record_colour_page")
    async def record(
        t: ColourTarget, readings: List[ColourReading], apply: bool
    ) -> dict:
        EVENTS.append(("record", len(readings), apply))
        return {
            "read": {"red": len(readings)},
            "written": len(readings) if apply else 0,
        }

    @activity.defn(name="ensure_light_processor_running")
    async def wake() -> None:
        EVENTS.append(("wake",))

    return [plan, resolve, record, wake]


@workflow.defn(name="ReadLaserColoursWorkflow")
class _Light:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: dict) -> list:
        return [ColourReading(prediction_id=f["prediction_id"], capture_id=f["capture_id"],
                              status="read", color="red", margin=100.0).model_dump(mode="json")
                for f in payload["frames"]]  # fmt: skip


async def _run(fn, arg, **stubs):
    EVENTS.clear()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue=QUEUE, activities=_stubs(**stubs),
                          workflows=[BackfillLaserColoursForMissionWorkflow,
                                     BackfillLaserColoursWorkflow]), \
                   Worker(env.client, task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
                          workflows=[_Light]):  # fmt: skip
            return await env.client.execute_workflow(
                fn, arg, id=f"{QUEUE}-{uuid.uuid4()}", task_queue=QUEUE
            )


async def test_a_mission_pages_to_its_end_reading_and_recording_each_page():
    counts = await _run(BackfillLaserColoursForMissionWorkflow.run,
                        ColourMissionRequest(TENANT, IDS[1], apply=False),
                        pages=((3, 1), (2, 0)))  # fmt: skip

    assert counts == {"read": {"red": 5}, "written": 0, "unavailable": 1}
    assert EVENTS == [("resolve", 0), ("wake",), ("record", 3, False),
                      ("resolve", 1), ("record", 2, False), ("resolve", 2)]  # fmt: skip


async def test_apply_is_passed_to_every_page():
    counts = await _run(BackfillLaserColoursForMissionWorkflow.run,
                        ColourMissionRequest(TENANT, IDS[1], apply=True))  # fmt: skip

    assert counts["written"] == 2
    assert ("record", 2, True) in EVENTS


async def test_the_run_reports_every_mission_and_the_totals_past_a_failure():
    out = await _run(BackfillLaserColoursWorkflow.run,
                     ColourBackfillRequest(tenant="lab"), broken=(2,))  # fmt: skip

    rows = {r["number"]: r for r in out["missions"]}
    assert set(rows) == {1, 2, 3} and "error" in rows[2]
    assert rows[1]["counts"]["read"] == {"red": 2}
    assert out["totals"]["read"] == {"red": 4}
    assert (out["apply"], out["failed_missions"]) == (False, 1)


# -- the CLI ------------------------------------------------------------------------


def test_the_cli_is_a_dry_run_unless_told_to_apply():
    from datetime import datetime, timezone

    from fishsense_services_orchestrator.laser_colour_backfill.run import (
        parse_args,
        workflow_id,
    )

    dry = parse_args(["start", "--tenant", "lab", "--missions", "334,471"])
    wet = parse_args(["start", "--tenant", "lab", "--apply"])
    now = datetime(2026, 10, 26, 18, 0, tzinfo=timezone.utc)

    assert (dry.apply, dry.missions, wet.apply) == (False, "334,471", True)
    assert workflow_id(False, now) == "laser-colour-backfill-dry-20261026T180000Z"
    assert workflow_id(True, now) == "laser-colour-backfill-apply-20261026T180000Z"

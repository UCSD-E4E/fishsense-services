"""The checksum-verification workflows: one dive, and the sweep over every dive.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_verify_dive_checksums_workflow.py and
test_verify_all_dives_workflow.py. In-process Temporal, activities stubbed.

What an operator depends on, kept:

* both arguments reach the activity, and the sampling limit is optional --
  `--input <dive>` alone means "check every frame";
* the sweep visits every canonical dive the selector returns, **one at a
  time** (the verification shares one NAS with the pipeline's staging), and
  forwards `limit_per_dive` -- the knob between a ~20 GB sample and a ~930 GB
  full sweep;
* **one dive's failure does not abandon the sweep**, and a dive that could not
  be verified carries an `error`, so it never reads as clean;
* an explicit dive list overrides the selector;
* progress is queryable while a days-long sweep runs.

v2 change: dives are named by their number (v1's dive id for a migrated dive).
"""

from __future__ import annotations

import uuid

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_orchestrator.ops.checksums.contracts import (
    ChecksumMismatch,
    VerifyAllDivesProgress,
    VerifyChecksumsReport,
)
from fishsense_services_orchestrator.ops.checksums.workflows import (
    VerifyAllDivesChecksumsWorkflow,
    VerifyDiveChecksumsWorkflow,
)

QUEUE = "test-verify-checksums"


def _activities(dive_numbers=(), *, per_dive=None, fail_on=()):
    """Stub the selector and the per-dive verifier. `fail_on` names dives whose
    verification raises, standing in for a dive whose NAS reads exhausted
    their retries."""
    calls: list[tuple[int, int | None]] = []

    @activity.defn(name="select_canonical_dive_numbers")
    async def select() -> list[int]:
        return list(dive_numbers)

    @activity.defn(name="verify_dive_checksums")
    async def verify(dive_number: int, limit: int | None) -> VerifyChecksumsReport:
        calls.append((dive_number, limit))
        if dive_number in fail_on:
            raise RuntimeError(f"NAS unreachable for dive {dive_number}")
        if per_dive and dive_number in per_dive:
            return per_dive[dive_number]
        return VerifyChecksumsReport(
            dive_number=dive_number,
            total_in_dive=55,
            checked=limit or 55,
            checksum_matched=limit or 55,
        )

    return [select, verify], calls


async def _execute(workflow, args, activities):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client, task_queue=QUEUE, workflows=[workflow], activities=activities
        ):
            return await env.client.execute_workflow(
                workflow.run,
                args=args,
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )


# -- one dive ---------------------------------------------------------------------


async def test_forwards_the_dive_and_limit_and_returns_the_report():
    acts, calls = _activities()
    report = await _execute(VerifyDiveChecksumsWorkflow, (412, 25), acts)

    assert calls == [(412, 25)]
    assert report.dive_number == 412
    assert report.checked == 25
    assert report.total_in_dive == 55


async def test_the_limit_is_optional_and_defaults_to_the_whole_dive():
    """`temporal workflow start --input 412` is the obvious way to run this,
    and it must mean "check every frame"."""
    acts, calls = _activities()
    report = await _execute(VerifyDiveChecksumsWorkflow, (412,), acts)

    assert calls == [(412, None)]
    assert report.checked == 55


# -- the sweep --------------------------------------------------------------------


async def test_the_sweep_visits_every_canonical_dive_the_selector_returns():
    acts, calls = _activities([11, 22, 33])
    report = await _execute(VerifyAllDivesChecksumsWorkflow, (5,), acts)

    assert [c[0] for c in calls] == [11, 22, 33]
    assert report.dives_requested == 3
    assert report.dives_verified == 3
    assert report.images_checked == 15


async def test_the_per_dive_limit_is_forwarded_to_each_dive():
    acts, calls = _activities([11, 22])
    await _execute(VerifyAllDivesChecksumsWorkflow, (5,), acts)

    assert calls == [(11, 5), (22, 5)]


async def test_no_limit_means_every_frame_of_every_dive():
    acts, calls = _activities([11])
    await _execute(VerifyAllDivesChecksumsWorkflow, (None,), acts)

    assert calls == [(11, None)]


async def test_dives_are_verified_one_at_a_time():
    """Serial by design: a parallel sweep would starve the pipeline's staging
    of NAS bandwidth for days. Strict input order proves it."""
    acts, calls = _activities([11, 22, 33, 44])
    report = await _execute(VerifyAllDivesChecksumsWorkflow, (5,), acts)

    assert [c[0] for c in calls] == [11, 22, 33, 44]
    assert report.dives_verified == 4


async def test_findings_are_carried_per_dive_and_clean_dives_are_kept():
    """Clean rows are the result too: dropping them would make "verified,
    fine" indistinguishable from "never reached"."""
    dirty = VerifyChecksumsReport(
        dive_number=22,
        total_in_dive=55,
        checked=5,
        checksum_matched=4,
        mismatches=[
            ChecksumMismatch(
                capture_number=9, path="a/b.ORF", stored="0" * 32, computed="1" * 32
            )
        ],
    )
    acts, _ = _activities([11, 22], per_dive={22: dirty})
    report = await _execute(VerifyAllDivesChecksumsWorkflow, (5,), acts)

    assert len(report.dives) == 2
    assert [d.dive_number for d in report.dives_with_findings] == [22]
    assert report.dives_with_findings[0].mismatches[0].stored == "0" * 32
    assert report.checksum_matched == 9


async def test_a_dive_that_cannot_be_verified_is_recorded_and_the_sweep_goes_on():
    """A full sweep is days of transfer; abandoning it for one unreachable
    dive would be its own outage. And the dive must not read as clean."""
    acts, calls = _activities([11, 22, 33], fail_on=(22,))
    report = await _execute(VerifyAllDivesChecksumsWorkflow, (5,), acts)

    # Dive 22 is retried by the policy; what matters is that 33 was reached.
    assert list(dict.fromkeys(c[0] for c in calls)) == [11, 22, 33]
    assert report.dives_verified == 2
    errored = [d for d in report.dives if d.error]
    assert [d.dive_number for d in errored] == [22]
    assert errored[0].is_clean is False


async def test_an_explicit_dive_list_overrides_the_selector():
    """For re-running just the dives a sample flagged."""
    acts, calls = _activities([11, 22, 33])
    await _execute(VerifyAllDivesChecksumsWorkflow, (5, [33]), acts)

    assert [c[0] for c in calls] == [33]


async def test_progress_is_queryable_while_the_sweep_runs():
    """A 930 GB sweep runs for days. "Is it still going, and how far?" has to
    be answerable without waiting for the return value."""
    acts, _ = _activities([11, 22])
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[VerifyAllDivesChecksumsWorkflow],
            activities=acts,
        ):
            handle = await env.client.start_workflow(
                VerifyAllDivesChecksumsWorkflow.run,
                args=(5,),
                id=f"{QUEUE}-{uuid.uuid4()}",
                task_queue=QUEUE,
            )
            await handle.result()
            # By method reference, so the payload decodes to the model.
            progress = await handle.query(VerifyAllDivesChecksumsWorkflow.progress)

    assert isinstance(progress, VerifyAllDivesProgress)
    assert progress.state == "done"
    assert progress.total_dives == 2
    assert progress.dives_done == 2
    assert progress.images_checked == 10
    assert progress.current_dive_number is None

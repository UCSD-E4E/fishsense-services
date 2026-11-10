"""The phone measurement's workflow (contract 28): a chunk of phone captures,
one activity each, in parallel; a capture that fails every try is left out."""

from __future__ import annotations

import uuid
from typing import List

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_contracts.phone_measurement import (
    PhoneFrame,
    PhoneMeasured,
    PhoneMeasureInput,
)
from fishsense_services_processor.phone_measurement.workflow import (
    MeasurePhoneCapturesWorkflow,
)

K = [1604.2147, 0.0, 956.5816, 0.0, 1604.2147, 717.7617, 0.0, 0.0, 1.0]


def _frame(capture_id) -> PhoneFrame:
    return PhoneFrame(capture_id=capture_id, rgb_url="https://s3/r", depth_url="https://s3/d",
                      depth_width=256, depth_height=192, depth_row_bytes=1024,
                      intrinsics=K)  # fmt: skip


async def _run(stub, captures) -> List[PhoneMeasured]:
    payload = PhoneMeasureInput(
        tenant_id=uuid.uuid4(), frames=[_frame(c) for c in captures]
    )
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue="t-phone", activities=[stub],
                          workflows=[MeasurePhoneCapturesWorkflow]):  # fmt: skip
            return await env.client.execute_workflow(
                MeasurePhoneCapturesWorkflow.run, payload,
                id=f"t-phone-{uuid.uuid4()}", task_queue="t-phone",
                result_type=List[PhoneMeasured],
            )  # fmt: skip


async def test_each_capture_is_measured_once():
    seen = []

    @activity.defn(name="measure_phone_capture")
    async def stub(frame: PhoneFrame) -> PhoneMeasured:
        seen.append(frame.capture_id)
        return PhoneMeasured(capture_id=frame.capture_id, status="no_fish")

    captures = [uuid.uuid4() for _ in range(3)]
    results = await _run(stub, captures)
    assert [r.capture_id for r in results] == captures
    assert sorted(seen) == sorted(captures)


async def test_a_capture_that_keeps_failing_does_not_lose_the_others():
    bad = uuid.uuid4()
    attempts = []

    @activity.defn(name="measure_phone_capture")
    async def stub(frame: PhoneFrame) -> PhoneMeasured:
        if frame.capture_id == bad:
            attempts.append(1)
            raise RuntimeError("out of memory")
        return PhoneMeasured(capture_id=frame.capture_id, status="no_fish")

    good = [uuid.uuid4(), uuid.uuid4()]
    results = await _run(stub, [good[0], bad, good[1]])
    assert [r.capture_id for r in results] == good
    assert len(attempts) == 3

"""The laser-colour backfill's processor stage (contract v25, 2026-10-26).

Each automatic dot's colour is read off the render its prediction was made on,
at the dot, by fishsense-core's `classify_laser_color` -- the live kernel's
rule -- with no model and no GPU (the light role). A render not there, or not
decoding, is the frame's status; a frame failing every try is ``failed``."""

from __future__ import annotations

import uuid

import cv2
import numpy as np
import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from fishsense_services_contracts.laser_colour_backfill import (
    ColourFrame,
    ColourFramesInput,
    ColourReading,
)
from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_processor.laser_colour_backfill.activities import (
    LaserColourActivities,
)
from fishsense_services_processor.laser_colour_backfill.workflow import (
    ReadLaserColoursWorkflow,
)

X, Y = 400.0, 300.0


def _render(bgr_dot=(30, 30, 230), background=(90, 110, 60)) -> bytes:
    """A blue-green scene with a 5 px dot at (X, Y), JPEG-encoded."""
    img = np.zeros((600, 800, 3), np.uint8)
    img[:] = background
    cv2.circle(img, (int(X), int(Y)), 5, bgr_dot, -1)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    return buf.tobytes()


class _Store:
    def __init__(self, data, error=None):
        self.data, self.error, self.read = data, error, []

    async def download_image(self, ref=None, url=None):
        self.read.append(url)
        if self.error:
            raise self.error
        return self.data


def _acts(store):
    return LaserColourActivities(store_factory=lambda: store)


def _frame(**kw):
    return ColourFrame(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                       render_url=kw.get("url", "https://g/r"),
                       x=kw.get("x", X), y=kw.get("y", Y))  # fmt: skip


@pytest.mark.parametrize(
    "dot, colour", [((30, 30, 230), "red"), ((40, 220, 40), "green")]
)
async def test_the_dots_colour_is_read_off_its_render(dot, colour):
    store = _Store(_render(dot))
    f = _frame()

    r = await ActivityEnvironment().run(_acts(store).read_laser_colour, f)

    assert store.read == ["https://g/r"]
    assert (r.status, r.color, r.prediction_id) == ("read", colour, f.prediction_id)
    assert (r.margin > 0) == (colour == "red")


async def test_a_dot_too_close_to_call_is_read_with_no_colour():
    r = await ActivityEnvironment().run(
        _acts(_Store(_render((120, 120, 120), (120, 120, 120)))).read_laser_colour,
        _frame(),
    )

    assert (r.status, r.color) == ("read", None)
    assert r.margin is not None and abs(r.margin) < 4.0


async def test_a_dot_outside_the_render_has_no_reading():
    r = await ActivityEnvironment().run(
        _acts(_Store(_render())).read_laser_colour, _frame(x=5000.0)
    )

    assert (r.status, r.color, r.margin) == ("read", None, None)


@pytest.mark.parametrize("store, status", [(_Store(None, RuntimeError("404")), "render_unavailable"),
                                           (_Store(b"not a jpeg"), "decode_failed")])  # fmt: skip
async def test_a_render_not_there_or_unreadable_is_the_frames_status(store, status):
    f = _frame()

    r = await ActivityEnvironment().run(_acts(store).read_laser_colour, f)

    assert (r.status, r.prediction_id, r.color) == (status, f.prediction_id, None)


FAIL: set = set()


@activity.defn(name="read_laser_colour")
async def _read(frame: ColourFrame) -> ColourReading:
    if frame.capture_id in FAIL:
        raise ApplicationError("boom")
    return ColourReading(prediction_id=frame.prediction_id, capture_id=frame.capture_id,
                         status="read", color="red", margin=100.0)  # fmt: skip


async def test_each_frame_is_read_and_one_failing_every_try_is_recorded():
    bad, good = uuid.uuid4(), uuid.uuid4()
    FAIL.add(bad)
    frames = [ColourFrame(prediction_id=uuid.uuid4(), capture_id=c, render_url="u",
                          x=1.0, y=1.0) for c in (bad, good)]  # fmt: skip
    try:
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=DATA_CONVERTER
        ) as env:
            async with Worker(env.client, task_queue="q",
                              workflows=[ReadLaserColoursWorkflow], activities=[_read],
                              workflow_runner=UnsandboxedWorkflowRunner()):  # fmt: skip
                out = await env.client.execute_workflow(
                    ReadLaserColoursWorkflow.run,
                    ColourFramesInput(tenant_id=uuid.uuid4(), mission_id=uuid.uuid4(),
                                      frames=frames),
                    id=f"t-{uuid.uuid4()}", task_queue="q",
                )  # fmt: skip
    finally:
        FAIL.clear()

    assert [(r.capture_id, r.status) for r in out] == [(bad, "failed"), (good, "read")]


def test_the_stage_runs_on_the_light_role():
    from fishsense_services_processor.laser_colour_backfill.stage import STAGE
    from fishsense_services_processor.registry import ROLE_LIGHT

    assert STAGE.role == ROLE_LIGHT
    assert ReadLaserColoursWorkflow in STAGE.workflows

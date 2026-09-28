"""Workflow contract tests for checkerboard lattice verification.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_verify_checkerboard_lattice_workflow.py; names and
reasons are v1's. The drawing is covered by `test_lattice_overlay.py` and the
detector by `test_checkerboard_detection.py`. What is left is the wiring, and
two pieces of it carry real consequence:

* **the board geometry travels per frame from the dispatch**, exactly as the
  calibration child's does. The whole study is about a mis-resolved pitch, so a
  verification run that rendered at a *different* pitch than the fit used would
  be answering a question nobody asked.
* **`sample_limit` is a stable head-of-list take.** A random sample would make
  two runs of the same study describe different frames, and a labeler's
  verdicts would stop being comparable to the run that produced them.

v2 changes: captures and `ObjectRef`s for image ids and checksums (each frame
carries the ref its render is written to), and a `CheckerboardTarget` with a
per-axis pitch for v1's rows/cols/square size.
"""

from __future__ import annotations

import uuid

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.slate_calibration import (
    CheckerboardLatticeRender,
    CheckerboardTarget,
    LatticeImage,
    VerifyCheckerboardLatticeInput,
)
from fishsense_services_processor.checkerboard.activities import (
    RenderCheckerboardLatticeInput,
)
from fishsense_services_processor.checkerboard.workflows import (
    VerifyCheckerboardLatticeWorkflow,
)

CAMERA_MATRIX = [[1800.0, 0.0, 640.0], [0.0, 1800.0, 480.0], [0.0, 0.0, 1.0]]
SQUARE_SIZE_M = 0.042
TASK_QUEUE = "test-lattice-verification"
TENANT = "7b0c7d4e-3f58-4d8e-9a3c-0c5f1f7f2d11"
TARGET = CheckerboardTarget(
    rows=10, cols=14, pitch_x_m=SQUARE_SIZE_M, pitch_y_m=SQUARE_SIZE_M
)


def _capture(n: int) -> uuid.UUID:
    return uuid.UUID(int=200 + n)


def _input(*, images=3, **overrides):
    kwargs = {
        "dive_id": uuid.UUID(int=522),
        "camera_matrix": CAMERA_MATRIX,
        "distortion_coefficients": [0.0] * 5,
        "target": TARGET,
        "images": [
            LatticeImage(
                capture_id=_capture(n),
                raw=ObjectRef(
                    bucket="scratch", key=f"tenants/{TENANT}/raw/{n:032d}.ORF"
                ),
                render=ObjectRef(
                    bucket="labels",
                    key=f"tenants/{TENANT}/checkerboard_lattice_jpeg/{n:032d}.JPG",
                ),
                laser_x=600.0 + n,
                laser_y=500.0,
            )
            for n in range(images)
        ],
        "sample_limit": None,
    }
    kwargs.update(overrides)
    return VerifyCheckerboardLatticeInput(**kwargs)


def _rendered(payload: RenderCheckerboardLatticeInput) -> CheckerboardLatticeRender:
    return CheckerboardLatticeRender(
        capture_id=payload.capture_id,
        image=payload.render,
        detected_rows=10,
        detected_cols=14,
        median_spacing_px=32.0,
        corners=[[0.0, 0.0]] * (10 * 14),
        width=4000,
        height=3000,
    )


async def _run(payload, *, workflow_id, on_render=None):
    seen: list[RenderCheckerboardLatticeInput] = []

    @activity.defn(name="render_checkerboard_lattice")
    async def _render(inner: RenderCheckerboardLatticeInput):
        seen.append(inner)
        return (on_render or _rendered)(inner)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[VerifyCheckerboardLatticeWorkflow],
            activities=[_render],
        ):
            result = await env.client.execute_workflow(
                VerifyCheckerboardLatticeWorkflow.run,
                payload,
                id=workflow_id,
                task_queue=TASK_QUEUE,
                result_type=list[CheckerboardLatticeRender],
            )
    return result, seen


async def test_every_frame_is_dispatched_with_the_board_geometry():
    result, seen = await _run(_input(images=3), workflow_id="wf-lattice-geometry")

    # Results keep input order — `asyncio.gather` returns in the order it was
    # given, whatever order the activities completed in.
    assert [r.capture_id for r in result] == [_capture(n) for n in range(3)]
    # Dispatch order is not completion order under gather, so this is a set:
    # what must hold is that every frame was dispatched exactly once, with the
    # dive's board geometry.
    assert sorted(p.capture_id for p in seen) == [_capture(n) for n in range(3)]
    assert {p.target for p in seen} == {TARGET}


async def test_each_frame_carries_its_own_dot():
    """The dot decides admission via `point_in_laser_region`, so a frame
    rendered against another frame's dot would admit the wrong population.
    v2: and its own render target."""
    _, seen = await _run(_input(images=3), workflow_id="wf-lattice-dots")

    assert {p.capture_id: p.laser_x for p in seen} == {
        _capture(0): 600.0,
        _capture(1): 601.0,
        _capture(2): 602.0,
    }
    assert {p.capture_id: p.render.key[-8:] for p in seen} == {
        _capture(n): f"{n:04d}.JPG" for n in range(3)
    }


async def test_sample_limit_caps_the_frames_rendered():
    result, seen = await _run(
        _input(images=10, sample_limit=4), workflow_id="wf-lattice-cap"
    )

    assert len(seen) == 4
    assert len(result) == 4


async def test_sample_limit_is_a_stable_head_of_list_take():
    """Two runs of the same study must render the same frames.

    Determinism is forced on workflow code anyway, but the property worth
    pinning is the *choice*: a seeded random sample would also replay, and
    would still make a second run describe a different set of frames than the
    verdicts already collected refer to.
    """
    _, first = await _run(
        _input(images=10, sample_limit=3), workflow_id="wf-lattice-stable-1"
    )
    _, second = await _run(
        _input(images=10, sample_limit=3), workflow_id="wf-lattice-stable-2"
    )

    assert sorted(p.capture_id for p in first) == [_capture(n) for n in range(3)]
    assert sorted(p.capture_id for p in first) == sorted(p.capture_id for p in second)


async def test_no_sample_limit_renders_every_frame():
    _, seen = await _run(_input(images=6), workflow_id="wf-lattice-uncapped")

    assert len(seen) == 6


async def test_a_sample_limit_above_the_frame_count_is_harmless():
    _, seen = await _run(
        _input(images=2, sample_limit=50), workflow_id="wf-lattice-over-cap"
    )

    assert len(seen) == 2


async def test_frames_with_no_lattice_are_returned_not_dropped():
    """A skipped frame still comes back, so the parent can tally *why*.

    The orchestrator turns only rendered frames into tasks, but the skip
    reasons are the other half of the finding: a dive whose boards mostly fail
    to detect is telling you something different from one whose lattice is
    wrong, and the two are indistinguishable if the skips never leave this
    workflow.
    """

    def _skip(payload):
        if payload.capture_id == _capture(1):
            return CheckerboardLatticeRender(
                capture_id=payload.capture_id, skip_reason="no_usable_board"
            )
        return _rendered(payload)

    result, _ = await _run(
        _input(images=3), workflow_id="wf-lattice-skips", on_render=_skip
    )

    assert len(result) == 3
    assert [r.skip_reason for r in result] == [None, "no_usable_board", None]


async def test_renders_carry_the_frame_dimensions_back():
    """Label Studio stores keypoints as percentages, so a corner pixel is
    meaningless without the rectified frame it was measured in."""
    result, _ = await _run(_input(images=1), workflow_id="wf-lattice-dims")

    assert (result[0].width, result[0].height) == (4000, 3000)

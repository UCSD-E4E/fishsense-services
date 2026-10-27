"""Calibration v3's read-only replay.

`ReplayCalibrationV3ForMissionWorkflow`: the mission's calibration inputs, as
the automatic chain resolves them (`resolve_automatic_calibration_inputs`,
read-only), then v3 on the per-image role (the processor's
`ReplayAutomaticCalibrationV3Workflow`)
-- with draws of its slate frames when asked. Its result is the report.

`ReplayCalibrationV3Workflow`: every planned mission, `AT_ONCE` at a time; it
returns only which child holds each mission's report (`collect` in `replay`
reads them), so no result nears Temporal's payload limit. Nothing is written.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_contracts import (
        PROCESSOR_GPU_TASK_QUEUE,
        PROCESSOR_TASK_QUEUE,
    )
    from fishsense_services_contracts.shadow_calibration import (
        SegmentSlateFrame,
        SegmentSlateFramesInput,
        SlateSegment,
    )
    from fishsense_services_contracts.calibration_v4 import (
        SlatePose,
        SlatePoseFrame,
        SlatePoseFramesInput,
    )
    from fishsense_services_contracts.object_store import ObjectRef
    from fishsense_services_orchestrator.nrp.gpu_fallback import MODE_GPU
    from fishsense_services_orchestrator.slates.contracts import SlatePdfTarget
    from fishsense_services_contracts.calibration_v3 import (
        CalibrationV3Report,
        ReplayCalibrationInput,
    )
    from fishsense_services_orchestrator.automatic_results.activities import (
        AutomaticCalibrationPlan,
        AutomaticTarget,
    )
    from fishsense_services_orchestrator.calibration_v3.activities import TemplateTarget
    from fishsense_services_orchestrator.nrp.workflow import (
        wake_gpu_processor,
        wake_per_image_processor,
    )

__all__ = [
    "AT_ONCE",
    "pose_slate_frames",
    "ReplayCalibrationV3ForMissionWorkflow",
    "ReplayCalibrationV3Workflow",
    "ReplayRequest",
    "ReplayTarget",
    "replay_child_id",
]

AT_ONCE = 4
_DB = dict(schedule_to_close_timeout=timedelta(minutes=15),
           retry_policy=RetryPolicy(maximum_attempts=3))  # fmt: skip


@dataclass
class ReplayTarget:
    tenant_id: uuid.UUID
    mission_id: uuid.UUID
    draw_sizes: List[int] = field(default_factory=list)
    draws_per_size: int = 0
    #: ``sift`` (pair ratios), ``sam`` (each slate's SAM mask: strategy 2) or
    #: ``pose`` (each slate posed automatically: calibration v4).
    size_source: str = "sift"
    #: Pose with this template (by v1 id) instead of the mission's own: the
    #: one identified on its frames, when it records none (2026-10-24).
    slate_template_v1: Optional[int] = None


@dataclass
class ReplayRequest:
    tenant: str
    #: Mission numbers to replay; empty: every mission with an automatic dot.
    numbers: List[int] = field(default_factory=list)
    #: Missions whose slate frames are also drawn from (the small-n control).
    draw_numbers: List[int] = field(default_factory=list)
    draw_sizes: List[int] = field(default_factory=list)
    draws_per_size: int = 0
    size_source: str = "sift"
    #: Mission number (as text: JSON keys) -> the template, by v1 id, to pose
    #: it with (`ReplayTarget.slate_template_v1`).
    templates: Dict[str, int] = field(default_factory=dict)


#: Slate frames per SAM child: each carries its render's presigned GET.
SEGMENT_CHUNK = 400


def replay_child_id(run_id: str, mission_id: str) -> str:
    return f"calibration-v3-replay-{run_id}-{mission_id}"


async def pose_slate_frames(
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    plan: AutomaticCalibrationPlan,
    template: dict,
    *,
    child_prefix: str,
) -> Optional[List[SlatePose]]:
    """Each of the plan's slate frames posed on a real GPU from `template`
    (calibration v4), a child per `SEGMENT_CHUNK` named `child_prefix`-n; None
    without a real GPU. The replay's, and the automatic chain's (0078)."""
    if await wake_gpu_processor() != MODE_GPU:
        return None
    await workflow.execute_activity(
        "stage_slate_pdf",
        SlatePdfTarget(tenant_id=tenant_id,
                       slate_template_id=uuid.UUID(template["slate_template_id"])),
        schedule_to_close_timeout=timedelta(minutes=5),
        retry_policy=RetryPolicy(non_retryable_error_types=["NotAMember"]),
        result_type=bool,
    )  # fmt: skip
    frames = [SlatePoseFrame(capture_id=f.capture_id, render_url=f.jpeg_url,
                             dot=[f.x, f.y])
              for f in plan.payload.frames if f.jpeg_url]  # fmt: skip
    chunks = await asyncio.gather(*[
        workflow.execute_child_workflow(
            "SlatePoseFramesWorkflow",
            SlatePoseFramesInput(
                tenant_id=tenant_id, mission_id=mission_id,
                camera_matrix=plan.payload.camera_matrix,
                slate_pdf=ObjectRef.model_validate(template["slate_pdf"]),
                slate_dpi=template["dpi"],
                reference_points=template["reference_points"],
                frames=frames[at : at + SEGMENT_CHUNK]),
            id=f"{child_prefix}-{n}",
            task_queue=PROCESSOR_GPU_TASK_QUEUE,
            execution_timeout=timedelta(hours=3),
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            result_type=List[SlatePose],
        )
        for n, at in enumerate(range(0, len(frames), SEGMENT_CHUNK))
    ])  # fmt: skip
    return [p for chunk in chunks for p in chunk]


@workflow.defn(name="ReplayCalibrationV3ForMissionWorkflow")
class ReplayCalibrationV3ForMissionWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, target: ReplayTarget) -> CalibrationV3Report:
        plan: AutomaticCalibrationPlan = await workflow.execute_activity(
            "resolve_automatic_calibration_inputs",
            AutomaticTarget(target.tenant_id, target.mission_id),
            result_type=AutomaticCalibrationPlan, **_DB,
        )  # fmt: skip
        if not plan.payload.frames:
            return CalibrationV3Report(mission_id=target.mission_id, outcome="refused",
                                       refusal_reason="no_candidates")  # fmt: skip
        sam_sizes: dict = {}
        pose_points: dict = {}
        poses: List[SlatePose] = []
        if target.size_source == "pose":
            posed = await self._pose(target, plan)
            if isinstance(posed, CalibrationV3Report):
                return posed
            poses = posed
            # Every pose is reported; only a strong one is fitted (its
            # status says so: `POSE_ECC_MIN`).
            pose_points = {str(p.capture_id): p.point if p.status == "posed" else None
                           for p in poses}  # fmt: skip
        if target.size_source == "sam":
            # Strategy 2: each slate frame's SAM mask under its dot, on the
            # shadow calibration's segmentation (GPU; only a real one).
            if await wake_gpu_processor() != MODE_GPU:
                return CalibrationV3Report(mission_id=target.mission_id,
                                           size_source="sam", outcome="refused",
                                           refusal_reason="no_gpu")  # fmt: skip
            frames = [SegmentSlateFrame(capture_id=f.capture_id, render_url=f.jpeg_url,
                                        dots=[[f.x, f.y]])
                      for f in plan.payload.frames if f.jpeg_url]  # fmt: skip
            chunks = await asyncio.gather(*[
                workflow.execute_child_workflow(
                    "SegmentSlateFramesWorkflow",
                    SegmentSlateFramesInput(tenant_id=target.tenant_id,
                                            mission_id=target.mission_id,
                                            frames=frames[at : at + SEGMENT_CHUNK]),
                    id=f"{workflow.info().workflow_id}-segment-{n}",
                    task_queue=PROCESSOR_GPU_TASK_QUEUE,
                    execution_timeout=timedelta(hours=3),
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    result_type=List[SlateSegment],
                )
                for n, at in enumerate(range(0, len(frames), SEGMENT_CHUNK))
            ])  # fmt: skip
            for segment in (s for chunk in chunks for s in chunk):
                ok = segment.status == "segmented" and segment.dots
                sam_sizes[str(segment.capture_id)] = (
                    segment.dots[0].size if ok else None
                )
        await wake_per_image_processor()
        report = await workflow.execute_child_workflow(
            "ReplayAutomaticCalibrationV3Workflow",
            ReplayCalibrationInput(payload=plan.payload, draw_sizes=target.draw_sizes,
                                   draws_per_size=target.draws_per_size,
                                   size_source=target.size_source, sam_sizes=sam_sizes,
                                   pose_points=pose_points),
            id=f"{workflow.info().workflow_id}-fit",
            task_queue=PROCESSOR_TASK_QUEUE,
            execution_timeout=timedelta(hours=3),
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            result_type=CalibrationV3Report,
        )  # fmt: skip
        return report.model_copy(update={"poses": poses}) if poses else report

    async def _pose(self, target: ReplayTarget, plan: AutomaticCalibrationPlan):
        """Each slate frame posed on a real GPU (calibration v4), or the
        refusal: no template that can pose a slate, or no GPU."""
        if target.slate_template_v1 is not None:
            template: Optional[dict] = await workflow.execute_activity(
                "resolve_slate_template_by_v1",
                TemplateTarget(target.tenant_id, target.slate_template_v1),
                result_type=Optional[dict], **_DB,
            )  # fmt: skip
        else:
            template = await workflow.execute_activity(
                "resolve_mission_slate_template",
                AutomaticTarget(target.tenant_id, target.mission_id),
                result_type=Optional[dict], **_DB,
            )  # fmt: skip
        if template is None:
            return CalibrationV3Report(mission_id=target.mission_id, size_source="pose",
                                       outcome="refused",
                                       refusal_reason="no_slate_template")  # fmt: skip
        posed = await pose_slate_frames(
            target.tenant_id, target.mission_id, plan, template,
            child_prefix=f"{workflow.info().workflow_id}-pose",
        )  # fmt: skip
        if posed is None:
            return CalibrationV3Report(mission_id=target.mission_id, size_source="pose",
                                       outcome="refused", refusal_reason="no_gpu")  # fmt: skip
        return posed


@workflow.defn(name="ReplayCalibrationV3Workflow")
class ReplayCalibrationV3Workflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, request: ReplayRequest) -> dict:
        plan: dict = await workflow.execute_activity(
            "plan_calibration_v3_replay", args=(request.tenant, request.numbers),
            result_type=dict, **_DB,
        )  # fmt: skip
        run_id = workflow.info().workflow_id
        rows: list = []

        async def one(m: dict) -> dict:
            draw = m["number"] in set(request.draw_numbers)
            child = replay_child_id(run_id, m["mission_id"])
            named = request.templates.get(str(m["number"]))
            row = {"number": m["number"], "mission_id": m["mission_id"], "child": child,
                   "slate_template_v1": named}  # fmt: skip
            try:
                await workflow.execute_child_workflow(
                    ReplayCalibrationV3ForMissionWorkflow.run,
                    ReplayTarget(uuid.UUID(plan["tenant_id"]), uuid.UUID(m["mission_id"]),
                                 request.draw_sizes if draw else [],
                                 request.draws_per_size if draw else 0,
                                 request.size_source, named),
                    id=child, id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    result_type=CalibrationV3Report,
                )  # fmt: skip
            except ChildWorkflowError as exc:
                row["error"] = str(exc.cause or exc)
            return row

        missions = plan["missions"]
        for at in range(0, len(missions), AT_ONCE):
            rows += await asyncio.gather(*[one(m) for m in missions[at : at + AT_ONCE]])
        return {"tenant_id": plan["tenant_id"], "missions": rows}

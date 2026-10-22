"""The shadow calibration's orchestrator activities.

- `select_next_mission_for_shadow_calibration`: across tenants, the next
  mission with slate frames and no result at this version (calibrated
  missions first; `shadow_calibration_store`).
- `resolve_shadow_calibration_inputs`: its slate frames, both dots, the line,
  the camera and the reference, read-only, and each frame's render by
  presigned GET (the render stage's, at its version under the mission's
  camera). A frame with no render yet is left out and counted.
- `assemble_shadow_fit`: the fit's payload, from the plan and SAM's results.
- `persist_shadow_calibration`: the fit's results, into the shadow tables
  only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from temporalio import activity

from fishsense_services_api.shadow_calibration_store import (
    ShadowCalibrationRow,
    ShadowFrameRow,
)
from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
)
from fishsense_services_contracts.renders import RENDER_VERSION
from fishsense_services_contracts.shadow_calibration import (
    SHADOW_ALGORITHM_VERSION,
    FitShadowCalibrationInput,
    SegmentSlateFrame,
    SegmentSlateFramesInput,
    ShadowFitFrame,
    ShadowFitResult,
    SlateSegment,
)

__all__ = ["ShadowCalibrationActivities", "ShadowPlan", "ShadowTarget"]


@dataclass
class ShadowTarget:
    tenant_id: uuid.UUID
    mission_id: uuid.UUID


@dataclass
class ShadowPlan:
    """What the mission's run needs: SAM's input, and per frame what the fit
    reads besides SAM's answer (in `fit`, sizes left empty)."""

    segment: SegmentSlateFramesInput
    fit: FitShadowCalibrationInput
    #: Slate frames left out for want of a render.
    unrendered: int = 0
    #: Per frame (by capture), which `dots` index each source's dot is.
    dot_index: Dict[str, Dict[str, int]] = field(default_factory=dict)


class ShadowCalibrationActivities:
    def __init__(self, *, catalog: Any, renders: Any) -> None:
        #: `ShadowCalibrationCatalog`.
        self._catalog = catalog
        #: `renders.urls.TenantRenderUrls`.
        self._renders = renders

    @activity.defn(name="select_next_mission_for_shadow_calibration")
    async def select_next_mission_for_shadow_calibration(
        self, exclude: Optional[List[uuid.UUID]] = None
    ) -> Optional[ShadowTarget]:
        for tenant_id in await self._catalog.member_tenants():
            mission_id = await self._catalog.next_mission_for_shadow_calibration(
                tenant_id, algorithm_version=SHADOW_ALGORITHM_VERSION,
                exclude=list(exclude or []),
            )  # fmt: skip
            if mission_id is not None:
                return ShadowTarget(tenant_id, mission_id)
        return None

    @activity.defn(name="resolve_shadow_calibration_inputs")
    async def resolve_shadow_calibration_inputs(
        self, target: ShadowTarget
    ) -> ShadowPlan:
        inputs = await self._catalog.shadow_calibration_inputs(
            target.tenant_id, target.mission_id,
            headtail_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
        )  # fmt: skip
        frames = [f for f in inputs.frames if f.label_dot or f.detector_dot]
        renders = (
            await self._renders.at(
                target.tenant_id, [f.capture_id for f in frames],
                render_version=RENDER_VERSION,
                camera_calibration_id=inputs.camera_calibration_id,
            )  # fmt: skip
            if frames
            else {}
        )
        rendered = [f for f in frames if (r := renders.get(f.capture_id)) is not None
                    and r.status == "rendered"]  # fmt: skip
        presign = await self._renders.presigner(target.tenant_id) if rendered else None
        segment, index = [], {}
        by_source: Dict[str, List[ShadowFitFrame]] = {"labels": [], "detector": []}
        for f in rendered:
            dots, idx = [], {}
            for source, dot in (("labels", f.label_dot), ("detector", f.detector_dot)):
                if dot is not None:
                    idx[source] = len(dots)
                    dots.append([float(dot[0]), float(dot[1])])
            index[str(f.capture_id)] = idx
            segment.append(SegmentSlateFrame(capture_id=f.capture_id,
                                             render_url=presign(renders[f.capture_id].key),
                                             dots=dots))  # fmt: skip
            common = dict(capture_id=f.capture_id, frame_source=f.frame_source,
                          slate_probability=f.slate_probability,
                          capture_number=f.capture_number, captured_at=f.captured_at)  # fmt: skip
            if f.label_dot is not None:
                by_source["labels"].append(ShadowFitFrame(
                    **common, dot_x=f.label_dot[0], dot_y=f.label_dot[1],
                    dot_label_id=f.label_id, dot_label_source=f.label_source))  # fmt: skip
            if f.detector_dot is not None:
                by_source["detector"].append(ShadowFitFrame(
                    **common, dot_x=f.detector_dot[0], dot_y=f.detector_dot[1],
                    dot_prediction_id=f.prediction_id,
                    dot_confidence=f.detector_confidence))  # fmt: skip
        if len(rendered) < len(frames):
            activity.logger.warning(
                "mission=%s: %d of %d slate frames have no render yet; left out",
                target.mission_id, len(frames) - len(rendered), len(frames),
            )  # fmt: skip
        return ShadowPlan(
            segment=SegmentSlateFramesInput(tenant_id=target.tenant_id,
                                            mission_id=target.mission_id, frames=segment),
            fit=FitShadowCalibrationInput(
                tenant_id=target.tenant_id, mission_id=target.mission_id,
                mission_number=inputs.mission_number,
                camera_calibration_id=inputs.camera_calibration_id,
                camera_matrix=inputs.camera_matrix,
                mission_line=list(inputs.mission_line) if inputs.mission_line else None,
                reference_laser_calibration_id=inputs.reference_laser_calibration_id,
                reference_borrowed=inputs.reference_borrowed,
                reference_position=inputs.reference_position,
                reference_axis=inputs.reference_axis,
                frames=by_source,
                mission_detector_dots=[list(d) for d in inputs.mission_detector_dots],
                sam_model="",
            ),
            unrendered=len(frames) - len(rendered),
            dot_index=index,
        )  # fmt: skip

    @activity.defn(name="assemble_shadow_fit")
    async def assemble_shadow_fit(
        self, plan: ShadowPlan, segments: List[SlateSegment]
    ) -> FitShadowCalibrationInput:
        """SAM's answer under each frame's dot, onto the fit's frames. A frame
        SAM never answered for (left out after its tries, or its render gone)
        stays with no size: the fit counts it as no mask at the dot."""
        by_capture = {str(s.capture_id): s for s in segments if s.status == "segmented"}
        models = {s.sam_model for s in by_capture.values() if s.sam_model}
        scales = [s.render_scale for s in by_capture.values() if s.render_scale]
        frames = {}
        for source, fs in plan.fit.frames.items():
            out = []
            for f in fs:
                seg = by_capture.get(str(f.capture_id))
                d = (None if seg is None
                     else seg.dots[plan.dot_index[str(f.capture_id)][source]])  # fmt: skip
                out.append(f.model_copy(update={} if d is None else {
                    "sam_prompt": d.prompt, "sam_score": d.score,
                    "mask_area_px": d.mask_area_px, "size": d.size}))  # fmt: skip
            frames[source] = out
        return plan.fit.model_copy(update={
            "frames": frames,
            "render_scale": float(sorted(scales)[len(scales) // 2]) if scales else 1.0,
            "sam_model": ",".join(sorted(models)) or "none",
        })  # fmt: skip

    @activity.defn(name="persist_shadow_calibration")
    async def persist_shadow_calibration(
        self, target: ShadowTarget, result: ShadowFitResult
    ) -> int:
        if result.mission_id != target.mission_id:
            raise ValueError(f"a shadow result for mission {result.mission_id}")
        rows = [
            ShadowCalibrationRow(
                **c.model_dump(exclude={"frames"}),
                frames=[ShadowFrameRow(**f.model_dump()) for f in c.frames],
            )
            for c in result.calibrations
        ]
        ids = await self._catalog.persist_shadow_calibrations(
            target.tenant_id, target.mission_id, rows
        )
        activity.logger.info("shadow calibration mission=%s: %d results stored",
                             target.mission_id, len(ids))  # fmt: skip
        return len(ids)

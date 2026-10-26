"""The v2 processing contract: what the orchestrator and processor exchange.

PLAN.md §9.1. v2-owned and schema-first, informed by v1's
``fishsense_shared.preprocess_contracts`` and ``task_queues`` at
fishsense-lite@a8b2c3bc, but not a rename of them: ids are v2 UUIDs, and the
queues are v2's own.

**Versioned.** The two sides deploy independently (the orchestrator in the
Incus slot, the processor on NRP, often days apart), so a shape changed on one
side fails at runtime on the other. ``CONTRACT_VERSION`` names the agreement
and ``schemas/v{N}.json`` publishes it; the tests fail if a model changes
without a new version. To change the contract: bump the version, then
``python -m fishsense_services_contracts > schemas/v{N}.json``.
"""

import importlib
import inspect
import pkgutil
from typing import Any

from pydantic import BaseModel
from pydantic_settings import BaseSettings

from fishsense_services_contracts.clustering import (
    ClusterMissionFrameImage,
    ClusterMissionFramesInput,
)
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.task_queues import (
    PROCESSOR_GPU_TASK_QUEUE,
    PROCESSOR_LIGHT_TASK_QUEUE,
    PROCESSOR_TASK_QUEUE,
)

#: v22 (2026-10-25): an automatic frame's dot carries its colour
#: (`AutomaticFrameResult.laser_color`/`laser_color_margin`), which the laser
#: kernel always read and the frames stage dropped. Optional; v21 parses.
#: v21 (2026-10-23): a laser dot is refused unless fishsense-core's
#: `LaserPoint.is_measurable()` passes (core #96): `low_parallax` joins the
#: laser-depth refusal's reasons (`laser_depth.DotRefusal`), and stage 14's
#: `FishLength.refusal` takes all three. Values added only; v20 parses.
#: v20 (2026-10-23): BioCLIP with fish-model classes, read-only (`species_models`:
#: `SpeciesModelsInput`, `SpeciesModelsResult`). New models only; v19 parses.
#: v19 (2026-10-23): the SimCC head/tail keypoint replay (`headtail_keypoints`:
#: `KeypointFramesInput`, `KeypointResult`). New models only; v18 parses.
#: v18 (2026-10-23): calibration v4's slate pose (`calibration_v4`:
#: `SlatePoseFramesInput`, `SlatePose`), and the replay fitting on its points
#: (`ReplayCalibrationInput.size_source="pose"`/`pose_points`,
#: `CalibrationV3Report.poses`). New models and optional fields; v17 parses.
#: v17 (2026-10-23): the replay records each SAM-size fit's predicted
#: length-scale SD (`CalibrationV3Report.scale_sd`, `CalibrationDraw.scale_sd`).
#: Optional fields; v16 parses.
#: v16 (2026-10-22): the replay may fit on SAM mask sizes
#: (`ReplayCalibrationInput.size_source`/`sam_sizes`,
#: `CalibrationV3Report.size_source`). Optional fields; v15 parses.
#: v15 (2026-10-22): label-free calibration v3's read-only replay
#: (`calibration_v3`: `ReplayCalibrationInput`, `CalibrationV3Report`). New
#: models only.
#: v14 (2026-10-22): the mask backfill (`mask_backfill`): an existing
#: prediction's SAM mask regenerated and stored only when it reproduces the
#: prediction (`BackfillMasksInput`, `BackfilledMask`). New models only.
#: v13 (2026-10-21): the shadow size-constancy laser calibration
#: (`shadow_calibration`): SAM 3.1 slate segmentation of each slate frame's
#: render (`SegmentSlateFramesInput`, `SlateSegment`) and the fit's payloads
#: (`FitShadowCalibrationInput`, `ShadowFitResult`). New models only.
#: v12 (2026-10-21): SAM masks stored as 8-bit greyscale PNGs in the tenant's
#: bucket, as the app's ``mask_png`` (`masks`):
#: a frame to predict may carry a mask's key and PUT (`AutomaticFrame.mask`,
#: `PredictHeadtailImage.mask`), and a result that kept a mask names it as
#: stored (`AutomaticFrameResult.mask`, `HeadtailPredictionResult.mask`). All
#: optional, so v11 payloads still parse.
#: v11 (2026-10-20): thumbnails -- each render scaled for /fish's cards, made
#: from the stored render (`thumbnails`). New models only; v10 parses.
#: v10 (2026-10-20): a render frame may also carry its raw's copy into the
#: tenant's bucket (`RenderFrame.raw_key`/`raw_put_url`/`checksum`), and may owe
#: only that (`key`/`put_url` optional); a result reports the copy
#: (`RenderResult.raw_status`/`raw_bytes`/`raw_md5`) and ``not_asked`` for a
#: render it did not owe. A v9 frame still parses, owing its render alone.
#: v9 (2026-10-19): renders -- every capture's raw rectified to a JPEG once, in
#: its tenant's bucket (`renders`); readers take it by presigned URL
#: (`jpeg_url`, `AutomaticFrame.render_url`), and the frames stage stops
#: writing (`AutomaticFrame.jpeg` optional). New fields optional, v8 parses.
#: v8 (2026-10-19): the automatic frames carry their render's version and camera
#: (`AutomaticFrame.jpeg_render_version`, `PredictAutomaticFrame(s)Input.
#: camera_calibration_id`, `AutomaticFrameResult.jpeg_render_version` and
#: `jpeg_camera_calibration_id`); all optional, so v7 payloads still parse.
#: v7 (2026-10-18, Phase 2a): the models renamed dive -> mission. Every payload's
#: JSON is unchanged -- each renamed field keeps its old name on the wire
#: (`renamed.py`) -- so a v6 processor and a v7 orchestrator interoperate; only
#: the published schema's model names differ.
CONTRACT_VERSION = 22


def _discover_models() -> tuple[type[BaseModel], ...]:
    """Every payload model the package defines, by module then definition
    order. A model defined here *is* contract; settings (how each side
    connects) are not. Discovered rather than listed, so no slice can add one
    and forget it."""
    found: list[type[BaseModel]] = []
    for module in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if module.name.startswith("_"):
            continue
        loaded = importlib.import_module(f"{__name__}.{module.name}")
        for obj in vars(loaded).values():
            if (
                inspect.isclass(obj)
                and issubclass(obj, BaseModel)
                and not issubclass(obj, BaseSettings)
                and obj.__module__ == loaded.__name__
                and obj not in found
            ):
                found.append(obj)
    return tuple(found)


#: Every model that crosses the orchestrator/processor boundary.
MODELS = _discover_models()

__all__ = [
    "CONTRACT_VERSION",
    "MODELS",
    "PROCESSOR_GPU_TASK_QUEUE",
    "PROCESSOR_LIGHT_TASK_QUEUE",
    "PROCESSOR_TASK_QUEUE",
    "ClusterMissionFrameImage",
    "ClusterMissionFramesInput",
    "ObjectRef",
    "json_schema",
]


def json_schema() -> dict[str, Any]:
    """The contract as published: each model's JSON Schema, by name.

    Structural only -- descriptions (pydantic copies docstrings in) are dropped,
    so rewording a comment is not a contract change.
    """
    return {model.__name__: _structural(model.model_json_schema()) for model in MODELS}


def _structural(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _structural(v) for k, v in schema.items() if k != "description"}
    if isinstance(schema, list):
        return [_structural(v) for v in schema]
    return schema

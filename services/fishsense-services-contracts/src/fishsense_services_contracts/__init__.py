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

from typing import Any

from fishsense_services_contracts.clustering import (
    ClusterDiveFrameImage,
    ClusterDiveFramesInput,
)
from fishsense_services_contracts.task_queues import (
    PROCESSOR_GPU_TASK_QUEUE,
    PROCESSOR_LIGHT_TASK_QUEUE,
    PROCESSOR_TASK_QUEUE,
)

CONTRACT_VERSION = 1

#: Every model that crosses the orchestrator/processor boundary.
MODELS = (ClusterDiveFrameImage, ClusterDiveFramesInput)

__all__ = [
    "CONTRACT_VERSION",
    "MODELS",
    "PROCESSOR_GPU_TASK_QUEUE",
    "PROCESSOR_LIGHT_TASK_QUEUE",
    "PROCESSOR_TASK_QUEUE",
    "ClusterDiveFrameImage",
    "ClusterDiveFramesInput",
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

"""Stage 1 (dive-frame clustering): the orchestrator's input to the processor.

The kernel only needs ``(capture_id, taken_datetime)`` -- image bytes are never
read -- so the orchestrator resolves them and the processor gets nothing else.
It returns the capture ids grouped into clusters (``list[list[UUID]]``).
"""

from uuid import UUID
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict
from fishsense_services_contracts.renamed import renamed_from


class ClusterMissionFrameImage(BaseModel):
    """One canonical capture's timestamp."""

    capture_id: UUID
    #: Aware: a naive time is ambiguous by exactly the camera's UTC offset, and
    #: clustering is timestamp arithmetic.
    taken_datetime: AwareDatetime


class ClusterMissionFramesInput(BaseModel):
    model_config = ConfigDict(validate_by_name=True)
    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
    images: list[ClusterMissionFrameImage]

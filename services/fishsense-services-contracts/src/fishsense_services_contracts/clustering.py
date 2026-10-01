"""Stage 1 (dive-frame clustering): the orchestrator's input to the processor.

The kernel only needs ``(capture_id, taken_datetime)`` -- image bytes are never
read -- so the orchestrator resolves them and the processor gets nothing else.
It returns the capture ids grouped into clusters (``list[list[UUID]]``).
"""

from uuid import UUID

from pydantic import AwareDatetime, BaseModel


class ClusterDiveFrameImage(BaseModel):
    """One canonical capture's timestamp."""

    capture_id: UUID
    #: Aware: a naive time is ambiguous by exactly the camera's UTC offset, and
    #: clustering is timestamp arithmetic.
    taken_datetime: AwareDatetime


class ClusterDiveFramesInput(BaseModel):
    dive_id: UUID
    images: list[ClusterDiveFrameImage]

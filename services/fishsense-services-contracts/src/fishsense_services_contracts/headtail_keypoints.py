"""The SimCC head/tail keypoint replay (v19, owner 2026-10-23): fishsense-core's
snout/fork model run on each automatic head/tail prediction's frame,
read-only, to compare its lengths with the SAM rule's before any switch. The
checkpoint is core's pin, the same in 4.2.0 and 4.3.0 (the processor-secret
sync's tests check it against core's manifest).

Each frame is the render the prediction was made on and its SAM mask box (the
model's crop is cut from that box). The model runs twice: on the decoded
render (`head`/`tail`) and on its crop JPEG-encoded again (`rt_*`): the
experiment's crops were JPEG files, an input gap core's docstring says is
unmeasured. New models only."""

from __future__ import annotations

from typing import Annotated, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from fishsense_services_contracts.renamed import renamed_from

__all__ = ["KeypointFrame", "KeypointFramesInput", "KeypointResult"]


class KeypointFrame(BaseModel):
    prediction_id: UUID
    capture_id: UUID
    #: The render the prediction was made on (presigned GET).
    render_url: str
    #: Its SAM mask box, ``[x0, y0, x1, y1)`` in render pixels.
    mask_bbox: List[int]


class KeypointFramesInput(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    tenant_id: UUID
    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
    frames: List[KeypointFrame]


class KeypointResult(BaseModel):
    """`status`: ``predicted``, ``render_unavailable``, ``decode_failed``,
    ``refused`` (the model refused the box), ``failed`` (every try)."""

    prediction_id: UUID
    capture_id: UUID
    status: str
    head: Optional[List[float]] = None
    tail: Optional[List[float]] = None
    confidence: Optional[float] = None
    crop: Optional[List[int]] = None
    #: The same crop JPEG-encoded again (quality 95), as the experiment's.
    rt_head: Optional[List[float]] = None
    rt_tail: Optional[List[float]] = None
    rt_confidence: Optional[float] = None
    checkpoint: Optional[str] = None

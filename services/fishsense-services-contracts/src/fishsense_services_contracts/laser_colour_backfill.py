"""The laser-colour backfill (v24, the owner, 2026-10-26: "I need the data").

Automatic dots predicted before contract v22 (#375) kept no colour: the frames
stage dropped what the laser kernel read. Head/tail v5 (#401) writes keypoints
to their own table and never runs the dot step again, so nothing was going to
fill them in. The backfill reads each dot's colour off the render its
prediction was made on, at the dot (render pixels, the frame the dot is in),
with fishsense-core's `classify_laser_color`: the live kernel's rule, on the
8-bit render rather than the linear raw. That change of input is why a
reading names its source and version, and why the run reports its agreement
with the labellers' colours and the live kernel's before anything is written.
New models only."""

from __future__ import annotations

from typing import Annotated, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from fishsense_services_contracts.renamed import renamed_from

__all__ = [
    "COLOUR_READING_SOURCE",
    "COLOUR_READING_STATUSES",
    "COLOUR_READING_VERSION",
    "ColourFrame",
    "ColourFramesInput",
    "ColourReading",
]

#: Where a backfilled colour was read: the prediction's render.
COLOUR_READING_SOURCE = "render"
#: Bumped by hand when a reading would change for an unchanged render.
COLOUR_READING_VERSION = 1
#: ``read`` (a colour, or too close to call), or why there is no reading.
COLOUR_READING_STATUSES = ("read", "render_unavailable", "decode_failed", "failed")


class ColourFrame(BaseModel):
    prediction_id: UUID
    capture_id: UUID
    #: The render the prediction was made on (presigned GET).
    render_url: str
    #: The automatic dot, in that render's pixels.
    x: float
    y: float


class ColourFramesInput(BaseModel):
    model_config = ConfigDict(validate_by_name=True)

    tenant_id: UUID
    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
    frames: List[ColourFrame]


class ColourReading(BaseModel):
    """`color` None with a `margin`: read, but too close to call (core's
    ``|margin| < MIN_COLOR_MARGIN``). Both None: no reading."""

    prediction_id: UUID
    capture_id: UUID
    status: Literal["read", "render_unavailable", "decode_failed", "failed"]
    color: Optional[Literal["red", "green"]] = None
    #: Signed R-G in 8-bit levels, positive for red (core's).
    margin: Optional[float] = None

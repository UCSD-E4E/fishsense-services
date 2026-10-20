"""Each render's thumbnail: what /fish's cards show (new in v2, 2026-10-20).

A render is the raw rectified at full size (4014x3016, ~6-8 MB): as a card on
/fish, a page of 50 would download ~300 MB (review of #151). So each render
gets a thumbnail, ``THUMBNAIL_WIDTH`` wide, made *from the stored render* --
never the NAS -- by the processor, which holds no tenant credentials: the
orchestrator presigns a GET of the render and a PUT of the thumbnail, and
checks the stored thumbnail against the size and MD5 the processor reports
before recording it, as for a render.

**Where.** Beside its render under its own prefix, the width in the key
(`thumbnail_key`): ``thumbnails/w{width}/{render key}``. A new width writes new
objects; a render's thumbnail never overwrites another's.
"""

from __future__ import annotations

import re
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, model_validator

__all__ = [
    "THUMBNAIL_STATUSES",
    "THUMBNAIL_WIDTH",
    "ThumbnailFrame",
    "ThumbnailInput",
    "ThumbnailResult",
    "ThumbnailsInput",
    "thumbnail_key",
]

#: Pixels across; the height keeps the render's aspect.
THUMBNAIL_WIDTH = 640

#: ``made``; or the render was not there to read.
THUMBNAIL_STATUSES = ("made", "render_unavailable")

_MD5 = re.compile(r"[0-9a-f]{32}")


def thumbnail_key(render_key: str, width: int = THUMBNAIL_WIDTH) -> str:
    """Where a render's thumbnail is, in its tenant's bucket."""
    if not render_key.startswith("renders/"):
        raise ValueError(f"not a render's key: {render_key!r}")
    return f"thumbnails/w{width}/{render_key}"


class ThumbnailFrame(BaseModel):
    """One render to thumbnail: where to read it, where to put the result."""

    render_id: UUID
    capture_id: UUID
    #: A presigned GET of the render.
    get_url: str
    #: `thumbnail_key` in the tenant's bucket, and a presigned PUT to it.
    key: str
    put_url: str


class ThumbnailsInput(BaseModel):
    """A tenant's renders to thumbnail (orchestrator -> processor)."""

    tenant_id: UUID
    width: int = THUMBNAIL_WIDTH
    frames: List[ThumbnailFrame]


class ThumbnailInput(BaseModel):
    """What one `make_thumbnail` activity runs on."""

    frame: ThumbnailFrame
    width: int = THUMBNAIL_WIDTH


class ThumbnailResult(BaseModel):
    """One render's thumbnail, or why there is none."""

    render_id: UUID
    status: str
    #: The bytes written, for the orchestrator to check the stored object.
    bytes: Optional[int] = None
    md5: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None

    @model_validator(mode="after")
    def _consistent(self) -> "ThumbnailResult":
        if self.status not in THUMBNAIL_STATUSES:
            raise ValueError(f"not a thumbnail status: {self.status!r}")
        made = self.status == "made"
        if made and not (
            self.bytes
            and self.bytes > 0
            and self.md5
            and _MD5.fullmatch(self.md5)
            and self.width
            and self.height
        ):
            raise ValueError("a thumbnail names its size, its MD5 and its pixels")
        if not made and (self.bytes is not None or self.md5 is not None):
            raise ValueError(f"a {self.status} thumbnail has no bytes")
        return self

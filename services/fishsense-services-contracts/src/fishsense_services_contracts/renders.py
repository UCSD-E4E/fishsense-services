"""Every capture's render: its raw rectified to a JPEG, once (new in v2,
2026-10-19).

The render is the image every step after the raw reads: SAM's mask and the
head/tail are found in it, species crops it, the label-free calibration takes
its features from it, and the portal shows it. Anything else -- v1's head/tail
JPEG above all -- is another image, and a label read against it is wrong.

**Where.** In the tenant's own bucket (PLAN.md §9.11), at `render_key`:
``renders/r{version}_{camera}/{checksum}.JPG``. The render version and the
camera calibration the raw was rectified with are both in the key, since
either changes the pixels: a re-render never overwrites the image older rows'
pixels belong to. Only the API holds the tenant's credentials, so the
orchestrator presigns: a PUT per render for the processor, a GET per render
for each reader.

**Versioned.** `RENDER_VERSION` names the rendering: the raw decoded and
rectified by fishsense-core with the camera's matrix and distortion, then
encoded (processor `headtail_preprocess.rectify_and_encode_jpeg`). Bump it
when a pixel would change. History: 1 (2026-10-19) as above.

**Verified.** A presigned PUT cannot sign bytes not rendered yet, so the
processor reports the render's size and MD5, and the orchestrator checks them
against the stored object before recording it.
"""

from __future__ import annotations

import re
import uuid
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, model_validator

from fishsense_services_contracts.object_store import ObjectRef

__all__ = [
    "RENDER_STATUSES",
    "RENDER_VERSION",
    "RenderCaptureInput",
    "RenderCapturesInput",
    "RenderFrame",
    "RenderResult",
    "render_key",
]

#: See the module docstring.
RENDER_VERSION = 1

#: ``rendered``; or why not: the raw was not in scratch, or would not decode.
RENDER_STATUSES = ("rendered", "raw_unavailable", "decode_failed")

_MD5 = re.compile(r"[0-9a-f]{32}")


def render_key(render_version: int, camera_calibration_id: UUID, checksum: str) -> str:
    """Where a capture's render is, in its tenant's bucket."""
    if (
        isinstance(render_version, bool)
        or not isinstance(render_version, int)
        or render_version < 1
    ):
        raise ValueError(f"a render version is a positive int, not {render_version!r}")
    if not isinstance(camera_calibration_id, uuid.UUID):
        raise ValueError(
            f"a camera calibration is a UUID, not {camera_calibration_id!r}"
        )
    if not checksum or not checksum.isalnum():
        raise ValueError(f"not a checksum: {checksum!r}")
    return f"renders/r{render_version}_{camera_calibration_id.hex}/{checksum}.JPG"


class RenderFrame(BaseModel):
    """One capture to render: its staged raw, and where the render goes."""

    capture_id: UUID
    raw: ObjectRef
    #: `render_key` in the tenant's bucket, for the record.
    key: str
    #: A presigned PUT to that key.
    put_url: str


class RenderCapturesInput(BaseModel):
    """A mission's captures to render (orchestrator -> processor)."""

    tenant_id: UUID
    mission_id: UUID
    render_version: int
    camera_calibration_id: UUID
    camera_matrix: List[List[float]]
    distortion_coefficients: List[float]
    frames: List[RenderFrame]


class RenderCaptureInput(BaseModel):
    """What one `render_capture` activity runs on."""

    frame: RenderFrame
    render_version: int
    camera_calibration_id: UUID
    camera_matrix: List[List[float]]
    distortion_coefficients: List[float]


class RenderResult(BaseModel):
    """One capture's render, or why there is none."""

    capture_id: UUID
    status: str
    render_version: int
    camera_calibration_id: UUID
    #: The bytes written, for the orchestrator to check the stored object.
    bytes: Optional[int] = None
    md5: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None

    @model_validator(mode="after")
    def _consistent(self) -> "RenderResult":
        if self.status not in RENDER_STATUSES:
            raise ValueError(f"not a render status: {self.status!r}")
        rendered = self.status == "rendered"
        if rendered and not (
            self.bytes and self.bytes > 0 and self.md5 and _MD5.fullmatch(self.md5)
        ):
            raise ValueError("a render names its size and its MD5 (32 hex)")
        if not rendered and (self.bytes is not None or self.md5 is not None):
            raise ValueError(f"a {self.status} capture has no bytes")
        return self

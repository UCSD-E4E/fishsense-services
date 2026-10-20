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

**The raw, copied in the same pass** (the owner, 2026-10-20: Lite's raws in
the tenant's bucket, as Mobile's are). The render stage already stages each
raw from the NAS, so a frame can also carry a presigned PUT for the raw, at
`raw_key` (``raws/{checksum}.ORF``). The processor uploads the bytes it read
and reports their size and MD5; the orchestrator records the copy only if the
stored object matches them and the MD5 is the capture's own checksum. A frame
owes a render, a raw copy, or both: a capture rendered before raws were
copied is owed only the copy, and is not rendered again (its stored render
must keep matching its recorded MD5). Its result is then ``not_asked`` for
the render. A frame of contract 9 owes its render and no raw.
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
    "RESULT_STATUSES",
    "RAW_STATUSES",
    "RenderResult",
    "raw_key",
    "render_key",
]

#: See the module docstring.
RENDER_VERSION = 1

#: ``rendered``; or why not: the raw was not in scratch, or would not decode.
RENDER_STATUSES = ("rendered", "raw_unavailable", "decode_failed")
#: A result's render status: also ``not_asked``, for a frame owing only its raw
#: copy. Never recorded.
RESULT_STATUSES = (*RENDER_STATUSES, "not_asked")
#: A raw copy's: ``copied``, or the raw was not in scratch to copy.
RAW_STATUSES = ("copied", "raw_unavailable")

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


def raw_key(checksum: str) -> str:
    """Where a capture's raw is copied, in its tenant's bucket."""
    if not checksum or not checksum.isalnum():
        raise ValueError(f"not a checksum: {checksum!r}")
    return f"raws/{checksum}.ORF"


class RenderFrame(BaseModel):
    """One capture: its staged raw, and what it owes -- a render, a copy of
    its raw in the tenant's bucket, or both."""

    capture_id: UUID
    raw: ObjectRef
    #: `render_key` in the tenant's bucket, and a presigned PUT to it; both
    #: None when the capture owes no render.
    key: Optional[str] = None
    put_url: Optional[str] = None
    #: The capture's checksum, which the copy's MD5 must be.
    checksum: Optional[str] = None
    #: `raw_key` in the tenant's bucket, and a presigned PUT to it; both None
    #: when the capture's raw is already there.
    raw_key: Optional[str] = None
    raw_put_url: Optional[str] = None

    @property
    def owes_render(self) -> bool:
        return self.put_url is not None

    @property
    def owes_raw(self) -> bool:
        return self.raw_put_url is not None

    @model_validator(mode="after")
    def _whole(self) -> "RenderFrame":
        if (self.key is None) != (self.put_url is None):
            raise ValueError("a render needs its key and its PUT")
        if (self.raw_key is None) != (self.raw_put_url is None):
            raise ValueError("a raw copy needs its key and its PUT")
        if self.owes_raw and not self.checksum:
            raise ValueError("a raw copy needs the checksum it must match")
        if not (self.owes_render or self.owes_raw):
            raise ValueError("a frame owes a render, a raw copy, or both")
        return self


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
    #: The raw's copy, when the frame owed one (`RAW_STATUSES`).
    raw_status: Optional[str] = None
    raw_bytes: Optional[int] = None
    raw_md5: Optional[str] = None

    @model_validator(mode="after")
    def _consistent(self) -> "RenderResult":
        if self.status not in RESULT_STATUSES:
            raise ValueError(f"not a render status: {self.status!r}")
        if self.raw_status is not None and self.raw_status not in RAW_STATUSES:
            raise ValueError(f"not a raw copy status: {self.raw_status!r}")
        copied = self.raw_status == "copied"
        if copied and not (
            self.raw_bytes
            and self.raw_bytes > 0
            and self.raw_md5
            and _MD5.fullmatch(self.raw_md5)
        ):
            raise ValueError("a copied raw names its size and its MD5 (32 hex)")
        if not copied and (self.raw_bytes is not None or self.raw_md5 is not None):
            raise ValueError("a raw not copied has no bytes")
        rendered = self.status == "rendered"
        if rendered and not (
            self.bytes and self.bytes > 0 and self.md5 and _MD5.fullmatch(self.md5)
        ):
            raise ValueError("a render names its size and its MD5 (32 hex)")
        if not rendered and (self.bytes is not None or self.md5 is not None):
            raise ValueError(f"a {self.status} capture has no bytes")
        return self

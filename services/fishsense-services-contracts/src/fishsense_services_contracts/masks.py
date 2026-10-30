"""A prediction's SAM mask, stored as a file in the tenant's bucket (v12).

New in v2 (2026-10-21). SAM's mask was dropped once its head/tail, box and area
were taken. FishSense Mobile stores its fish mask as a file in the tenant's
bucket, named in ``capture_objects``; the owner chose ours to match the app's
(2026-10-21). The app's mask is moving from raw ``mask_u8`` to ``mask_png``
(#230, docs/handoff/fishsense-mobile/README.md): an 8-bit grayscale PNG,
lossless, exactly the mask's width x height, nonzero on the fish -- a fraction
of the raw size (ours would be 12 MB raw at 4014x3016), and a browser draws it
as it is. Ours are that PNG (review of #232):

* **at the frame SAM segmented**: the crop SAM saw, placed in a frame-sized
  array, 255 on the fish and 0 elsewhere;
* **a new key per mask** (`mask_key`): a re-run never overwrites a mask an
  older prediction names; the prediction rows are append-only, and so are their
  masks;
* **checked before it is recorded**: the processor PUTs it through a presigned
  URL and reports its size and SHA-256; the orchestrator reads it back and
  checks both, and that its PNG header is 8-bit grayscale at the frame;
* **an addition, never a reason to fail**: a mask that can't be stored or
  doesn't check out costs only itself; its prediction stands.

Both stages that segment store theirs: the automatic track (the capture's
render) and the human-path head/tail predictor (its head/tail JPEG).
"""

from __future__ import annotations

import re
import uuid
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "KEPT_MASK_STATUSES",
    "MASK_CONTENT_TYPE",
    "MASK_SOURCES",
    "MaskSource",
    "MaskUpload",
    "StoredMask",
    "check_mask",
    "is_mask_of",
    "mask_key",
]

#: The stage whose prediction a mask belongs to.
MASK_SOURCES = ("automatic", "headtail")
MaskSource = Literal["automatic", "headtail"]
MASK_CONTENT_TYPE = "image/png"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PREFIX = "masks/"
_SUFFIX = ".png"


def mask_key(source: str, capture_id: UUID, mask_id: UUID) -> str:
    """Where one mask is, in its tenant's bucket: under its stage and its
    capture, at a key no other mask has. The capture is in the key so the
    orchestrator can tell a mask is the result's own (`is_mask_of`)."""
    if source not in MASK_SOURCES:
        raise ValueError(f"not a mask source: {source!r}")
    for name, value in (("capture", capture_id), ("mask", mask_id)):
        if not isinstance(value, uuid.UUID):
            raise ValueError(f"a {name} id is a UUID, not {value!r}")
    return f"{_PREFIX}{source}/{capture_id}/{mask_id}{_SUFFIX}"


def is_mask_of(key: str, source: str, capture_id: UUID) -> bool:
    """`key` is one of `mask_key`'s for this stage and capture."""
    return key.startswith(f"{_PREFIX}{source}/{capture_id}/") and key.endswith(_SUFFIX)


def _a_mask_key(value: str) -> str:
    if not value.startswith(_PREFIX) or not value.endswith(_SUFFIX):
        raise ValueError(f"not a mask key: {value!r}")
    return value


class MaskUpload(BaseModel):
    """Where to PUT a frame's mask, should the stage keep one."""

    key: str
    put_url: str

    _key = field_validator("key")(_a_mask_key)


class StoredMask(BaseModel):
    """A mask the processor PUT: its key, the PNG's size and SHA-256, and the
    frame it is in (the PNG's ``width x height``)."""

    key: str
    size: int = Field(gt=0)
    sha256: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    _key = field_validator("key")(_a_mask_key)

    @field_validator("sha256")
    @classmethod
    def _hex(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("a SHA-256 is 64 lowercase hex")
        return value


#: The statuses whose result kept a mask (its box is named): only these may
#: name a stored mask.
KEPT_MASK_STATUSES = ("predicted", "headtail_failed")


def check_mask(status: str, mask: StoredMask | None) -> None:
    """A result names a stored mask only when it kept one."""
    if mask is not None and status not in KEPT_MASK_STATUSES:
        raise ValueError(f"a {status} result kept no mask to store")

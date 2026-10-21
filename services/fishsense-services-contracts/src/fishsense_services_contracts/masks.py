"""A prediction's SAM mask, stored as a file in the tenant's bucket (v12).

New in v2 (2026-10-21). SAM's mask was dropped once its head/tail, box and area
were taken. FishSense Mobile syncs its own fish mask as ``mask_u8``: uint8,
width x height, nonzero = fish, in the pixels of the frame it measured
(docs/handoff/fishsense-mobile/SYNC.md), stored in the tenant's bucket and
named in ``capture_objects``. Ours are stored the same way (owner, 2026-10-21):

* **the app's encoding**, at the frame SAM segmented: uint8, ``width x
  height``, row by row, 255 on the fish and 0 elsewhere -- the crop SAM saw,
  placed in a frame-sized array;
* **gzip'd**: a 4014x3016 frame is 12 MB raw and a binary mask gzips to tens of
  KB (owner's choice, 2026-10-21). Gunzipped, it is the app's ``mask_u8``;
* **a new key per mask** (`mask_key`): a re-run never overwrites a mask an
  older prediction names; the prediction rows are append-only, and so are their
  masks;
* **checked before it is recorded**: the processor PUTs it through a presigned
  URL and reports its size and SHA-256; the orchestrator reads it back and
  checks both, and its gunzipped size against the frame, first.

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
MASK_CONTENT_TYPE = "application/gzip"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PREFIX = "masks/"


def mask_key(source: str, capture_id: UUID, mask_id: UUID) -> str:
    """Where one mask is, in its tenant's bucket: under its stage and its
    capture, at a key no other mask has. The capture is in the key so the
    orchestrator can tell a mask is the result's own (`is_mask_of`)."""
    if source not in MASK_SOURCES:
        raise ValueError(f"not a mask source: {source!r}")
    for name, value in (("capture", capture_id), ("mask", mask_id)):
        if not isinstance(value, uuid.UUID):
            raise ValueError(f"a {name} id is a UUID, not {value!r}")
    return f"{_PREFIX}{source}/{capture_id}/{mask_id}.u8.gz"


def is_mask_of(key: str, source: str, capture_id: UUID) -> bool:
    """`key` is one of `mask_key`'s for this stage and capture."""
    return key.startswith(f"{_PREFIX}{source}/{capture_id}/") and key.endswith(".u8.gz")


def _a_mask_key(value: str) -> str:
    if not value.startswith(_PREFIX) or not value.endswith(".u8.gz"):
        raise ValueError(f"not a mask key: {value!r}")
    return value


class MaskUpload(BaseModel):
    """Where to PUT a frame's mask, should the stage keep one."""

    key: str
    put_url: str

    _key = field_validator("key")(_a_mask_key)


class StoredMask(BaseModel):
    """A mask the processor PUT: its key, its gzip'd size and SHA-256, and the
    frame it is in (gunzipped, ``width x height`` bytes)."""

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

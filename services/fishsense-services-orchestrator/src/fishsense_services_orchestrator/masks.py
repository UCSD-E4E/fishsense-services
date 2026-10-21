"""Stored SAM masks: presigning their PUTs, checking them before they are
recorded, and deleting those that never are (contract 12, migration 0058).

New in v2 (2026-10-21). Both stages that segment -- the automatic track and
the human-path head/tail predictor -- PUT each kept mask to a key the
orchestrator presigned (`mask_upload`), as the app's ``mask_png`` (#230): an
8-bit greyscale PNG at the frame. The processor runs on machines we don't own,
so `checked_masks` reads each back before it is named against its prediction:
the key one of this stage's for the result's own capture, the bytes the ones
reported, the PNG 8-bit greyscale at the frame (from its header: nothing is
decompressed). A mask that fails is logged, left unrecorded, and deleted
(`discard`) -- its prediction still stands. Reads run `READS_AT_ONCE` at a
time, heartbeating (review of #232: one at a time, a page took minutes).
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import uuid
from collections.abc import Sequence
from typing import Any, Optional

from temporalio import activity

from fishsense_services_api.prediction_masks_store import PredictionMask
from fishsense_services_contracts.masks import (
    MASK_CONTENT_TYPE,
    MaskUpload,
    is_mask_of,
    mask_key,
)

__all__ = ["READS_AT_ONCE", "checked_masks", "discard", "mask_upload", "png_size"]

#: Mask reads in flight at once, per persist.
READS_AT_ONCE = 16
_PNG = b"\x89PNG\r\n\x1a\n"


def mask_upload(objects: Any, source: str, capture_id: uuid.UUID) -> MaskUpload:
    """A new key for one capture's mask, and its presigned PUT."""
    key = mask_key(source, capture_id, uuid.uuid4())
    return MaskUpload(key=key, put_url=objects.presign_mask_put(key).url)


def png_size(data: bytes) -> Optional[tuple[int, int, int, int]]:
    """``(width, height, bit depth, colour type)`` from a PNG's IHDR, or None
    for bytes that are not a PNG."""
    if len(data) < 26 or data[:8] != _PNG or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height, data[24], data[25]


def _why_not(objects: Any, source: str, result: Any) -> tuple[Optional[str], Any]:
    """``(why the mask fails, None)`` or ``(None, PredictionMask)``. Blocking."""
    m = result.mask
    if not is_mask_of(m.key, source, result.capture_id):
        return f"{m.key} is not a {source} mask of capture {result.capture_id}", None
    if (result.width, result.height) != (m.width, m.height):
        return (f"{m.key} is {m.width}x{m.height}, the frame "
                f"{result.width}x{result.height}"), None  # fmt: skip
    try:
        data = objects.get(m.key)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return f"reading {m.key} failed: {exc}", None
    if data is None:
        return f"{m.key} is not there", None
    if len(data) != m.size or hashlib.sha256(data).hexdigest() != m.sha256:
        return f"{m.key} is not the mask reported", None
    if png_size(data) != (m.width, m.height, 8, 0):
        return f"{m.key} is not an 8-bit greyscale PNG of {m.width}x{m.height}", None
    return None, PredictionMask(
        capture_id=result.capture_id, bucket=objects.bucket, key=m.key,
        sha256=m.sha256, size=m.size, content_type=MASK_CONTENT_TYPE,
        width=m.width, height=m.height,
    )  # fmt: skip


async def checked_masks(
    objects: Any, source: str, results: Sequence[Any]
) -> tuple[list[Optional[PredictionMask]], list[str]]:
    """``(masks parallel to results, keys that did not check out)``: each
    result's stored mask if it checks out (None where it has none or fails),
    and the failures' keys, for `discard`."""
    slots = asyncio.Semaphore(READS_AT_ONCE)

    async def one(result) -> tuple[Optional[str], Any]:
        if result.mask is None:
            return None, None
        async with slots:
            outcome = await asyncio.to_thread(_why_not, objects, source, result)
        activity.heartbeat()
        return outcome

    outcomes = await asyncio.gather(*(one(r) for r in results))
    masks, failed = [], []
    for result, (why, mask) in zip(results, outcomes, strict=True):
        if why is not None:
            activity.logger.warning(
                "capture=%s: mask not recorded: %s", result.capture_id, why
            )
            failed.append(result.mask.key)
        masks.append(mask)
    return masks, failed


async def discard(objects: Any, keys: Sequence[str]) -> None:
    """Delete masks that will never be recorded, so none is left behind
    unnamed (review of #232). Best effort: one that can't be deleted is
    logged, never a failure."""
    for key in keys:
        try:
            await asyncio.to_thread(objects.delete, key)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            activity.logger.warning("mask %s not deleted: %s", key, exc)

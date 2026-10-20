"""The render activity: one capture's raw in, its render in the tenant's
bucket out.

New in v2 (2026-10-19). The render is the image every step after the raw
reads (`fishsense_services_contracts.renders`): the head/tail stage-5.1
rendering (`headtail_preprocess.activities.rectify_and_encode_jpeg`, the raw
rectified with the camera's matrix and distortion), PUT through the presigned
URL the orchestrator issued for the tenant's bucket. The processor holds no
tenant credentials. A presigned PUT signs no bytes, so the result names the
size and MD5 written, and the orchestrator checks the stored object against
them before recording it.

The raw comes from scratch, staged by the orchestrator. A raw missing there,
or one that will not decode, is an abstention (`raw_unavailable`,
`decode_failed`), recorded rather than retried forever. A frame asking for
another render version is refused and retried (`RenderVersionMismatch`): a
worker at another version -- a lab worker deployed by hand -- never writes
its rendering under another version's key.

**The raw, copied in the same pass** (2026-10-20). A frame may also owe a copy
of its raw in the tenant's bucket (`RenderFrame.raw_put_url`). The raw is
downloaded once, copied first -- an undecodable raw is still the data -- and
rendered after, if the frame owes a render; one owing only its copy is not
rendered (``not_asked``), since its stored render must keep matching its
recorded MD5. The result names the copy's size and MD5 for the orchestrator
to check against the stored object and the capture's checksum.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_contracts.renders import (
    RENDER_VERSION,
    RenderCaptureInput,
    RenderResult,
)
from fishsense_services_processor.slate_detect.frame import DECODE_ERRORS

__all__ = ["RenderActivities", "http_put"]

#: S3 codes for "not there": the frame was never staged, or was evicted.
_MISSING = {"404", "NoSuchKey", "NotFound"}


def http_put(url: str, headers: dict[str, str], data: bytes) -> int:
    """PUT `data` to a presigned URL; the HTTP status."""
    request = urllib.request.Request(url, data=data, method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def _size(jpeg: bytes) -> tuple[int, int]:
    # pylint: disable-next=import-outside-toplevel
    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as image:
        return image.size


class RenderActivities:  # pylint: disable=too-few-public-methods
    """The render activity, given its seams: the object store (scratch), the
    rendering, and the HTTP PUT."""

    def __init__(
        self,
        *,
        store_factory: Callable[[], Any],
        render_jpeg: Callable[[bytes, list, list], bytes],
        put: Callable[[str, dict[str, str], bytes], int] = http_put,
    ) -> None:
        self._store_factory = store_factory
        self._store: Any = None
        self._render_jpeg = render_jpeg
        self._put = put

    def _object_store(self) -> Any:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    @activity.defn(name="render_capture")
    async def render_capture(self, payload: RenderCaptureInput) -> RenderResult:
        """Copy one capture's raw and render it, as its frame owes, each PUT
        where the orchestrator says."""
        frame = payload.frame
        if payload.render_version != RENDER_VERSION:
            raise ApplicationError(
                f"capture {frame.capture_id}: asked for render "
                f"{payload.render_version}; this worker renders {RENDER_VERSION}",
                type="RenderVersionMismatch",
            )
        base = {
            "capture_id": frame.capture_id,
            "render_version": RENDER_VERSION,
            "camera_calibration_id": payload.camera_calibration_id,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                raw_path = await self._object_store().download_raw(
                    frame.raw, Path(tmpdir)
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in _MISSING:
                    raise
                activity.logger.warning(
                    "capture=%s: raw %s not in scratch", frame.capture_id, frame.raw.uri
                )
                missing = "raw_unavailable" if frame.owes_raw else None
                return RenderResult(
                    status="raw_unavailable", raw_status=missing, **base
                )
            raw = await asyncio.to_thread(raw_path.read_bytes)
        copy = await self._copy_raw(frame, raw) if frame.owes_raw else {}
        if not frame.owes_render:
            return RenderResult(status="not_asked", **copy, **base)
        try:
            jpeg = await asyncio.to_thread(
                self._render_jpeg,
                raw,
                payload.camera_matrix,
                payload.distortion_coefficients,
            )
        except DECODE_ERRORS as exc:
            activity.logger.warning(
                "capture=%s: raw would not decode: %s", frame.capture_id, exc
            )
            return RenderResult(status="decode_failed", **copy, **base)
        status = await asyncio.to_thread(
            self._put, frame.put_url, {"Content-Type": "image/jpeg"}, jpeg
        )
        if not 200 <= status < 300:
            raise RuntimeError(
                f"capture {frame.capture_id}: PUT of its render answered {status}"
            )
        width, height = await asyncio.to_thread(_size, jpeg)
        return RenderResult(
            status="rendered",
            bytes=len(jpeg),
            md5=hashlib.md5(jpeg).hexdigest(),
            width=width,
            height=height,
            **copy,
            **base,
        )

    async def _copy_raw(self, frame, raw: bytes) -> dict:
        """PUT the raw where the orchestrator presigned; the copy's fields."""
        status = await asyncio.to_thread(
            self._put,
            frame.raw_put_url,
            {"Content-Type": "application/octet-stream"},
            raw,
        )
        if not 200 <= status < 300:
            raise RuntimeError(
                f"capture {frame.capture_id}: PUT of its raw answered {status}"
            )
        return {
            "raw_status": "copied",
            "raw_bytes": len(raw),
            "raw_md5": hashlib.md5(raw).hexdigest(),
        }

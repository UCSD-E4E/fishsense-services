"""The thumbnail activity: a stored render in, its thumbnail in the tenant's
bucket out.

New in v2 (2026-10-20; review of #151: /fish's cards were full renders). The
render is read through a presigned GET -- never the NAS: it is already in the
tenant's bucket -- scaled to the cards' width (`contracts.thumbnails`), and PUT
through a presigned PUT; the processor holds no tenant credentials. The
result names the size and MD5 written, for the orchestrator to check against
the stored object. A render not there (deleted since, or never stored) is an
abstention, recorded rather than tried every hour.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import urllib.error
import urllib.request
from collections.abc import Callable

from temporalio import activity

from fishsense_services_contracts.thumbnails import ThumbnailInput, ThumbnailResult
from fishsense_services_processor.renders.activities import http_put

__all__ = ["ThumbnailActivities", "http_get", "scale"]

#: JPEG quality: a card, not a measurement.
QUALITY = 82


def http_get(url: str) -> bytes | None:
    """GET a presigned URL; None when nothing is there (404). Anything else --
    a 403 from a signature, a clock or a key's permission -- raises, so the
    try is retried rather than the render recorded missing for good (review
    of the stack)."""
    try:
        with urllib.request.urlopen(url, timeout=300) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def scale(jpeg: bytes, width: int) -> tuple[bytes, int, int]:
    """The JPEG `width` wide, its aspect kept: (bytes, width, height)."""
    # pylint: disable-next=import-outside-toplevel
    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as image:
        # Decode at a reduced scale where the JPEG allows: far cheaper than
        # decoding 12 MP to throw most of it away.
        image.draft("RGB", (width, width))
        rgb = image.convert("RGB")
        w, h = rgb.size
        height = max(1, round(h * width / w))
        small = rgb.resize((width, height), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    small.save(out, "JPEG", quality=QUALITY, optimize=True)
    return out.getvalue(), width, height


class ThumbnailActivities:  # pylint: disable=too-few-public-methods
    """The thumbnail activity, given its seams: the HTTP GET and PUT."""

    def __init__(
        self,
        *,
        get: Callable[[str], bytes | None] = http_get,
        put: Callable[[str, dict[str, str], bytes], int] = http_put,
    ) -> None:
        self._get = get
        self._put = put

    @activity.defn(name="make_thumbnail")
    async def make_thumbnail(self, payload: ThumbnailInput) -> ThumbnailResult:
        """Read one render, scale it, PUT it where the orchestrator says."""
        frame = payload.frame
        render = await asyncio.to_thread(self._get, frame.get_url)
        if render is None:
            activity.logger.warning("render=%s: not there to read", frame.render_id)
            return ThumbnailResult(
                render_id=frame.render_id, status="render_unavailable"
            )
        thumb, width, height = await asyncio.to_thread(scale, render, payload.width)
        status = await asyncio.to_thread(
            self._put, frame.put_url, {"Content-Type": "image/jpeg"}, thumb
        )
        if not 200 <= status < 300:
            raise RuntimeError(
                f"render {frame.render_id}: PUT of its thumbnail answered {status}"
            )
        return ThumbnailResult(
            render_id=frame.render_id,
            status="made",
            bytes=len(thumb),
            md5=hashlib.md5(thumb).hexdigest(),
            width=width,
            height=height,
        )

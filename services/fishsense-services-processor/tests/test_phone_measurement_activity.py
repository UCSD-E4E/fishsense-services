"""The phone measurement's activity (contract 28): one phone capture's stored
JPEG and depth, fetched by presigned GET, measured by the app's sequence, its
mask PUT. A file that is gone, or won't read, is said, not raised."""

from __future__ import annotations

import io
import urllib.error
import uuid

import cv2
import numpy as np
import pytest

from fishsense_services_contracts.masks import MaskUpload, mask_key
from fishsense_services_contracts.phone_measurement import PHONE_ALGORITHM, PhoneFrame
from fishsense_services_processor.phone_measurement.activities import (
    PhoneMeasurementActivities,
)

W, H = 1920, 1440
K = [1604.2147, 0.0, 956.5816, 0.0, 1604.2147, 717.7617, 0.0, 0.0, 1.0]
CAPTURE = uuid.uuid4()


def _jpeg() -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((H, W, 3), 60, np.uint8))
    assert ok
    return buf.tobytes()


def _depth(z: float = 0.5) -> bytes:
    return np.full((192, 256), z, np.float32).tobytes()


def _mask() -> np.ndarray:
    mask = np.zeros((H, W), np.uint8)
    cv2.ellipse(mask, (960, 720), (400, 120), 0, 0, 360, 255, -1)
    return mask


class _Segmenter:
    def __init__(self, mask):
        self.mask = mask

    def inference_single(self, img):
        return self.mask

    def model_id(self):
        return "fishial/test@000000000000"


def _frame(with_mask: bool = True) -> PhoneFrame:
    upload = MaskUpload(
        key=mask_key("phone", CAPTURE, uuid.uuid4()), put_url="https://s3/put"
    )
    return PhoneFrame(capture_id=CAPTURE, rgb_url="https://s3/rgb", depth_url="https://s3/depth",
                      depth_width=256, depth_height=192, depth_row_bytes=1024,
                      intrinsics=K, mask=upload if with_mask else None)  # fmt: skip


def _activities(files, mask="fish", puts=None):
    def get(url):
        value = files[url]
        if isinstance(value, Exception):
            raise value
        return value

    def put(url, headers, data):
        puts.append((url, headers, data))
        return 200

    return PhoneMeasurementActivities(
        get=get,
        put=put,
        segmenter=lambda: _Segmenter(_mask() if mask == "fish" else mask),
    )


def _missing(url):
    return urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO())


async def test_a_capture_is_measured_and_its_mask_stored():
    puts = []
    acts = _activities(
        {"https://s3/rgb": _jpeg(), "https://s3/depth": _depth()}, puts=puts
    )
    got = await acts.measure_phone_capture(_frame())
    assert got.status == "measured"
    assert got.length_m == pytest.approx(800 * 0.5 / K[0], rel=0.01)
    assert got.snout_depth_m == pytest.approx(0.5, rel=1e-4)
    assert (got.model_id, got.algorithm) == (
        "fishial/test@000000000000",
        PHONE_ALGORITHM,
    )
    assert got.core_version
    [(url, headers, png)] = puts
    assert url == "https://s3/put" and headers["Content-Type"] == "image/png"
    assert got.mask.key.startswith(f"masks/phone/{CAPTURE}/")
    assert (got.mask.width, got.mask.height) == (W, H)
    decoded = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_UNCHANGED)
    assert decoded.shape == (H, W) and set(np.unique(decoded)) == {0, 255}


async def test_no_mask_upload_stores_nothing_and_still_measures():
    puts = []
    acts = _activities(
        {"https://s3/rgb": _jpeg(), "https://s3/depth": _depth()}, puts=puts
    )
    got = await acts.measure_phone_capture(_frame(with_mask=False))
    assert got.status == "measured" and got.mask is None and puts == []


async def test_no_fish_stores_no_mask():
    puts = []
    acts = _activities({"https://s3/rgb": _jpeg(), "https://s3/depth": _depth()},
                       mask=None, puts=puts)  # fmt: skip
    got = await acts.measure_phone_capture(_frame())
    assert got.status == "no_fish" and got.mask is None and puts == []


@pytest.mark.parametrize("gone", ["https://s3/rgb", "https://s3/depth"])
async def test_a_file_that_is_gone_is_image_unavailable(gone):
    files = {"https://s3/rgb": _jpeg(), "https://s3/depth": _depth()}
    files[gone] = _missing(gone)
    got = await _activities(files, puts=[]).measure_phone_capture(_frame())
    assert got.status == "image_unavailable" and got.length_m is None


async def test_a_store_error_is_raised_to_be_retried():
    files = {"https://s3/rgb": urllib.error.HTTPError("u", 500, "x", {}, io.BytesIO()),
             "https://s3/depth": _depth()}  # fmt: skip
    with pytest.raises(urllib.error.HTTPError):
        await _activities(files, puts=[]).measure_phone_capture(_frame())


async def test_an_unreadable_jpeg_is_decode_failed():
    files = {"https://s3/rgb": b"not a jpeg", "https://s3/depth": _depth()}
    got = await _activities(files, puts=[]).measure_phone_capture(_frame())
    assert got.status == "decode_failed"


async def test_depth_of_the_wrong_size_is_decode_failed():
    files = {"https://s3/rgb": _jpeg(), "https://s3/depth": b"\x00" * 100}
    got = await _activities(files, puts=[]).measure_phone_capture(_frame())
    assert got.status == "decode_failed" and "depth" in got.error

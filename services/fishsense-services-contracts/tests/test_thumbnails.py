"""Each render's thumbnail: what /fish's cards show (review of #151: a full
4014x3016 render as a card is ~6-8 MB, ~285 MB for a page of 50)."""

import uuid

import pytest
from pydantic import ValidationError

from fishsense_services_contracts.renders import RENDER_VERSION, render_key
from fishsense_services_contracts.thumbnails import (
    THUMBNAIL_WIDTH,
    ThumbnailFrame,
    ThumbnailResult,
    thumbnail_key,
)

CAMERA = uuid.UUID("0123456789abcdef0123456789abcdef")
SUM = "c" * 32
MD5 = "0123456789abcdef0123456789abcdef"


def test_a_thumbnail_sits_beside_its_render_under_its_own_prefix():
    render = render_key(RENDER_VERSION, CAMERA, SUM)
    assert thumbnail_key(render) == f"thumbnails/w{THUMBNAIL_WIDTH}/" + render
    with pytest.raises(ValueError):
        thumbnail_key("raws/x.ORF")


def test_a_frame_carries_its_render_to_read_and_where_to_put_the_thumbnail():
    frame = ThumbnailFrame(render_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                           get_url="https://s/g", key="thumbnails/w640/renders/x.JPG",
                           put_url="https://s/p")  # fmt: skip
    assert frame.key.startswith("thumbnails/")


def test_a_made_thumbnail_names_its_bytes_and_size_and_a_miss_none():
    made = ThumbnailResult(render_id=uuid.uuid4(), status="made", bytes=40_000,
                           md5=MD5, width=640, height=481)  # fmt: skip
    assert made.width == THUMBNAIL_WIDTH
    with pytest.raises(ValidationError):
        ThumbnailResult(render_id=uuid.uuid4(), status="made")
    with pytest.raises(ValidationError):
        ThumbnailResult(render_id=uuid.uuid4(), status="render_unavailable",
                        bytes=1, md5=MD5)  # fmt: skip
    with pytest.raises(ValidationError):
        ThumbnailResult(render_id=uuid.uuid4(), status="gone")

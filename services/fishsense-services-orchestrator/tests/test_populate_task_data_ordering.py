"""Imported LS tasks carry the fields needed to sort a project by capture time.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/tests/
test_populate_task_data_ordering.py. Names, bodies and reasons are v1's. v2
adaptation: the image is a `TaskImage` (its capture's `number`, v1's image id
for a migrated capture, and `captured_at`), and the storage is passed in.

Label Studio fixes task order at import: `id` and `inner_id` are assigned then
and are immutable, and re-importing to reorder would mean deleting tasks and
losing their annotations. So a project that imported in the wrong order can
only be rescued by sorting the Data Manager on some column -- and until v1
added these the only task data was the image URL, which is named by MD5
checksum and therefore sorts randomly.

`taken` is written as an ISO-8601 string deliberately. Label Studio sorts data
columns as text, so a numeric index sorts "0, 1, 10, 100" -- verified against
prod project 287542 before choosing this. ISO-8601 is the format whose text
order equals its chronological order.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fishsense_services_orchestrator.labels.populate import (
    LabelStudioStorageSettings,
    TaskImage,
    build_task_data,
)

STORAGE = LabelStudioStorageSettings(
    bucket="labels-fishsense-lite",
    prefix="fishsense-lite",
    endpoint_url="http://garage.example.com",
    region="garage",
    access_key="ak",
    secret_key="sk",
)


@pytest.fixture(name="build")
def _build():
    return lambda folder, image: build_task_data(folder, image, STORAGE)


def _image(number=7, taken=datetime(2023, 8, 31, 19, 42, 29, tzinfo=timezone.utc)):
    return TaskImage(number=number, checksum=f"{number:032d}", captured_at=taken)


def test_keeps_both_image_keys(build):
    """`image` and `img` both ship -- prod labeling configs use either."""
    data = build("preprocess_groups_jpeg", _image())

    assert data["image"] == data["img"]
    assert data["image"].startswith("s3://")
    assert "preprocess_groups_jpeg" in data["image"]


def test_carries_capture_time_as_iso_text(build):
    """Sortable as text, because that is how the Data Manager sorts."""
    data = build("preprocess_groups_jpeg", _image())

    assert data["taken"] == "2023-08-31T19:42:29+00:00"


def test_iso_text_order_matches_chronological_order(build):
    """The property the whole field exists for."""
    early = build(
        "f", _image(1, datetime(2023, 8, 31, 19, 42, 29, tzinfo=timezone.utc))
    )
    later = build(
        "f", _image(2, datetime(2023, 8, 31, 19, 44, 47, tzinfo=timezone.utc))
    )
    next_day = build("f", _image(3, datetime(2023, 9, 1, 1, 0, 0, tzinfo=timezone.utc)))

    assert early["taken"] < later["taken"] < next_day["taken"]


def test_carries_image_id(build):
    """Ties within a second are common -- EXIF resolution is one second and
    these cameras fire ~4 frames a second -- so the id is the tiebreak. v2
    keeps v1's key: it is the capture's number, v1's image id when migrated,
    so a migrated project's column keeps meaning what it did."""
    data = build("preprocess_groups_jpeg", _image(number=4321))

    assert data["image_id"] == 4321


def test_missing_capture_time_is_null_not_absent(build):
    """An image with no EXIF timestamp must still produce a valid task; the
    key stays present so the Data Manager still renders the column."""
    data = build("preprocess_groups_jpeg", _image(taken=None))

    assert data["taken"] is None
    assert data["image"].startswith("s3://")


def test_the_url_is_v1s_for_a_migrated_capture(build):
    """A migrated project's tasks are deduplicated by this URL: if v2 built a
    different one for the same JPEG, its next populate would import every
    task again."""
    data = build("preprocess_jpeg", TaskImage(12, "ab" * 16, None))

    assert data["image"] == (
        "s3://labels-fishsense-lite/fishsense-lite/preprocess_jpeg/"
        + "ab" * 16
        + ".JPG"
    )

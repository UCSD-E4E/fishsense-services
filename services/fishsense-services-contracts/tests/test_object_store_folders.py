"""The automatic render's folder names the render: its version and the camera
calibration the raw was rectified with (2026-10-19). Either changing changes
the pixels, so either changing must change the key -- a re-render never
overwrites the image older rows' pixels belong to."""

import uuid

import pytest

from fishsense_services_contracts.object_store import (
    automatic_jpeg_folder,
    is_jpeg_folder,
)

CAMERA = uuid.UUID("0123456789abcdef0123456789abcdef")


def test_the_folder_names_the_render_version_and_the_camera():
    assert (
        automatic_jpeg_folder(1, CAMERA)
        == "automatic_jpeg_r1_0123456789abcdef0123456789abcdef"
    )
    assert is_jpeg_folder(automatic_jpeg_folder(27, uuid.uuid4()))


def test_another_camera_is_another_folder():
    assert automatic_jpeg_folder(1, CAMERA) != automatic_jpeg_folder(1, uuid.uuid4())


@pytest.mark.parametrize("version", [0, -1, True, 1.0, "1"])
def test_a_render_version_is_a_positive_int(version):
    with pytest.raises(ValueError):
        automatic_jpeg_folder(version, CAMERA)


def test_the_camera_is_a_uuid():
    with pytest.raises(ValueError):
        automatic_jpeg_folder(1, str(CAMERA))


@pytest.mark.parametrize(
    "folder",
    [
        "automatic_jpeg",
        "automatic_jpeg_r1",
        "automatic_jpeg_r0_0123456789abcdef0123456789abcdef",
        "automatic_jpeg_r01_0123456789abcdef0123456789abcdef",
        "automatic_jpeg_r1_0123456789ABCDEF0123456789ABCDEF",
        "automatic_jpeg_r1_0123456789abcdef",
        "automatic_jpeg_r1_0123456789abcdef0123456789abcdefx",
    ],
)
def test_no_other_automatic_folder(folder):
    assert not is_jpeg_folder(folder)

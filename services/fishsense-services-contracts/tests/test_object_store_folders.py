"""The automatic render's folder carries its render version (2026-10-19)."""

import pytest

from fishsense_services_contracts.object_store import (
    automatic_jpeg_folder,
    is_jpeg_folder,
)


def test_the_folder_names_the_render_version():
    assert automatic_jpeg_folder(1) == "automatic_jpeg_r1"
    assert is_jpeg_folder(automatic_jpeg_folder(27))


@pytest.mark.parametrize("version", [0, -1, True, 1.0, "1"])
def test_a_render_version_is_a_positive_int(version):
    with pytest.raises(ValueError):
        automatic_jpeg_folder(version)


@pytest.mark.parametrize(
    "folder",
    ["automatic_jpeg", "automatic_jpeg_r0", "automatic_jpeg_r01", "automatic_jpeg_rx"],
)
def test_no_unversioned_or_malformed_automatic_folder(folder):
    assert not is_jpeg_folder(folder)

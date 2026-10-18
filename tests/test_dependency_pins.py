"""Pins the lock must keep, checked where CI can see them.

sam3 (SAM 3.1, the GPU image's head/tail and automatic-results masks) does
``import pkg_resources``, which setuptools removed in 82.0.0. A Dependabot
bump (e268081, 2026-10-01) widened the pin to ``<84`` and locked 83.0.0 past
its own comment; the GPU image could not import SAM 3.1 from then on, and
nothing noticed until the first automatic-results validation run on
2026-10-18: every frame failed with ``No module named 'pkg_resources'``. CI
never installs the GPU extra, so the lock is checked here instead.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

LOCK = Path(__file__).resolve().parents[1] / "uv.lock"


def _locked() -> dict[str, list[str]]:
    versions: dict[str, list[str]] = {}
    for package in tomllib.loads(LOCK.read_text())["package"]:
        versions.setdefault(package["name"], []).append(package["version"])
    return versions


def test_setuptools_still_ships_pkg_resources_while_sam3_is_locked():
    locked = _locked()
    assert "sam3" in locked, "sam3 left the lock: drop this pin and this test"
    for version in locked["setuptools"]:
        major = int(version.split(".")[0])
        assert (
            major < 82
        ), f"setuptools {version} has no pkg_resources, which sam3 imports"


def test_the_gpu_image_imports_sam3_when_it_is_built():
    """The real path, at build time: the import that failed in production
    on 2026-10-18 runs in the GPU image's own stage, so a missing module
    fails the image build (CI builds it) instead of every frame."""
    dockerfile = (LOCK.parent / "Dockerfile").read_text()
    stage = dockerfile.split("FROM runtime AS processor-gpu", 1)[1].split("\nFROM ", 1)[
        0
    ]
    assert "import pkg_resources" in stage
    assert "import sam3.model.model_builder" in stage

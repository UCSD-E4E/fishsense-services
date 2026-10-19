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


#: fishsense-core 4.1.0's own SAM 3.1 imports (fishsense_core/fish/sam3.py:68-69),
#: the real path the GPU image must be able to take.
SAM3_IMPORTS = (
    "from sam3.model.sam3_image_processor import Sam3Processor",
    "from sam3.model_builder import build_sam3_image_model",
)


def test_setuptools_still_ships_pkg_resources_while_sam3_is_locked():
    locked = _locked()
    assert "sam3" in locked, "sam3 left the lock: drop this pin and this test"
    assert "setuptools" in locked, (
        "setuptools left the lock while sam3 is in it: sam3 imports "
        "pkg_resources, which only setuptools provides"
    )
    for version in locked["setuptools"]:
        major = int(version.split(".")[0])
        assert (
            major < 82
        ), f"setuptools {version} has no pkg_resources, which sam3 imports"


def test_the_gpu_image_imports_sam3_when_it_is_built():
    """The real path, at build time: fishsense-core's own SAM 3.1 imports
    run in the GPU image's stage, as its runtime user, so a missing module
    fails the image build (CI builds it) instead of every frame (2026-10-18).
    A misspelt module path fails that build too: the review of #103 caught
    `sam3.model.model_builder`, which does not exist."""
    dockerfile = (LOCK.parent / "Dockerfile").read_text()
    stage = dockerfile.split("FROM runtime AS processor-gpu", 1)[1].split("\nFROM ", 1)[
        0
    ]
    check = stage[stage.index("USER app") :]
    assert "import pkg_resources" in check
    for line in SAM3_IMPORTS:
        assert line in check, line
    # ...and Triton's kernel cache is writable for that user (review of #110).
    assert "expanduser('~/.triton" in check


def test_the_images_user_has_a_home():
    """Triton writes its kernel cache under $HOME (no XDG): the runtime
    stage's `app` user (uid 10001, the pods' runAsUser) must have a real,
    writable home, in the image itself -- for every pod, compose and a
    `docker run` alike (review of #110)."""
    dockerfile = (LOCK.parent / "Dockerfile").read_text()
    runtime = dockerfile.split("FROM python:3.13-slim AS runtime", 1)[1].split(
        "\nFROM ", 1
    )[0]
    useradd = next(line for line in runtime.splitlines() if "useradd" in line)
    assert "--uid 10001" in useradd
    assert "--create-home" in useradd and "--no-create-home" not in useradd


def test_the_gpu_image_can_compile_triton_kernels():
    """Triton builds its kernel launchers with a C compiler at run time. SAM
    3.1's NMS, connected components and distance transform are Triton kernels
    on CUDA (sam3/perflib/triton/, sam3/model/edt.py), so every frame with
    more than one detection failed `RuntimeError: Failed to find C compiler`
    on the slim image (2026-10-19, dive 87). The GPU image installs one, and
    its build check fails if it is missing."""
    dockerfile = (LOCK.parent / "Dockerfile").read_text()
    stage = dockerfile.split("FROM runtime AS processor-gpu", 1)[1].split("\nFROM ", 1)[
        0
    ]
    before_user = stage[: stage.index("USER app")]
    assert "apt-get install" in before_user
    assert " gcc" in before_user and "libc6-dev" in before_user
    check = stage[stage.index("USER app") :]
    assert "shutil.which('gcc')" in check

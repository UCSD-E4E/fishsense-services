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


#: The workspace override that lifts label-studio-sdk's OpenCV cap (root
#: pyproject `[tool.uv] override-dependencies`), for fishsense-core 4.3.0.
OPENCV = "opencv-python-headless"
OPENCV_FLOOR, OPENCV_CEILING_MAJOR = "5.0.0.93", 6


def _override(name: str) -> str:
    pyproject = tomllib.loads((LOCK.parent / "pyproject.toml").read_text())
    (entry,) = [
        d
        for d in pyproject["tool"]["uv"]["override-dependencies"]
        if d.replace(" ", "").startswith(name)
    ]
    return entry


def _requirements(dist: str, name: str):
    """`dist`'s requirements on exactly `name` (a distribution name, so the
    plain `opencv-python` never stands in for the headless one)."""
    from importlib.metadata import requires

    from packaging.requirements import Requirement

    reqs = [Requirement(r) for r in requires(dist) or []]
    return [r for r in reqs if r.name.lower() == name]


def test_the_opencv_override_is_bounded_above():
    """An override replaces *every* constraint on the package, core's own
    included: an open range would also let a lock refresh take OpenCV 6 for
    both the processor and the orchestrator, and hide a later core's own
    bound (review of #348). The lock's majors are read off the same bound."""
    entry = _override(OPENCV).replace(" ", "")
    assert f">={OPENCV_FLOOR}" in entry, entry
    assert f"<{OPENCV_CEILING_MAJOR}" in entry, entry
    for version in _locked()[OPENCV]:
        assert int(version.split(".")[0]) < OPENCV_CEILING_MAJOR, version


def test_only_the_headless_opencv_is_locked():
    """Two OpenCV distributions both install `cv2`, and the one installed last
    wins file by file (review of #348)."""
    locked = _locked()
    clashing = {
        "opencv-python",
        "opencv-contrib-python",
        "opencv-contrib-python-headless",
    }
    assert not clashing & set(locked), sorted(clashing & set(locked))


def _sdk_imports(root: Path) -> set[str]:
    """Every label_studio_sdk module a source tree names: imports, and the
    module strings handed to importlib / __import__."""
    import re

    found = set()
    for path in root.rglob("*.py"):
        text = path.read_text()
        found |= set(
            re.findall(r"^\s*from (label_studio_sdk[\w.]*) import", text, re.M)
        )
        found |= set(re.findall(r"^\s*import (label_studio_sdk[\w.]*)", text, re.M))
        found |= set(
            re.findall(
                r"""(?:import_module|__import__)\(\s*["'](label_studio_sdk[\w.]*)""",
                text,
            )
        )
    return found


def test_the_opencv_override_is_still_needed_and_still_safe():
    """Why the override exists, each reason checked so it can't outlive them:
    fishsense-core needs OpenCV 5, label-studio-sdk still caps the headless
    OpenCV below 5, and every service reaches only the SDK's API client (not
    its cv2 converters), which imports on the OpenCV 5 the lock holds."""
    import importlib

    from packaging.version import Version

    def capped_below_5(dist: str) -> bool:
        return any(
            not r.specifier.contains(Version("5.0.0.93"))
            for r in _requirements(dist, OPENCV)
        )

    assert not capped_below_5("fishsense-core") and _requirements(
        "fishsense-core", OPENCV
    ), "fishsense-core no longer needs the headless OpenCV 5: drop the override"
    assert capped_below_5(
        "label-studio-sdk"
    ), "label-studio-sdk no longer caps the headless OpenCV below 5: drop the override"

    imported = set()
    for src in sorted((LOCK.parent / "services").glob("*/src")):
        imported |= _sdk_imports(src)
    assert imported <= {
        "label_studio_sdk.client",
        "label_studio_sdk.core",
    }, f"a service now reaches {sorted(imported)}: check them on OpenCV 5"
    for module in sorted(imported):
        importlib.import_module(module)


def _cv2_names(paths) -> set[str]:
    import re

    names = set()
    for path in paths:
        names |= set(re.findall(r"\bcv2\.([A-Za-z_]\w*)", path.read_text()))
    return names


def test_every_cv2_name_our_code_and_cores_use_exists():
    """OpenCV 5 moved and dropped names (review of #348): an import check
    cannot see that, a call does, on a production frame. Every `cv2.<name>`
    in the processor and in fishsense-core must exist in the locked OpenCV.
    The GPU image checks SAM 3.1 and core's GPU extras the same way."""
    import cv2

    import fishsense_core

    sources = list(
        (LOCK.parent / "services/fishsense-services-processor/src").rglob("*.py")
    )
    sources += list(Path(fishsense_core.__file__).parent.rglob("*.py"))
    missing = sorted(n for n in _cv2_names(sources) if not hasattr(cv2, n))
    assert not missing, f"cv2 {cv2.__version__} has no {missing}"

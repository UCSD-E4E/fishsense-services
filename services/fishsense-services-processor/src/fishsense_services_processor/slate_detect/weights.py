"""The slate detector's weights: fetched through fishsense-core, pinned from
settings.

New in v2, built as the head/tail stage's SAM 3.1 weights are
(`headtail_predict.weights`), for the same reasons: PLAN.md §9.12 puts model
weights in Garage's `model-weights` behind core's `WeightStore`
(`weights.GarageWeightStore`), and a manifest's sha256 decides what loads.

The checkpoint is 2026-10-03_slate_detector@95a77d95's
`runs/final-q1/slate_efficientnet_b0.pt` (models live in their dated repos;
v2 pins the weights it runs). **fishsense-core 4.1.0's manifest has no entry
for it**, so this stage builds a one-entry manifest from **required**
settings, measured once from the uploaded copy:

    FISHSENSE_SLATE_DETECTOR_SHA256   sha256 of slate_efficientnet_b0.pt
    FISHSENSE_SLATE_DETECTOR_SIZE     its size in bytes

at ``model-weights/slate-detector/q1/slate_efficientnet_b0.pt``, so a
checkpoint that isn't that file is refused rather than loaded. Other weights
are a new version segment and a `SLATE_DETECTOR_VERSION` bump.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fishsense_core.models import Manifest
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fishsense_services_processor.weights import GarageWeightStore, fetch_weights

__all__ = [
    "SLATE_DETECTOR_FILENAME",
    "SLATE_DETECTOR_MODEL",
    "SLATE_DETECTOR_WEIGHTS_VERSION",
    "SlateDetectorSettings",
    "fetch_slate_detector",
    "slate_detector_manifest",
]

SLATE_DETECTOR_MODEL = "slate-detector"
#: The source repo's run: `runs/final-q1`.
SLATE_DETECTOR_WEIGHTS_VERSION = "q1"
SLATE_DETECTOR_FILENAME = "slate_efficientnet_b0.pt"


class SlateDetectorSettings(BaseSettings):
    """The pinned checkpoint, from ``FISHSENSE_SLATE_DETECTOR_*``. Required:
    without them the stage cannot verify, and so cannot load, its weights."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_SLATE_DETECTOR_")

    sha256: str
    size: int

    @field_validator("sha256")
    @classmethod
    def _a_sha256(cls, value: str) -> str:
        value = value.strip().lower()
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("must be a sha256 hex digest")
        return value

    @field_validator("size")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be the checkpoint's size in bytes")
        return value


def slate_detector_manifest(settings: SlateDetectorSettings) -> Manifest:
    """A manifest with the one entry the settings pin."""
    return Manifest.parse(f"""
[[model]]
name = "{SLATE_DETECTOR_MODEL}"
version = "{SLATE_DETECTOR_WEIGHTS_VERSION}"
default = true

[[model.artifact]]
targets = ["server"]
filename = "{SLATE_DETECTOR_FILENAME}"
sha256 = "{settings.sha256}"
size = {settings.size}
""")


async def fetch_slate_detector(
    *, store: GarageWeightStore, cache_dir: Path, settings: SlateDetectorSettings
) -> tuple[Path, str]:
    """The verified checkpoint's local path, and its sha256 (what a
    prediction records). The download and hash run off the event loop."""
    manifest = slate_detector_manifest(settings)
    ref = manifest.resolve(SLATE_DETECTOR_MODEL, SLATE_DETECTOR_WEIGHTS_VERSION)
    path = await asyncio.to_thread(
        fetch_weights,
        SLATE_DETECTOR_MODEL,
        SLATE_DETECTOR_WEIGHTS_VERSION,
        store=store,
        cache_dir=cache_dir,
        manifest=manifest,
    )
    return path, ref.sha256

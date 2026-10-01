"""SAM 3.1's weights: fetched through fishsense-core, pinned from settings.

Replaces fishsense-lite@77e8f8e5's data-worker checkpoint_cache.py
(`ensure_checkpoint`) and its `[sam3]` settings (model_name `sam3`,
model_version `3.1`, checkpoint_filename `sam3.1_multiplex.pt`; the key
`model-weights/sam3/3.1/sam3.1_multiplex.pt`). PLAN.md §9.12: weights come from
Garage's `model-weights` through core's `WeightStore` (`weights.GarageWeightStore`),
and core's manifest's sha256 decides what loads.

**fishsense-core 4.1.0's manifest has no SAM 3.1 entry** (facebook/sam3.1 is
gated and publishes no hash). Until a core release pins it, this stage builds a
one-entry manifest from **required** settings -- the sha256 and size of the
`model-weights` copy, measured once:

    FISHSENSE_SAM3_SHA256   sha256 of sam3.1_multiplex.pt
    FISHSENSE_SAM3_SIZE     its size in bytes

so a checkpoint that isn't that file is refused rather than loaded (upstream's
loader is `strict=False`, so a wrong checkpoint would load silently and degrade
every mask). When core pins SAM 3.1, drop this module's manifest and fetch with
core's built-in one.

The checkpoint is the one v1 pinned by name in `HEADTAIL_PREDICTOR_VERSION` 2;
changing it is a version bump.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fishsense_core.models import Manifest
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fishsense_services_processor.weights import GarageWeightStore, fetch_weights

__all__ = [
    "SAM3_FILENAME",
    "SAM3_MODEL",
    "SAM3_VERSION",
    "Sam3Settings",
    "fetch_sam3",
    "sam3_manifest",
]

SAM3_MODEL = "sam3"
SAM3_VERSION = "3.1"
SAM3_FILENAME = "sam3.1_multiplex.pt"


class Sam3Settings(BaseSettings):
    """The pinned SAM 3.1 checkpoint, from ``FISHSENSE_SAM3_*``. Required:
    without them the GPU tier cannot verify, and so cannot load, its weights."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_SAM3_")

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


def sam3_manifest(settings: Sam3Settings) -> Manifest:
    """A manifest with the one SAM 3.1 entry the settings pin."""
    return Manifest.parse(f"""
[[model]]
name = "{SAM3_MODEL}"
version = "{SAM3_VERSION}"
default = true

[[model.artifact]]
targets = ["server"]
filename = "{SAM3_FILENAME}"
sha256 = "{settings.sha256}"
size = {settings.size}
""")


async def fetch_sam3(
    *, store: GarageWeightStore, cache_dir: Path, settings: Sam3Settings
) -> tuple[Path, str]:
    """The verified checkpoint's local path, and its model id
    (``sam3/3.1@<sha256[:12]>``: what a prediction records as its checkpoint).
    The download and hash run off the event loop."""
    manifest = sam3_manifest(settings)
    ref = manifest.resolve(SAM3_MODEL, SAM3_VERSION)
    path = await asyncio.to_thread(
        fetch_weights,
        SAM3_MODEL,
        SAM3_VERSION,
        store=store,
        cache_dir=cache_dir,
        manifest=manifest,
    )
    return path, ref.id

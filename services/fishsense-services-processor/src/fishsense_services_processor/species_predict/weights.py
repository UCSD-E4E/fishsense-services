"""BioCLIP's weights: fetched through fishsense-core, pinned from settings.

New in v2 (no v1 counterpart), built as the head/tail stage's SAM 3.1 weights
are (`headtail_predict.weights`), for the same reasons: PLAN.md §9.12 puts
model weights in Garage's `model-weights` behind core's `WeightStore`
(`weights.GarageWeightStore`), and a manifest's sha256 decides what loads.

**fishsense-core 4.1.0's manifest has no BioCLIP entry.** Until a core release
pins one, this stage builds a manifest from **required** settings -- the
sha256 and size of each `model-weights` copy, measured once:

    FISHSENSE_BIOCLIP_SHA256            BioCLIP 2.5's open_clip_model.safetensors
    FISHSENSE_BIOCLIP_SIZE              its size in bytes
    FISHSENSE_BIOCLIP_FALLBACK_SHA256   BioCLIP 2's
    FISHSENSE_BIOCLIP_FALLBACK_SIZE

at ``model-weights/bioclip/{2.5-vith14,2}/open_clip_model.safetensors``, so
weights that aren't those files are refused rather than loaded.

The two models are coral-gardeners-fish-detector@67c8627's
(bioclip_classifier.py): `hf-hub:imageomics/bioclip-2.5-vith14` and
`hf-hub:imageomics/bioclip-2`. Their architectures are pinned here as each hub
repo's `open_clip_config.json`, verbatim, at the revision the weights were
measured from, and written beside the verified weights, where open_clip's
`local-dir:` reads both. Nothing is fetched from the hub. Changing either
file is a `SPECIES_PREDICTOR_VERSION` bump.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from fishsense_core.models import Manifest
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fishsense_services_contracts.species_prediction import (
    SPECIES_FALLBACK_MODEL_ID,
    SPECIES_PRIMARY_MODEL_ID,
)
from fishsense_services_processor.species_predict.classifier import validate_model_id
from fishsense_services_processor.weights import GarageWeightStore, fetch_weights

__all__ = [
    "BIOCLIP_FILENAME",
    "BIOCLIP_MODEL",
    "BIOCLIP_VERSIONS",
    "OPEN_CLIP_CONFIG_FILENAME",
    "OPEN_CLIP_CONFIGS",
    "BioclipSettings",
    "bioclip_manifest",
    "fetch_bioclip",
]

BIOCLIP_MODEL = "bioclip"
BIOCLIP_FILENAME = "open_clip_model.safetensors"
OPEN_CLIP_CONFIG_FILENAME = "open_clip_config.json"

#: Each allowed model id's manifest version (its `model-weights` key segment).
BIOCLIP_VERSIONS = {
    SPECIES_PRIMARY_MODEL_ID: "2.5-vith14",
    SPECIES_FALLBACK_MODEL_ID: "2",
}

_OPENAI_PREPROCESS: dict[str, Any] = {
    "mean": [0.48145466, 0.4578275, 0.40821073],
    "std": [0.26862954, 0.26130258, 0.27577711],
    "interpolation": "bicubic",
    "resize_mode": "shortest",
}

#: Each repo's `open_clip_config.json`: imageomics/bioclip-2.5-vith14 at
#: 191d741545e4c741cdef4b22c6eb69c945c1e592 (ViT-H/14) and imageomics/bioclip-2
#: at 2957b322090f9cb17ae72c71981c7218a28d81e0 (ViT-L/14).
OPEN_CLIP_CONFIGS: dict[str, dict[str, Any]] = {
    "2.5-vith14": {
        "model_cfg": {
            "embed_dim": 1024,
            "vision_cfg": {
                "image_size": 224,
                "layers": 32,
                "width": 1280,
                "head_width": 80,
                "patch_size": 14,
            },
            "text_cfg": {
                "context_length": 77,
                "vocab_size": 49408,
                "width": 1024,
                "heads": 16,
                "layers": 24,
            },
        },
        "preprocess_cfg": _OPENAI_PREPROCESS,
    },
    "2": {
        "model_cfg": {
            "embed_dim": 768,
            "vision_cfg": {
                "image_size": 224,
                "layers": 24,
                "width": 1024,
                "patch_size": 14,
            },
            "text_cfg": {
                "context_length": 77,
                "vocab_size": 49408,
                "width": 768,
                "heads": 12,
                "layers": 12,
            },
        },
        "preprocess_cfg": _OPENAI_PREPROCESS,
    },
}


def _sha256(value: str) -> str:
    value = value.strip().lower()
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("must be a sha256 hex digest")
    return value


def _size(value: int) -> int:
    if value <= 0:
        raise ValueError("must be the weights' size in bytes")
    return value


class BioclipSettings(BaseSettings):
    """The pinned BioCLIP weights, from ``FISHSENSE_BIOCLIP_*``. Required:
    without them the stage cannot verify, and so cannot load, its models."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_BIOCLIP_")

    sha256: str
    size: int
    fallback_sha256: str
    fallback_size: int

    @field_validator("sha256", "fallback_sha256")
    @classmethod
    def _a_sha256(cls, value: str) -> str:
        return _sha256(value)

    @field_validator("size", "fallback_size")
    @classmethod
    def _positive(cls, value: int) -> int:
        return _size(value)


def bioclip_manifest(settings: BioclipSettings) -> Manifest:
    """A manifest with the two BioCLIP entries the settings pin; 2.5 is the
    default."""
    entries = (
        (BIOCLIP_VERSIONS[SPECIES_PRIMARY_MODEL_ID], settings.sha256, settings.size,
         "true"),
        (BIOCLIP_VERSIONS[SPECIES_FALLBACK_MODEL_ID], settings.fallback_sha256,
         settings.fallback_size, "false"),
    )  # fmt: skip
    return Manifest.parse("".join(f"""
[[model]]
name = "{BIOCLIP_MODEL}"
version = "{version}"
default = {default}

[[model.artifact]]
targets = ["server"]
filename = "{BIOCLIP_FILENAME}"
sha256 = "{sha256}"
size = {size}
""" for version, sha256, size, default in entries))


def _write_config(directory: Path, version: str) -> None:
    """The pinned config beside the weights, replaced atomically, so a
    concurrent reader never sees half of it."""
    path = directory / OPEN_CLIP_CONFIG_FILENAME
    text = json.dumps(OPEN_CLIP_CONFIGS[version], indent=2, sort_keys=True)
    if path.exists() and path.read_text() == text:
        return
    partial = directory / f".{OPEN_CLIP_CONFIG_FILENAME}.{os.getpid()}.partial"
    partial.write_text(text)
    os.replace(partial, path)


def _fetch(
    model_id: str,
    *,
    store: GarageWeightStore,
    cache_dir: Path,
    settings: BioclipSettings,
) -> Path:
    version = BIOCLIP_VERSIONS[validate_model_id(model_id)]
    path = fetch_weights(
        BIOCLIP_MODEL,
        version,
        store=store,
        cache_dir=cache_dir,
        manifest=bioclip_manifest(settings),
    )
    _write_config(path.parent, version)
    return path.parent


async def fetch_bioclip(
    model_id: str,
    *,
    store: GarageWeightStore,
    cache_dir: Path,
    settings: BioclipSettings,
) -> tuple[Path, str]:
    """The directory open_clip loads `model_id` from (verified weights and
    their pinned config), and the weights' id (``bioclip/2.5-vith14@<sha256
    [:12]>``: what a prediction records). The original BioCLIP, or any model
    but the two, is refused. The download and hash run off the event loop."""
    ref = bioclip_manifest(settings).resolve(
        BIOCLIP_MODEL, BIOCLIP_VERSIONS[validate_model_id(model_id)]
    )
    directory = await asyncio.to_thread(
        _fetch, model_id, store=store, cache_dir=cache_dir, settings=settings
    )
    return directory, ref.id

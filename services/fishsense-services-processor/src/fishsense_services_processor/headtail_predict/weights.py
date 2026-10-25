"""SAM 3.1's weights, fetched and verified through fishsense-core's manifest.

Replaces fishsense-lite@77e8f8e5's data-worker checkpoint_cache.py
(`ensure_checkpoint`) and its `[sam3]` settings (model_name `sam3`,
model_version `3.1`, checkpoint_filename `sam3.1_multiplex.pt`; the key
`model-weights/sam3/3.1/sam3.1_multiplex.pt`). PLAN.md §9.12: weights come from
Garage's `model-weights` through core's `WeightStore` (`weights.GarageWeightStore`),
and core's manifest's sha256 decides what loads, so a checkpoint that isn't
that file is refused rather than loaded (upstream's loader is `strict=False`,
so a wrong checkpoint would load silently and degrade every mask).

History: fishsense-core 4.1.0's manifest had no SAM 3.1 entry (facebook/sam3.1
is gated and publishes no hash), so until 2026-10-23 this module built a
one-entry manifest from required ``FISHSENSE_SAM3_SHA256`` /
``FISHSENSE_SAM3_SIZE`` settings, measured once from the `model-weights` copy
and kept in the processor's Secret. Core 4.2.0 pinned the same file (sha256
``0567debeec80...``, 3 502 755 717 bytes); the owner matched the Secret's
recorded pin against it, and the shim went. The recorded checkpoint id,
``sam3/3.1@0567debeec80``, is unchanged.

The checkpoint is the one v1 pinned by name in `HEADTAIL_PREDICTOR_VERSION` 2,
so the version is named here rather than taken as core's default: changing it
is a predictor version bump, not a core upgrade.
"""

from __future__ import annotations

from pathlib import Path

from fishsense_core.models import builtin_manifest

from fishsense_services_processor.weights import GarageWeightStore, afetch_weights

__all__ = ["SAM3_MODEL", "SAM3_VERSION", "fetch_sam3"]

SAM3_MODEL = "sam3"
SAM3_VERSION = "3.1"


async def fetch_sam3(*, store: GarageWeightStore, cache_dir: Path) -> tuple[Path, str]:
    """The verified checkpoint's local path, and its model id
    (``sam3/3.1@<sha256[:12]>``: what a prediction records as its checkpoint).
    The download and hash run off the event loop.

    Raises what core's `fetch` raises: `KeyError` (no such manifest entry),
    `ModelIntegrityError`, `ModelUnavailable`; the activity maps each to its
    own non-retryable failure.

    One manifest resolves both the id and the file (review of #358): the id
    recorded is always the entry that loaded."""
    manifest = builtin_manifest()
    ref = manifest.resolve(SAM3_MODEL, SAM3_VERSION)
    path = await afetch_weights(
        SAM3_MODEL, SAM3_VERSION, store=store, cache_dir=cache_dir, manifest=manifest
    )
    return path, ref.id

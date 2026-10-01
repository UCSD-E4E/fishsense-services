"""BioCLIP itself: opt-in, skipped by default.

New in v2 (no v1 counterpart). Every other species test stubs the encoder;
these load the real model, on the CPU unless there is a GPU, and download
nothing. They need torch and open_clip (the processor's `torch` extra) and a
directory holding one BioCLIP hub snapshot -- `open_clip_config.json` beside
`open_clip_model.safetensors`, e.g. a Hugging Face cache's
`models--imageomics--bioclip-2.5-vith14/snapshots/<revision>/`:

    FISHSENSE_BIOCLIP_REAL_MODEL_DIR=/path/to/snapshot \\
    FISHSENSE_BIOCLIP_REAL_MODEL_VERSION=2.5-vith14 \\
        pytest tests/test_species_predict_real_model.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from fishsense_services_contracts.species_prediction import SpeciesCandidate
from fishsense_services_processor.species_predict.classifier import (
    BioclipClassifier,
    OpenClipEncoder,
)
from fishsense_services_processor.species_predict.weights import OPEN_CLIP_CONFIGS

MODEL_DIR = os.environ.get("FISHSENSE_BIOCLIP_REAL_MODEL_DIR")
VERSION = os.environ.get("FISHSENSE_BIOCLIP_REAL_MODEL_VERSION", "2.5-vith14")

pytestmark = pytest.mark.skipif(
    not MODEL_DIR, reason="opt-in: set FISHSENSE_BIOCLIP_REAL_MODEL_DIR"
)

CANDIDATES = [
    SpeciesCandidate(choice=f"Fish, {name}", scientific_name=name)
    for name in ("Lachnolaimus maximus", "Lutjanus griseus", "Sparisoma viride")
]


def test_the_pinned_config_is_the_hub_snapshots():
    """The architecture written beside the verified weights is the one the
    hub publishes for them."""
    published = json.loads((Path(MODEL_DIR) / "open_clip_config.json").read_text())
    assert published == OPEN_CLIP_CONFIGS[VERSION]


def test_the_real_model_ranks_a_crop_over_the_closed_set():
    pytest.importorskip("torch")
    pytest.importorskip("open_clip")
    classifier = BioclipClassifier(
        encoder=OpenClipEncoder.load(Path(MODEL_DIR)),
        model_id=f"bioclip/{VERSION}@local",
        predictor_version=1,
    )
    crop = Image.fromarray(
        np.random.default_rng(0).integers(0, 255, (224, 300, 3), dtype=np.uint8)
    )

    ranking = classifier.classify(crop, CANDIDATES)

    assert {c for c, _ in ranking.top5} == {c.choice for c in CANDIDATES}
    assert sum(p for _, p in ranking.top5) == pytest.approx(1.0, abs=1e-4)
    assert 0.0 <= ranking.margin <= ranking.top1_probability <= 1.0

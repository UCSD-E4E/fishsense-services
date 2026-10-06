"""The slate detector's real checkpoint: opt-in, skipped by default.

New in v2. Every other slate-detect test stubs the classifier or uses a
random network; these load 2026-10-03_slate_detector@95a77d95's
runs/final-q1/slate_efficientnet_b0.pt and download nothing. They need the
processor's `torch` extra:

    FISHSENSE_SLATE_DETECTOR_REAL_WEIGHTS=../2026-10-03_slate_detector/runs/final-q1/slate_efficientnet_b0.pt \\
        pytest tests/test_slate_detect_real_weights.py

With ``FISHSENSE_SLATE_DETECTOR_REAL_SOURCE`` set to that repo's root as well,
the port is compared with the source's own `load_model` and `predict` on its
cached frames (`data/frames/*.jpg`), on the same device.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

WEIGHTS = os.environ.get("FISHSENSE_SLATE_DETECTOR_REAL_WEIGHTS")
SOURCE = os.environ.get("FISHSENSE_SLATE_DETECTOR_REAL_SOURCE")

pytestmark = pytest.mark.skipif(
    not WEIGHTS, reason="opt-in: set FISHSENSE_SLATE_DETECTOR_REAL_WEIGHTS"
)

#: What the owner uploads to model-weights/slate-detector/q1/ and pins in
#: FISHSENSE_SLATE_DETECTOR_SHA256 / _SIZE.
Q1_SHA256 = "b8d377ba22d155e7056a5e9ae747fdd0970c7c73dee981bbee17d95c8156cf78"
Q1_SIZE = 16_339_455


def test_the_file_is_final_q1():
    data = Path(WEIGHTS).read_bytes()
    assert (hashlib.sha256(data).hexdigest(), len(data)) == (Q1_SHA256, Q1_SIZE)


def test_the_port_loads_the_checkpoint_and_scores_a_frame():
    pytest.importorskip("torch")
    from fishsense_services_processor.slate_detect.model import SlateClassifier

    classifier = SlateClassifier.load(Path(WEIGHTS), Q1_SHA256)
    noise = np.random.default_rng(0).integers(0, 255, (1202, 1600, 3))

    assert 0.0 <= classifier.probability(Image.fromarray(noise.astype(np.uint8))) <= 1


@pytest.mark.skipif(
    not SOURCE, reason="opt-in: set FISHSENSE_SLATE_DETECTOR_REAL_SOURCE"
)
def test_the_port_agrees_with_the_source():
    torch = pytest.importorskip("torch")
    from fishsense_services_processor.slate_detect.model import SlateClassifier

    sys.path.insert(0, str(Path(SOURCE) / "src"))
    try:
        # pylint: disable=import-outside-toplevel,import-error
        from slate_detector.dataset import Frame
        from slate_detector.train import load_model, predict
    finally:
        sys.path.pop(0)

    cache = Path(SOURCE) / "data" / "frames"
    ids = sorted(int(p.stem) for p in cache.glob("*.jpg"))[:8]
    assert ids, f"no cached frames in {cache}"
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model, cfg = load_model(Path(WEIGHTS), device)
    cfg.workers = 0
    frames = [Frame(i, "", "", 0, 0, 0, "", 0) for i in ids]
    expected = predict(model, frames, cache, cfg, device)

    port = SlateClassifier.load(Path(WEIGHTS), Q1_SHA256, device=device)
    actual = [port.probability(Image.open(cache / f"{i}.jpg")) for i in ids]

    np.testing.assert_allclose(actual, expected, atol=2e-3)

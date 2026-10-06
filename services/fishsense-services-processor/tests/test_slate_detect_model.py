"""The slate classifier's torch side: the port of the source's train.py.

New in v2. Ported from 2026-10-03_slate_detector@95a77d95
src/slate_detector/train.py (`GeM`, `build_model`, `transforms`, `load_model`,
`predict`). These need the processor's `torch` extra and skip without it; they
use a randomly initialised network, so nothing is downloaded. The real
checkpoint is test_slate_detect_real_weights.py (opt-in). Pinned here:

* the network is the source's: EfficientNet-B0, GeM pooling (its `p` a
  parameter in the checkpoint), one logit;
* the input is the whole frame resized to 1024x768, ImageNet-normalised;
* the frame and its mirror are averaged, so a frame and its mirror score the
  same;
* a checkpoint that is not this architecture at this input size, or not
  exactly its parameters, or not plain tensors, is refused.
"""

from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


def nvblas_preloaded() -> bool:
    """fishsense-core 4.1.0's `__init__` dlopens every `nvidia/*/lib/*.so`
    with RTLD_GLOBAL, libnvblas included; with no nvblas config, torch's CPU
    BLAS then segfaults (test_slate_detect_cpu_fallback.py). In a full
    run, collection has imported fishsense-core by now, so the in-process CPU
    tests below run only when this file runs on its own."""
    maps = Path("/proc/self/maps")
    return maps.exists() and "libnvblas" in maps.read_text()


if nvblas_preloaded():
    pytest.skip(
        "fishsense-core preloaded libnvblas; run this file on its own",
        allow_module_level=True,
    )

torch = pytest.importorskip("torch")
pytest.importorskip("torchvision")

# pylint: disable=wrong-import-position
from fishsense_services_processor.slate_detect import activities as act
from fishsense_services_processor.slate_detect import model as sut

SHA = "b8d377ba22d155e7056a5e9ae747fdd0970c7c73dee981bbee17d95c8156cf78"
#: The source's `asdict(TrainConfig())`, as final-q1 saved it.
CONFIG = {
    "arch": "efficientnet_b0",
    "epochs": 12,
    "image_size": 1024,
    "batch_size": 4,
    "lr": 3e-4,
    "weight_decay": 1e-4,
    "workers": 6,
    "seed": 0,
}
CPU = torch.device("cpu")


@pytest.fixture(name="checkpoint")
def _checkpoint(tmp_path):
    torch.manual_seed(0)
    path = tmp_path / "slate_efficientnet_b0.pt"
    torch.save({"config": CONFIG, "state_dict": sut.build_model().state_dict()}, path)
    return path


def _frame(seed=0, size=(1600, 1202)):
    rgb = np.random.default_rng(seed).integers(0, 255, (size[1], size[0], 3))
    return Image.fromarray(rgb.astype(np.uint8))


def test_the_network_is_the_sources():
    model = sut.build_model()

    assert isinstance(model.avgpool, sut.GeM)
    assert model.classifier[-1].out_features == 1
    assert model.classifier[-1].in_features == 1280
    assert model.classifier[0].inplace is False
    assert "avgpool.p" in model.state_dict()
    assert float(model.avgpool.p) == pytest.approx(3.0)


def test_the_whole_frame_is_resized_to_1024_by_768():
    x = sut.eval_transform()(_frame())

    assert tuple(x.shape) == (3, 768, 1024)
    assert x.dtype == torch.float32


def test_a_checkpoint_loads_and_scores_a_probability(checkpoint):
    classifier = sut.SlateClassifier.load(checkpoint, SHA, device=CPU)

    probability = classifier.probability(_frame())

    assert 0.0 <= probability <= 1.0
    assert classifier.weights_sha256 == SHA


class _LeftMinusRight(torch.nn.Module):
    """A logit that is the left half's mean minus the right half's: a
    frame's and its mirror's are opposite, so only their average is 0."""

    def forward(self, x):
        half = x.shape[-1] // 2
        return (x[..., :half].mean(dim=(1, 2, 3)) - x[..., half:].mean(dim=(1, 2, 3)))[
            :, None
        ]


def test_the_frame_and_its_mirror_are_averaged():
    """Test-time augmentation: the logits of the frame and its flip are
    averaged before the sigmoid (the source's `predict`)."""
    classifier = sut.SlateClassifier(_LeftMinusRight(), device=CPU, weights_sha256=SHA)
    rgb = np.zeros((1202, 1600, 3), dtype=np.uint8)
    rgb[:, :400] = 255

    assert classifier.probability(Image.fromarray(rgb)) == pytest.approx(0.5)


def test_it_runs_on_the_gpu_when_there_is_one(checkpoint):
    classifier = sut.SlateClassifier.load(checkpoint, SHA)
    # pylint: disable=protected-access
    expected = "cuda" if torch.cuda.is_available() else "cpu"
    assert classifier._device.type == expected


@pytest.mark.parametrize(
    "config",
    [{**CONFIG, "arch": "convnext_tiny"}, {**CONFIG, "image_size": 768}],
)
def test_another_architecture_or_size_is_refused(tmp_path, config):
    path = tmp_path / "other.pt"
    torch.save({"config": config, "state_dict": {}}, path)

    with pytest.raises(sut.CheckpointInvalid):
        sut.SlateClassifier.load(path, SHA, device=CPU)


def test_parameters_that_are_not_exactly_the_networks_are_refused(tmp_path):
    state = sut.build_model().state_dict()
    del state["avgpool.p"]
    path = tmp_path / "partial.pt"
    torch.save({"config": CONFIG, "state_dict": state}, path)

    with pytest.raises(sut.CheckpointInvalid, match="avgpool.p"):
        sut.SlateClassifier.load(path, SHA, device=CPU)


@pytest.mark.parametrize("content", [{"state_dict": {}}, [1, 2, 3]])
def test_something_else_is_refused(tmp_path, content):
    path = tmp_path / "other.pt"
    torch.save(content, path)

    with pytest.raises(sut.CheckpointInvalid):
        sut.SlateClassifier.load(path, SHA, device=CPU)


class _Payload:  # pylint: disable=too-few-public-methods
    """Not a tensor: `weights_only` must refuse to unpickle it."""


def test_only_plain_tensors_are_unpickled(tmp_path):
    path = tmp_path / "pickled.pt"
    with open(path, "wb") as handle:
        pickle.dump({"config": CONFIG, "state_dict": _Payload()}, handle)

    with pytest.raises(sut.CheckpointInvalid, match="unreadable"):
        sut.SlateClassifier.load(path, SHA, device=CPU)


def test_the_activity_names_a_refused_checkpoint_without_torch(tmp_path):
    """The activity sees its own `CheckpointInvalid`, which it maps to a
    non-retryable error, whatever the model module raised."""
    path = tmp_path / "other.pt"
    torch.save({"config": {**CONFIG, "arch": "resnet34"}, "state_dict": {}}, path)

    # pylint: disable=protected-access
    with pytest.raises(act.CheckpointInvalid, match="resnet34"):
        act._load_classifier(path, SHA)

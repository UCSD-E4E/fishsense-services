"""The slate presence classifier itself (needs the processor's `torch` extra).

Ported from 2026-10-03_slate_detector@95a77d95 src/slate_detector/train.py:
`GeM`, `build_model("efficientnet_b0")`, `transforms(1024, train=False)`,
`load_model` and `predict`. ImageNet EfficientNet-B0 with GeM pooling in
place of global average pooling -- the slate is about 1% of the frame, and
GeM leans toward the strongest local response instead of diluting it over the
water -- and one logit. The whole frame is resized to 1024x768 (h, w = 768,
1024), never cropped, normalised with ImageNet's statistics; the frame and its
horizontal flip are scored and their logits averaged before the sigmoid.

v2 changes:

* the network is built with no pretrained weights (`weights=None`): the
  checkpoint holds every parameter, and nothing is downloaded;
* the checkpoint is read with `weights_only=True`, and refused
  (`CheckpointInvalid`) unless it is the architecture and input size this
  port implements, with exactly its parameters;
* on a GPU it runs under fp16 autocast, as the source's `predict` did; on the
  CPU (the GPU queue's fallback) in fp32. The answer is the same to well
  under the threshold's resolution.

Imported only when the model loads: torch is not in the per-image or light
images, and the registry imports every stage wherever it runs.
"""

from __future__ import annotations

from pathlib import Path

import torch
from PIL import Image
from torch import nn
from torchvision import models
from torchvision.transforms import v2 as T

__all__ = [
    "ARCH",
    "IMAGE_SIZE",
    "CheckpointInvalid",
    "GeM",
    "SlateClassifier",
    "build_model",
    "eval_transform",
]

#: The source's `TrainConfig` the final-q1 checkpoint was trained with.
ARCH = "efficientnet_b0"
#: Long side; frames are 4:3, so 1024x768.
IMAGE_SIZE = 1024

_IMAGENET_MEAN = [0.485, 0.456, 0.406]
_IMAGENET_STD = [0.229, 0.224, 0.225]


class CheckpointInvalid(ValueError):
    """The checkpoint is not the classifier this port implements."""


class GeM(nn.Module):
    """Generalised-mean pooling (the source's, verbatim)."""

    def __init__(self, p: float = 3.0, eps: float = 1e-6):
        super().__init__()
        self.p = nn.Parameter(torch.tensor(p))
        self.eps = eps

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return (
            x.clamp(min=self.eps)
            .pow(self.p)
            .mean(dim=(-2, -1), keepdim=True)
            .pow(1.0 / self.p)
        )


def build_model() -> nn.Module:
    """EfficientNet-B0, GeM pooling, one logit; no pretrained download."""
    model = models.efficientnet_b0(weights=None)
    model.avgpool = GeM()
    # In-place dropout would overwrite GeM's output (the source's note; it
    # matters only when training, and is kept so the module is the same).
    model.classifier[0].inplace = False
    model.classifier[-1] = nn.Linear(model.classifier[-1].in_features, 1)
    return model


def eval_transform(image_size: int = IMAGE_SIZE) -> T.Compose:
    """The source's `transforms(image_size, train=False)`."""
    size = (image_size * 3 // 4, image_size)  # (h, w), landscape 4:3
    return T.Compose(
        [
            T.ToImage(),
            T.Resize(size, antialias=True),
            T.ToDtype(torch.float32, scale=True),
            T.Normalize(_IMAGENET_MEAN, _IMAGENET_STD),
        ]
    )


def _checked_config(checkpoint) -> dict:
    if not isinstance(checkpoint, dict) or not {"config", "state_dict"} <= set(
        checkpoint
    ):
        raise CheckpointInvalid("not a slate-detector checkpoint (config, state_dict)")
    config = checkpoint["config"]
    if config.get("arch") != ARCH:
        raise CheckpointInvalid(f"arch {config.get('arch')!r} is not {ARCH!r}")
    if config.get("image_size") != IMAGE_SIZE:
        raise CheckpointInvalid(
            f"image_size {config.get('image_size')!r} is not {IMAGE_SIZE}"
        )
    return config


class SlateClassifier:
    """P(slate) for a frame, from a loaded checkpoint."""

    def __init__(
        self, model: nn.Module, *, device: torch.device, weights_sha256: str
    ) -> None:
        self._model = model.to(device).to(memory_format=torch.channels_last).eval()
        self._device = device
        self._transform = eval_transform()
        self.weights_sha256 = weights_sha256

    @classmethod
    def load(
        cls, path: Path, weights_sha256: str, *, device: torch.device | None = None
    ) -> "SlateClassifier":
        """The verified checkpoint at `path`, on the GPU when there is one."""
        try:
            checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        except Exception as exc:  # pylint: disable=broad-except
            raise CheckpointInvalid(f"unreadable checkpoint: {exc}") from exc
        _checked_config(checkpoint)
        model = build_model()
        try:
            model.load_state_dict(checkpoint["state_dict"])
        except RuntimeError as exc:
            raise CheckpointInvalid(str(exc)) from exc
        if device is None:
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        return cls(model, device=device, weights_sha256=weights_sha256)

    @torch.no_grad()
    def probability(self, image: Image.Image) -> float:
        """P(slate): the frame and its horizontal flip, logits averaged."""
        x = self._transform(image.convert("RGB")).unsqueeze(0)
        x = x.to(self._device).to(memory_format=torch.channels_last)
        with torch.autocast(
            self._device.type,
            dtype=torch.float16,
            enabled=self._device.type == "cuda",
        ):
            logits = (self._model(x) + self._model(torch.flip(x, dims=[3]))) / 2
        return float(torch.sigmoid(logits.float()).squeeze())

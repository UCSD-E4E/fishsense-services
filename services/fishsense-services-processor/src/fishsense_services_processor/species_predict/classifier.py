"""BioCLIP over a closed set of species: the model rule, the encoder, the scoring.

New in v2 (no v1 counterpart). Ported from coral-gardeners-fish-detector@
67c8627 src/coral_fish_pipeline/classification/bioclip_classifier.py
(`BioCLIPClassifier`: the allowed and forbidden models, `_load_model`,
`_get_text_features`, `_get_logit_scale`, `_predict_image`). Its behaviour,
kept:

* only BioCLIP 2.5 (ViT-H/14) and BioCLIP 2; **the original BioCLIP is
  forbidden**, whatever form its id takes;
* each species' text embedding is the normalised mean of its four prompts'
  normalised embeddings (`species_prompts`);
* the image embedding is scored against them with the model's own
  `logit_scale` (100 when it has none), softmaxed over the closed set;
* top-5, the top-1 probability and the margin (top-1 minus top-2);
* fp16 weights and autocast on a GPU; fp32 on a CPU.

v2 changes:

* **no opt-in for the original BioCLIP** (coral-gardeners had
  `allow_original_bioclip`);
* the model is built from a local directory of verified weights
  (`local-dir:`; see `weights`), never fetched from the hub;
* the scoring is numpy, and torch sits behind the encoder seam
  (`OpenClipEncoder`), so only the GPU image needs it;
* the choices scored are the species labeling config's (the orchestrator's
  candidates), and a ranking names each by its taxonomy value.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

import numpy as np

from fishsense_services_contracts.species_prediction import (
    SPECIES_FALLBACK_MODEL_ID,
    SPECIES_PRIMARY_MODEL_ID,
    SPECIES_TOP_K,
    SpeciesCandidate,
    species_prompts,
)

__all__ = [
    "ALLOWED_BIOCLIP_MODELS",
    "FORBIDDEN_MODELS",
    "BioclipClassifier",
    "OpenClipEncoder",
    "Ranking",
    "is_original_bioclip",
    "is_out_of_memory",
    "mean_text_embedding",
    "rank",
    "validate_model_id",
]

ALLOWED_BIOCLIP_MODELS = frozenset(
    {SPECIES_PRIMARY_MODEL_ID, SPECIES_FALLBACK_MODEL_ID}
)
FORBIDDEN_MODELS = frozenset({"hf-hub:imageomics/bioclip", "imageomics/bioclip"})

#: coral-gardeners' `_get_logit_scale` fallback: CLIP's trained value.
DEFAULT_LOGIT_SCALE = 100.0


def is_original_bioclip(model_id: str) -> bool:
    """Whether `model_id` names the original BioCLIP, bare or with a
    revision, path or query after it (coral-gardeners' `_is_original_bioclip`)."""
    normalized = model_id.removeprefix("hf-hub:")
    for forbidden in FORBIDDEN_MODELS:
        marker = forbidden.removeprefix("hf-hub:")
        if normalized == marker:
            return True
        idx = normalized.find(marker)
        if idx >= 0:
            end = idx + len(marker)
            if end == len(normalized) or normalized[end] in {"/", ":", "@", "?"}:
                return True
    return False


def validate_model_id(model_id: str) -> str:
    """`model_id` if it is BioCLIP 2.5 or 2; the original is forbidden, and
    anything else unsupported."""
    if is_original_bioclip(model_id):
        raise ValueError(
            "Original BioCLIP is forbidden. Use BioCLIP 2.5 or BioCLIP 2 only."
        )
    if model_id not in ALLOWED_BIOCLIP_MODELS:
        raise ValueError(
            f"Unsupported BioCLIP model {model_id!r}. "
            f"Allowed: {sorted(ALLOWED_BIOCLIP_MODELS)}"
        )
    return model_id


def is_out_of_memory(exc: BaseException) -> bool:
    """A CUDA out-of-memory (torch raises a RuntimeError subclass), the one
    failure coral-gardeners answers by falling back to BioCLIP 2."""
    return isinstance(exc, RuntimeError) and "out of memory" in str(exc).lower()


def _normalise(x: np.ndarray) -> np.ndarray:
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


def mean_text_embedding(prompt_features: np.ndarray) -> np.ndarray:
    """One species' embedding: each prompt's normalised, then their mean."""
    return _normalise(_normalise(np.asarray(prompt_features, dtype=np.float64)).mean(0))


@dataclass(frozen=True)
class Ranking:
    #: ``(choice, probability)``, best first; at most `SPECIES_TOP_K`.
    top5: list[tuple[str, float]]
    top1_probability: float
    #: Top-1 minus top-2 (top-1 itself for a one-species set).
    margin: float


def rank(
    image_features: np.ndarray,
    text_features: np.ndarray,
    logit_scale: float,
    choices: Sequence[str],
) -> Ranking:
    """Softmax of the logit-scaled cosine similarities over the closed set."""
    image = _normalise(np.asarray(image_features, dtype=np.float64))
    text = _normalise(np.asarray(text_features, dtype=np.float64))
    logits = logit_scale * (text @ image)
    probs = np.exp(logits - logits.max())
    probs /= probs.sum()
    order = np.argsort(-probs, kind="stable")[: min(SPECIES_TOP_K, len(choices))]
    top = [(choices[int(i)], float(probs[i])) for i in order]
    top2 = top[1][1] if len(top) > 1 else 0.0
    return Ranking(top5=top, top1_probability=top[0][1], margin=top[0][1] - top2)


class Encoder(Protocol):
    """What the classifier needs of a model: embeddings as numpy."""

    logit_scale: float

    def encode_text(self, prompts: Sequence[str]) -> np.ndarray: ...

    def encode_image(self, image: Any) -> np.ndarray: ...


class OpenClipEncoder:
    """BioCLIP through open_clip, on the GPU when there is one. The only
    place torch and open_clip are imported, and only when a model loads."""

    def __init__(self, model: Any, preprocess: Any, tokenizer: Any, device: Any):
        self._model = model
        self._preprocess = preprocess
        self._tokenizer = tokenizer
        self.device = device
        self.logit_scale = self._logit_scale()

    @classmethod
    def load(cls, weights_dir: Path) -> "OpenClipEncoder":
        """Build from `weights_dir`: open_clip's `local-dir:` reads the
        pinned `open_clip_config.json` and the verified safetensors beside it,
        so nothing is downloaded."""
        # pylint: disable=import-outside-toplevel,import-error
        import open_clip
        import torch

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        name = f"local-dir:{weights_dir}"
        model, _, preprocess = open_clip.create_model_and_transforms(name)
        tokenizer = open_clip.get_tokenizer(name)
        model = model.eval().to(device)
        if torch.cuda.is_available():
            model = model.half()
        return cls(model, preprocess, tokenizer, device)

    def _logit_scale(self) -> float:
        scale = getattr(self._model, "logit_scale", None)
        if scale is None:
            return DEFAULT_LOGIT_SCALE
        return float(scale.detach().float().exp().item())

    def _on_gpu(self) -> bool:
        return getattr(self.device, "type", None) == "cuda"

    def encode_text(self, prompts: Sequence[str]) -> np.ndarray:
        import torch  # pylint: disable=import-outside-toplevel,import-error

        with torch.no_grad():
            tokens = self._tokenizer(list(prompts)).to(self.device)
            return self._model.encode_text(tokens).float().cpu().numpy()

    def encode_image(self, image: Any) -> np.ndarray:
        import torch  # pylint: disable=import-outside-toplevel,import-error

        tensor = self._preprocess(image).unsqueeze(0).to(self.device)
        if self._on_gpu():
            tensor = tensor.half()
        with torch.no_grad():
            with torch.autocast(
                device_type="cuda", dtype=torch.float16, enabled=self._on_gpu()
            ):
                features = self._model.encode_image(tensor)
            return features.float().cpu().numpy()[0]


class BioclipClassifier:
    """One loaded model, scoring crops against a candidate set. Each set's
    text embeddings are computed once."""

    def __init__(self, *, encoder: Encoder, model_id: str, predictor_version: int):
        self._encoder = encoder
        #: The verified weights' id (``bioclip/2.5-vith14@<sha256[:12]>``).
        self.model_id = model_id
        self.predictor_version = predictor_version
        self._text: dict[tuple[tuple[str, str], ...], np.ndarray] = {}

    def _text_features(self, candidates: Sequence[SpeciesCandidate]) -> np.ndarray:
        key = tuple((c.choice, c.scientific_name) for c in candidates)
        if key not in self._text:
            self._text[key] = np.stack(
                [
                    mean_text_embedding(
                        self._encoder.encode_text(species_prompts(c.scientific_name))
                    )
                    for c in candidates
                ]
            )
        return self._text[key]

    def classify(self, image: Any, candidates: Sequence[SpeciesCandidate]) -> Ranking:
        text = self._text_features(candidates)
        return rank(
            self._encoder.encode_image(image),
            text,
            self._encoder.logit_scale,
            [c.choice for c in candidates],
        )

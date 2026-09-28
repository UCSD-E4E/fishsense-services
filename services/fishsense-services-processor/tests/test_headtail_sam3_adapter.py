"""The SAM 3.1 adapter: autocast, the processor's real call shape, PIL input.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_predict_headtail_image.py `TestSam3AdapterAutocast`.
Names, bodies and reasons are v1's. These need torch, which only the GPU
image installs (the processor's `torch` extra), so the module skips without it.

SAM 3.1's weights are bfloat16 and `Sam3Processor` sets up no autocast: without
a context, every frame dies in `vitdet.forward` with a dtype mismatch -- a plain
retryable RuntimeError, which in prod looped on all 356 images of dive 94 while
holding a GPU (2026-09-08). Every other test stubs the very seam this adapter
implements, so only these can see it.
"""

from __future__ import annotations

import numpy as np

import pytest

torch = pytest.importorskip("torch")

# pylint: disable=wrong-import-position
from fishsense_services_processor.headtail_predict.predict import (  # noqa: E402
    _Sam3Adapter,
)


class _RecordingProcessor:
    """A stand-in with `Sam3Processor`'s REAL signatures: `set_image` returns
    the state, `set_text_prompt(prompt, state)` takes it back and *is* the
    inference call, and the result is a dict."""

    def __init__(self, device_type):
        self._device_type = device_type
        self.enabled_at_set_image = None
        self.enabled_at_predict = None
        self.dtype_at_predict = None
        self.image_type = None
        self.state_round_tripped = False

    def _sample(self):
        return (
            torch.is_autocast_enabled(self._device_type),
            torch.get_autocast_dtype(self._device_type),
        )

    def set_image(self, image, _state=None):
        self.enabled_at_set_image = self._sample()[0]
        self.image_type = type(image).__name__
        return {"sentinel": object()}

    def set_text_prompt(self, _prompt, state):
        self.enabled_at_predict, self.dtype_at_predict = self._sample()
        self.state_round_tripped = "sentinel" in state
        state["masks"] = []
        return state


def _device_type():
    return "cuda" if torch.cuda.is_available() else "cpu"


def _segment(processor):
    return _Sam3Adapter(processor).segment(np.zeros((32, 32, 3), dtype=np.uint8))


def test_inference_runs_under_bfloat16_autocast():
    processor = _RecordingProcessor(_device_type())

    assert not _segment(processor)

    assert processor.enabled_at_set_image is True, (
        "the vision backbone runs inside set_image -- that is where the dtype "
        "mismatch was raised"
    )
    assert processor.enabled_at_predict is True
    assert processor.dtype_at_predict is torch.bfloat16


def test_state_is_carried_from_set_image_into_the_prompt_call():
    processor = _RecordingProcessor(_device_type())
    _segment(processor)
    assert processor.state_round_tripped is True


def test_the_model_is_handed_a_pil_image_not_the_raw_array():
    """An HWC ndarray yields three-pixel-wide masks, silently."""
    processor = _RecordingProcessor(_device_type())
    _segment(processor)
    assert processor.image_type == "Image", processor.image_type


def test_autocast_does_not_leak_past_the_call():
    """A context manager, not `__enter__()`: the activity thread is reused."""
    _segment(_RecordingProcessor(_device_type()))
    assert torch.is_autocast_enabled(_device_type()) is False

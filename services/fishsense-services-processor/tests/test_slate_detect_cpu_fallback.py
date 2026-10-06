"""The GPU queue's CPU fallback, as a processor process runs it.

Found while porting the slate detector (2026-10-05), and **not the slate
stage's bug**: fishsense-core 4.1.0's `__init__` (`_preload_nvidia_libs`)
dlopens every `site-packages/nvidia/*/lib/*.so*` with RTLD_GLOBAL, libnvblas
included. With no `nvblas.conf`, any later torch CPU matrix multiply
segfaults ("[NVBLAS] CPU Blas library need to be provided"). The worker
imports every stage, and so fishsense-core, before any torch model runs, so
on the CPU-fallback Deployment every torch stage dies the same way (the slate
detector, laser prediction, head/tail's Mask R-CNN); on a GPU the convolutions
run on CUDA and it does not show. Importing torch first avoids it.

Fixed in the processor: its worker entry point imports torch first, wherever
torch is installed. Runs in a subprocess, so the crash cannot take the test session with it.
Needs the `torch` extra.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("torch")

_PROBE = textwrap.dedent("""
    {first}
    from fishsense_services_processor import worker  # the process's entry point
    from fishsense_services_processor import registry
    registry.stages()  # what the worker does at start: every stage imported
    import torch
    from PIL import Image
    from fishsense_services_processor.slate_detect import model
    classifier = model.SlateClassifier(
        model.build_model(), device=torch.device("cpu"), weights_sha256="0" * 64
    )
    print(classifier.probability(Image.new("RGB", (1600, 1202))))
    """)


def _cpu_inference(first: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _PROBE.format(first=first)],
        capture_output=True,
        text=True,
        timeout=600,
        env={"CUDA_VISIBLE_DEVICES": "", "PATH": "/usr/bin:/bin"},
        check=False,
    )


def test_cpu_inference_after_every_stage_is_imported():
    """The worker, the process's entry point, imports torch before anything
    imports fishsense-core (`fishsense_services_processor/worker.py`)."""
    result = _cpu_inference()
    assert result.returncode == 0, result.stderr[-2000:]


def test_importing_torch_first_avoids_it():
    """The workaround a fix in the processor would take."""
    result = _cpu_inference(first="import torch")
    assert result.returncode == 0, result.stderr[-2000:]

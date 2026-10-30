"""A processor worker's Temporal identity says its role, its device and its
release.

New in v2 (2026-10-19). Workers now run off NRP too (lab machines, deployed by
hand), and any worker polling a queue takes its work. The orchestrator's GPU
wake must know whether a *CUDA* worker is polling the GPU queue -- the CPU
fallback polls the same queue -- so the identity carries the device.

2026-10-30: the release too. The lab workers were on v0.72.0 when v0.74.0's
borrow check reached the per-image queue; the wake counted them and stood no
NRP pod up, and the check -- a workflow they did not have -- waited two
hours. A worker of another release is not the release's capacity.
"""

from fishsense_services_contracts.workers import (
    is_cuda_worker,
    is_per_image_worker,
    worker_identity,
)

RELEASE = "0.75.0"


def test_the_identity_names_host_role_device_and_release():
    assert worker_identity(
        pid=7, host="waiter", role="gpu", device="cuda", release=RELEASE
    ) == ("7@waiter/gpu/cuda/0.75.0")


def test_only_a_cuda_gpu_worker_of_the_release_counts_as_a_gpu():
    assert is_cuda_worker("7@waiter/gpu/cuda/0.75.0", RELEASE)
    assert not is_cuda_worker("7@waiter/gpu/cuda/0.74.0", RELEASE)  # stale
    assert not is_cuda_worker("7@waiter/gpu/cuda", RELEASE)  # before releases
    assert not is_cuda_worker("7@fallback-pod/gpu/cpu/0.75.0", RELEASE)
    assert not is_cuda_worker("7@waiter/per_image/cpu/0.75.0", RELEASE)
    assert not is_cuda_worker(
        "1@fishsense-processor-gpu-8454f69974-nnmlg", RELEASE
    )  # pre-identity


def test_a_per_image_worker_of_the_release_is_known_by_its_role_on_any_device():
    """The per-image wake (#158) asks whether a per-image worker -- an NRP
    pod or a lab render worker -- of this release already polls the
    processor queue."""
    assert is_per_image_worker("1@kastner-ml/per_image/cpu/0.75.0", RELEASE)
    assert is_per_image_worker("1@waiter/per_image/cuda/0.75.0", RELEASE)
    assert not is_per_image_worker("1@kastner-ml/per_image/cpu/0.72.0", RELEASE)
    assert not is_per_image_worker("1@kastner-ml/per_image/cpu", RELEASE)
    assert not is_per_image_worker("7@waiter/gpu/cuda/0.75.0", RELEASE)
    assert not is_per_image_worker("7@pod/light/cpu/0.75.0", RELEASE)
    assert not is_per_image_worker("1@fishsense-processor-per-image-abc", RELEASE)

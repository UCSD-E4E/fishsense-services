"""A processor worker's Temporal identity says its role and its device.

New in v2 (2026-10-19). Workers now run off NRP too (lab machines, deployed by
hand), and any worker polling a queue takes its work. The orchestrator's GPU
wake must know whether a *CUDA* worker is polling the GPU queue -- the CPU
fallback polls the same queue -- so the identity carries the device.
"""

from fishsense_services_contracts.workers import is_cuda_worker, worker_identity


def test_the_identity_names_host_role_and_device():
    assert worker_identity(pid=7, host="waiter", role="gpu", device="cuda") == (
        "7@waiter/gpu/cuda"
    )


def test_only_a_cuda_gpu_worker_counts_as_a_gpu():
    assert is_cuda_worker("7@waiter/gpu/cuda")
    assert not is_cuda_worker("7@fallback-pod/gpu/cpu")  # the CPU fallback
    assert not is_cuda_worker("7@waiter/per_image/cpu")
    assert not is_cuda_worker(
        "1@fishsense-processor-gpu-8454f69974-nnmlg"
    )  # pre-identity


def test_a_per_image_worker_is_known_by_its_role_on_any_device():
    """The per-image wake (#158) asks whether a per-image worker -- an NRP
    pod or a lab render worker -- already polls the processor queue."""
    from fishsense_services_contracts.workers import is_per_image_worker

    assert is_per_image_worker("1@kastner-ml/per_image/cpu")
    assert is_per_image_worker("1@waiter/per_image/cuda")
    assert not is_per_image_worker("7@waiter/gpu/cuda")
    assert not is_per_image_worker("7@pod/light/cpu")
    assert not is_per_image_worker("1@fishsense-processor-per-image-abc")

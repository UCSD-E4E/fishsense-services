"""How a processor worker names itself to Temporal: host, role, device.

New in v2 (2026-10-19). Workers run on NRP and, deployed by hand, on lab
machines too; any worker polling a queue takes its work. The orchestrator's
GPU wake asks Temporal which workers poll the GPU queue, and only a CUDA one
is a GPU -- the CPU fallback polls the same queue -- so the identity says the
device: ``{pid}@{host}/{role}/{device}``. Not a payload: no contract version.
"""

from __future__ import annotations

__all__ = ["CUDA", "CPU", "is_cuda_worker", "worker_identity"]

CUDA = "cuda"
CPU = "cpu"


def worker_identity(*, pid: int, host: str, role: str, device: str) -> str:
    return f"{pid}@{host}/{role}/{device}"


def is_cuda_worker(identity: str) -> bool:
    """A GPU-role worker with a CUDA device. An identity from before this
    convention (``pid@host``) is not counted: the wake then stands NRP up,
    as it always did."""
    parts = identity.split("/")
    return len(parts) == 3 and parts[1] == "gpu" and parts[2] == CUDA

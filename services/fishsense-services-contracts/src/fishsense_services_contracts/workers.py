"""How a processor worker names itself to Temporal: host, role, device,
release.

New in v2 (2026-10-19). Workers run on NRP and, deployed by hand, on lab
machines too; any worker polling a queue takes its work. The orchestrator's
GPU wake asks Temporal which workers poll the GPU queue, and only a CUDA one
is a GPU -- the CPU fallback polls the same queue -- so the identity says the
device: ``{pid}@{host}/{role}/{device}/{release}``. Not a payload: no
contract version.

The release (2026-10-30): the lab workers were on v0.72.0 when v0.74.0's
borrow check reached the per-image queue. The wake counted them and stood no
NRP pod up, and the check -- a workflow they did not have -- waited two hours
(mission 383). A worker counts only for the release it runs: the
orchestrator's, since release-please moves every package's version together.
An identity without one (a worker from before this) counts for none.
"""

from __future__ import annotations

__all__ = ["CUDA", "CPU", "is_cuda_worker", "is_per_image_worker", "worker_identity"]

CUDA = "cuda"
CPU = "cpu"


def worker_identity(
    *, pid: int, host: str, role: str, device: str, release: str
) -> str:
    return f"{pid}@{host}/{role}/{device}/{release}"


def _parts(identity: str, release: str) -> list[str] | None:
    parts = identity.split("/")
    return parts if len(parts) == 4 and parts[3] == release else None


def is_cuda_worker(identity: str, release: str) -> bool:
    """A GPU-role worker of `release` with a CUDA device. Any other identity
    is not counted: the wake then stands NRP up, as it always did."""
    parts = _parts(identity, release)
    return parts is not None and parts[1] == "gpu" and parts[2] == CUDA


def is_per_image_worker(identity: str, release: str) -> bool:
    """A per-image-role worker of `release` on any device: an NRP pod or a
    lab render worker (#158). Any other identity is not counted, so the wake
    stands NRP up as it always did."""
    parts = _parts(identity, release)
    return parts is not None and parts[1] == "per_image"

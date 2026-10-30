"""The processor's worker process: serve one role, on its queue.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/worker.py. v1
ran one role per Deployment from one codebase, chosen by settings; v2 keeps
that shape. The role comes from ``FISHSENSE_PROCESSOR_ROLE`` (``per_image``,
``light`` or ``gpu``; see `registry`), and what the role serves comes from the
stages that declare it. It connects with the shared Temporal settings
(``fishsense_services_contracts.temporal``) and has no database or NAS access
-- everything it needs arrives in the payload.

Kept from v1: the per-role activity cap, and a graceful drain on SIGTERM. The
drain matters more in v2: the orchestrator tears the processor down by
deleting its Deployment, which SIGTERMs a pod that may be mid-activity.

    FISHSENSE_PROCESSOR_ROLE=light python -m fishsense_services_processor
"""

# Import torch before anything imports fishsense-core, wherever torch is
# installed (the GPU image; the light image has none). fishsense-core 4.1.0's
# `__init__` dlopens every `site-packages/nvidia/*/lib/*.so*` with RTLD_GLOBAL,
# libnvblas included, and torch loaded after that segfaults on any CPU matrix
# multiply ("[NVBLAS] CPU Blas library need to be provided") -- the GPU
# queue's CPU-fallback Deployment, for every torch stage. Here, in the
# process's entry point, not the package's `__init__`: Temporal's workflow
# sandbox re-imports the package, and torch in the sandbox crashes it.
# Pinned by tests/test_slate_detect_cpu_fallback.py; remove once
# fishsense-core stops preloading libnvblas.
try:
    import torch  # noqa: F401  pylint: disable=unused-import
except ImportError:
    pass

import asyncio
import logging
import os
import signal
import socket
from importlib.metadata import version
from collections.abc import Iterable
from datetime import timedelta
from typing import Any, Literal

from pydantic import PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict
from temporalio.client import Client
from temporalio.worker import Worker

from fishsense_services_contracts.temporal import TemporalConnection, connect_options
from fishsense_services_contracts.workers import CPU, CUDA, worker_identity
from fishsense_services_processor.registry import (
    ROLE_GPU,
    Stage,
    gpu_activity_cap,
    registration_for_role,
)

__all__ = [
    "GRACEFUL_SHUTDOWN_TIMEOUT",
    "ProcessorSettings",
    "build_worker",
    "main",
    "run",
]

log = logging.getLogger(__name__)

#: How long in-flight activities get to finish when the pod is told to stop
#: (v1's 30 s). Every manifest's ``terminationGracePeriodSeconds`` is longer.
#: Without it the work in progress is canceled at once and re-run -- safe,
#: since activities are idempotent, but it throws away a nearly done image.
GRACEFUL_SHUTDOWN_TIMEOUT = timedelta(seconds=30)


class ProcessorSettings(BaseSettings):
    """Which role this process serves, from ``FISHSENSE_PROCESSOR_*``."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_PROCESSOR_")

    #: Required: a pod whose manifest forgot it must fail to start, not serve
    #: some other queue while its own sits unpolled.
    role: Literal["per_image", "light", "gpu"]
    #: Lowers (or raises) the role's cap; see
    #: `registry.ROLE_MAX_CONCURRENT_ACTIVITIES` before raising one.
    max_concurrent_activities: PositiveInt | None = None


_DETECT = object()


def detect_gpu_memory_bytes() -> int | None:
    """The CUDA card's total memory, or None without one (the CPU fallback).
    torch is imported first in this process (`main`), so this is cheap."""
    try:
        import torch  # pylint: disable=import-outside-toplevel
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None
    return int(torch.cuda.get_device_properties(0).total_memory)


def _role_cap(role: str, default: int, total: int | None) -> int:
    if role != ROLE_GPU:
        return default
    cap = gpu_activity_cap(total)
    log.info("GPU memory %s bytes: %d activities at once", total, cap)
    return cap


def _identity(role: str, total: int | None) -> str:
    """``pid@host/role/device/release`` (`contracts.workers`): the
    orchestrator's wakes count a worker of their release on the queue,
    wherever it runs."""
    device = CUDA if role == ROLE_GPU and total is not None else CPU
    return worker_identity(
        pid=os.getpid(), host=socket.gethostname(), role=role, device=device,
        release=version("fishsense-services-processor"),
    )  # fmt: skip


def build_worker(
    client: Client,
    *,
    role: str,
    stages: Iterable[Stage] | None = None,
    max_concurrent_activities: int | None = None,
    gpu_memory_bytes: Any = _DETECT,
) -> Worker:
    """The Temporal worker for one role. The GPU role's cap is sized by its
    card (`registry.gpu_activity_cap`) unless the pod overrides it."""
    registration = registration_for_role(role, stages)
    if not registration.workflows and not registration.activities:
        raise ValueError(
            f"the {role!r} role has nothing registered: no ported stage declares it"
        )
    total = (
        (detect_gpu_memory_bytes() if gpu_memory_bytes is _DETECT else gpu_memory_bytes)
        if role == ROLE_GPU
        else None
    )
    return Worker(
        client,
        task_queue=registration.task_queue,
        workflows=list(registration.workflows),
        activities=list(registration.activities),
        max_concurrent_activities=max_concurrent_activities
        or _role_cap(role, registration.max_concurrent_activities, total),
        identity=_identity(role, total),
        graceful_shutdown_timeout=GRACEFUL_SHUTDOWN_TIMEOUT,
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = ProcessorSettings()
    temporal = TemporalConnection()
    options = connect_options(temporal)
    client = await Client.connect(**options)
    worker = build_worker(
        client,
        role=settings.role,
        max_concurrent_activities=settings.max_concurrent_activities,
    )
    log.info(
        "serving role=%s queue=%s on Temporal address=%s namespace=%s tls=%s",
        settings.role,
        worker.task_queue,
        temporal.address,
        temporal.namespace,
        bool(options["tls"]),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with worker:
        await stop.wait()
        log.info(
            "shutdown signal received; draining (graceful_shutdown_timeout=%s)",
            GRACEFUL_SHUTDOWN_TIMEOUT,
        )


def run() -> None:
    asyncio.run(main())

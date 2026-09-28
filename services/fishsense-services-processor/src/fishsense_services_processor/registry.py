"""The processor's roles, and the stage registry that fills them.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/roles.py and
role_names.py. The processor runs in one of three roles, one per NRP
Deployment, each polling its own contract queue (v1's split, for v1's reasons):

* ``per_image`` -- the per-image fan-out stages. Each decodes a full-res `.ORF`
  and peaks at 1-3 GB, so this role's concurrency is capped by memory;
* ``light`` -- the stages that hold no image bytes: rows in, numpy, rows out.
  Split from ``per_image`` for memory, not CPU: a sub-second line fit must not
  inherit the decoders' cap (v1 moved them on 2026-09-04, after three of four
  auto-accept firings expired on ScheduleToStart behind one preprocess);
* ``gpu`` -- the torch inference stages. The queue means *prefer* a GPU: when
  the GPU Deployment can't start, a CPU-only one serves the same queue
  (orchestrator ``nrp.gpu_fallback``).

v2 changes: the roles are filled by the stages, not by three lists. Each stage
package declares its `STAGE` in `<package>/stage.py` -- its role, workflows and
activities -- as the orchestrator's do, so porting a stage adds a package and
edits nothing shared. v1's ``cpu`` is ``per_image``, and there is no ``all``
role (see `tests/test_roles.py`).
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any, Final

from fishsense_services_contracts import (
    PROCESSOR_GPU_TASK_QUEUE,
    PROCESSOR_LIGHT_TASK_QUEUE,
    PROCESSOR_TASK_QUEUE,
)

__all__ = [
    "ROLES",
    "ROLE_GPU",
    "ROLE_LIGHT",
    "ROLE_MAX_CONCURRENT_ACTIVITIES",
    "ROLE_PER_IMAGE",
    "ROLE_TASK_QUEUES",
    "Registration",
    "Stage",
    "registration_for_role",
    "stages",
]

ROLE_PER_IMAGE: Final = "per_image"
ROLE_LIGHT: Final = "light"
ROLE_GPU: Final = "gpu"

#: Every value ``FISHSENSE_PROCESSOR_ROLE`` accepts.
ROLES: Final = (ROLE_PER_IMAGE, ROLE_LIGHT, ROLE_GPU)

ROLE_TASK_QUEUES: Final[dict[str, str]] = {
    ROLE_PER_IMAGE: PROCESSOR_TASK_QUEUE,
    ROLE_LIGHT: PROCESSOR_LIGHT_TASK_QUEUE,
    ROLE_GPU: PROCESSOR_GPU_TASK_QUEUE,
}

#: Each role's activity cap, and why (a pod may lower it, e.g. the GPU
#: queue's CPU fallback runs 1):
#:
#: * ``per_image`` 2 -- a memory ceiling. Each activity peaks at 1-3 GB in a
#:   rawpy decode; v1's pod OOMKilled into CrashLoopBackOff at the SDK default
#:   of 100, and again at 4 (17 restarts, 2026-07-21). Throughput comes from
#:   replicas instead;
#: * ``light`` 8 -- nothing here decodes an image, and every stage drains one
#:   dive per firing, so a busy hour never queues;
#: * ``gpu`` 2 -- v1's GPU pod shared the per-image settings; it still decodes
#:   a raw frame per image, so the same memory ceiling holds.
ROLE_MAX_CONCURRENT_ACTIVITIES: Final[dict[str, int]] = {
    ROLE_PER_IMAGE: 2,
    ROLE_LIGHT: 8,
    ROLE_GPU: 2,
}


@dataclass(frozen=True)
class Stage:
    """One ported stage's processor side, and the role that serves it. An
    activity runs on its workflow's queue, so a stage's workflows and the
    activities they call share its role."""

    name: str
    role: str
    workflows: Sequence[type]
    activities: Sequence[Callable[..., Any]]


@dataclass(frozen=True)
class Registration:
    """One Temporal worker's worth of wiring: a queue and what serves it."""

    task_queue: str
    workflows: Sequence[type]
    activities: Sequence[Callable[..., Any]]
    max_concurrent_activities: int


@cache
def stages() -> tuple[Stage, ...]:
    """Every `<package>/stage.py`'s `STAGE`, in package-name order."""
    import fishsense_services_processor as root  # pylint: disable=import-outside-toplevel

    found = []
    for module in sorted(pkgutil.iter_modules(root.__path__), key=lambda m: m.name):
        if not module.ispkg:
            continue
        name = f"{root.__name__}.{module.name}.stage"
        if importlib.util.find_spec(name) is None:
            continue
        found.append(importlib.import_module(name).STAGE)
    return tuple(found)


def registration_for_role(
    role: str, stages_: Iterable[Stage] | None = None
) -> Registration:
    """The wiring for one role: its queue, and every stage declaring it.

    Raises for an unknown role (v1's ``cpu`` and ``all`` included) rather than
    guessing, since a pod serving the wrong queue is a stall with no error.
    """
    if role not in ROLES:
        raise ValueError(
            f"{role!r} is not a processor role; expected one of {list(ROLES)}"
        )
    mine = [s for s in (stages() if stages_ is None else stages_) if s.role == role]
    return Registration(
        task_queue=ROLE_TASK_QUEUES[role],
        workflows=tuple(w for s in mine for w in s.workflows),
        activities=tuple(a for s in mine for a in s.activities),
        max_concurrent_activities=ROLE_MAX_CONCURRENT_ACTIVITIES[role],
    )

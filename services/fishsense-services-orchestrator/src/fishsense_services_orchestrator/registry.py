"""The stage registry: how the orchestrator finds what it serves.

Each stage package declares a `STAGE` in its `stage.py`: its workflows, its
schedules, and how to build its activities from the shared `Deps`. `stages()`
discovers them, and the worker, the schedules and the tests all derive from
that, so adding a stage adds a package and edits nothing shared. That is what
lets stages be ported on parallel branches without colliding in one list.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cache
from typing import Any

from temporalio.client import ScheduleOverlapPolicy

__all__ = ["Deps", "ScheduledWorkflow", "Stage", "stages"]


@dataclass(frozen=True)
class Deps:
    """What every stage's activities are built from. A stage reads its own
    settings (the NAS, Label Studio, ...) when it builds, so a stage's missing
    configuration fails the worker's startup, not its first run."""

    #: The app role's engine; catalogs act on it as `sub`.
    engine: Any
    #: The orchestrator's service principal (PLAN.md §9.11).
    sub: str


@dataclass(frozen=True)
class ScheduledWorkflow:
    schedule_id: str
    workflow: Any
    every: timedelta
    offset: timedelta
    run_timeout: timedelta
    overlap: ScheduleOverlapPolicy


@dataclass(frozen=True)
class Stage:
    name: str
    workflows: Sequence[type]
    #: The stage's activities, as bound methods, built once at startup.
    build_activities: Callable[[Deps], Sequence[Callable]]
    schedules: Sequence[ScheduledWorkflow] = field(default_factory=tuple)


@cache
def stages() -> tuple[Stage, ...]:
    """Every `<package>/stage.py`'s `STAGE`, in package-name order."""
    import fishsense_services_orchestrator as root  # pylint: disable=import-outside-toplevel

    found = []
    for module in sorted(pkgutil.iter_modules(root.__path__), key=lambda m: m.name):
        if not module.ispkg:
            continue
        name = f"{root.__name__}.{module.name}.stage"
        if importlib.util.find_spec(name) is None:
            continue
        found.append(importlib.import_module(name).STAGE)
    return tuple(found)

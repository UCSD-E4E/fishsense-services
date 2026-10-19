"""Temporal type names never change (Phase 2a renamed dives to missions).

Temporal records every workflow and activity by its type name, and a running
workflow replays against the code by those names. So renaming Python code
must never rename what Temporal sees: a renamed class or function pins its
old name (`@workflow.defn(name="IngestDiveWorkflow")`). This compares every
type name the orchestrator and processor register with the set recorded when
the rename landed (`fixtures/temporal_names.json`, 68 workflows and 128
activities, identical to origin/main at 2026-10-18).

Adding new Temporal work adds names here; removing or renaming one needs a
plan for the histories that name it.
"""

import importlib
import inspect
import json
import pkgutil
from pathlib import Path

from temporalio import activity, workflow

FIXTURE = Path(__file__).parent / "fixtures" / "temporal_names.json"


def _registered() -> dict[str, set[str]]:
    names = {"workflow": set(), "activity": set()}
    for package in ("fishsense_services_orchestrator", "fishsense_services_processor"):
        root = importlib.import_module(package)
        for info in pkgutil.walk_packages(root.__path__, package + "."):
            if info.name.endswith("__main__"):
                continue  # an entry point runs when imported; it defines no types
            try:
                module = importlib.import_module(info.name)
            except ModuleNotFoundError:
                continue  # an optional model dependency (slate_detect.model)
            for value in vars(module).values():
                if isinstance(value, type):
                    defn = workflow._Definition.from_class(value)
                    if defn is not None and defn.name:
                        names["workflow"].add(defn.name)
                    for _, member in inspect.getmembers(value):
                        if callable(member):
                            act = activity._Definition.from_callable(member)
                            if act is not None and act.name:
                                names["activity"].add(act.name)
                elif callable(value):
                    act = activity._Definition.from_callable(value)
                    if act is not None and act.name:
                        names["activity"].add(act.name)
    return names


def test_no_temporal_type_name_was_lost_or_renamed():
    recorded = json.loads(FIXTURE.read_text())
    registered = _registered()

    for kind in ("workflow", "activity"):
        lost = set(recorded[kind]) - registered[kind]
        assert not lost, f"{kind} type names no longer registered: {sorted(lost)}"

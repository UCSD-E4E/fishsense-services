"""What `current_measurements` does to answer, not only what it answers (0060).

Its rules are pinned where they are used (test_measurements.py,
test_measurement_store.py, test_depth_measure_schema.py). Here: the view does
no work a row's rules cannot need. On production (2026-10-21) it joined every
species label's subject to every measurement and expressed each rule as a
filter the planner could not estimate. It guessed 24 current measurements of
3,314, and the joins above it, planned for 24, took the fish list to 3.0 s.
"""

from __future__ import annotations

import json

from depth_measure_seed import (  # noqa: F401
    calibrated_dive,
    fish,
    forget_identities,
    measurable_capture,
    measurement,
    tenant,
)
from sqlalchemy import text

from fishsense_services_api.db import tenant_transaction


def _executed(plan, relation: str) -> int:
    """How many times plan nodes reading ``relation`` ran."""
    here = plan.get("Actual Loops", 0) if plan.get("Relation Name") == relation else 0
    return here + sum(_executed(p, relation) for p in plan.get("Plans", []))


async def _analyze(app_engine, tenant_id, sql: str):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        found = await conn.execute(
            text(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}"), {"t": tenant_id}
        )
        return found.scalar_one()[0]["Plan"]


async def test_a_low_priority_missions_subjects_are_never_read(
    owner_engine, app_engine
):
    """The stale-binding rule holds on high-priority missions only (0028), so
    elsewhere the capture's subject cannot change the answer and is not
    looked up."""
    lab = await tenant(owner_engine)
    mission_id, calibration = await calibrated_dive(owner_engine, lab, priority="low")
    snook = await fish(owner_engine, lab, model="Snook")
    image = await measurable_capture(
        owner_engine, lab, mission_id, "Fish Model, Grouper", in_cluster=False
    )
    await measurement(owner_engine, lab, image, snook, calibration)

    plan = await _analyze(
        app_engine, lab, "SELECT * FROM current_measurements WHERE tenant_id = :t"
    )

    assert plan["Actual Rows"] == 1
    assert _executed(plan, "species_labels") == 0


async def test_a_high_priority_missions_subject_is_read_for_its_own_capture(
    owner_engine, app_engine
):
    """Where the rule holds, the subject is looked up by the capture: not every
    species label in the tenant."""
    lab = await tenant(owner_engine)
    mission_id, calibration = await calibrated_dive(owner_engine, lab)
    grouper = await fish(owner_engine, lab, model="Grouper")
    image = await measurable_capture(
        owner_engine, lab, mission_id, "Fish Model, Grouper", in_cluster=False
    )
    await measurement(owner_engine, lab, image, grouper, calibration)
    for _ in range(5):  # other frames' labels, which the answer cannot need
        await measurable_capture(
            owner_engine, lab, mission_id, "Fish Model, Grouper", in_cluster=False
        )
    async with owner_engine.begin() as conn:
        await conn.execute(text("ANALYZE species_labels"))

    plan = await _analyze(
        app_engine, lab, "SELECT * FROM current_measurements WHERE tenant_id = :t"
    )

    assert plan["Actual Rows"] == 1
    assert _scanned_rows(plan, "species_labels") <= 1, json.dumps(plan)[:2000]


def _scanned_rows(plan, relation: str) -> int:
    """How many rows plan nodes reading ``relation`` returned, over all loops."""
    here = 0
    if plan.get("Relation Name") == relation:
        here = plan.get("Actual Rows", 0) * plan.get("Actual Loops", 0)
    return here + sum(_scanned_rows(p, relation) for p in plan.get("Plans", []))

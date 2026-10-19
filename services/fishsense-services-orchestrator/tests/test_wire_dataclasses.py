"""The orchestrator's dataclass payloads keep their old names on the wire.

Phase 2a renamed dives to missions. A pydantic model writes its aliases
itself; a stdlib dataclass's fields are inferred, so the contracts'
DATA_CONVERTER dumps each dataclass by alias: what this version writes, a
running workflow (and a rollback to v0.11) reads.
"""

import json
import uuid

from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_orchestrator.automatic_results.activities import AutomaticTarget
from fishsense_services_orchestrator.automatic_results.workflow import (
    AutomaticMissionReport,
    AutomaticRunReport,
)

CONVERTER = DATA_CONVERTER.payload_converter
TENANT, MISSION = uuid.UUID(int=1), uuid.UUID(int=2)


def _wire(value) -> dict:
    [payload] = CONVERTER.to_payloads([value])
    return json.loads(payload.data)


def _read(data, type_):
    [payload] = CONVERTER.to_payloads([data])
    [value] = CONVERTER.from_payloads([payload], [type_])
    return value


def test_a_dataclass_is_written_under_its_old_names():
    assert _wire(AutomaticTarget(tenant_id=TENANT, mission_id=MISSION)) == {
        "tenant_id": str(TENANT),
        "dive_id": str(MISSION),
    }


def test_nested_dataclasses_too():
    report = AutomaticRunReport(missions=[], stopped="backlog empty")

    assert set(_wire(report)) == {"dives", "stopped"}


def test_either_name_reads():
    old = _read({"tenant_id": str(TENANT), "dive_id": str(MISSION)}, AutomaticTarget)
    new = _read({"tenant_id": str(TENANT), "mission_id": str(MISSION)}, AutomaticTarget)

    assert old == new == AutomaticTarget(tenant_id=TENANT, mission_id=MISSION)


def test_a_list_of_dataclasses_too():
    wire = _wire([AutomaticTarget(tenant_id=TENANT, mission_id=MISSION)])

    assert wire == [{"tenant_id": str(TENANT), "dive_id": str(MISSION)}]


def test_the_report_dataclass_is_importable():
    assert AutomaticMissionReport is not None

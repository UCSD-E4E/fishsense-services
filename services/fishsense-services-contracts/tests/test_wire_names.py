"""Renamed fields keep their names on the wire (Phase 2a, `renamed.py`).

Phase 2a renamed dives to missions. Temporal keeps every payload it has
seen, and the processor deploys days apart from the orchestrator, so the JSON
between them must not change: a renamed field is written under its old name
and read under either. Inside the code, a model is its new self --
`model_dump()` and attribute access use the new names.
"""

import json
import uuid

from fishsense_services_contracts import CONTRACT_VERSION
from fishsense_services_contracts.clustering import ClusterMissionFramesInput
from fishsense_services_contracts.temporal import DATA_CONVERTER, connect_options
from fishsense_services_contracts.temporal import TemporalConnection

MISSION = uuid.UUID(int=7)
CONVERTER = DATA_CONVERTER.payload_converter


def _payload(value) -> dict:
    [payload] = CONVERTER.to_payloads([value])
    return json.loads(payload.data)


def _from(data: dict, type_):
    [payload] = CONVERTER.to_payloads([data])
    [value] = CONVERTER.from_payloads([payload], [type_])
    return value


def test_a_renamed_field_is_written_under_its_old_name():
    wire = _payload(ClusterMissionFramesInput(mission_id=MISSION, images=[]))

    assert wire == {"dive_id": str(MISSION), "images": []}


def test_a_renamed_field_is_read_under_either_name():
    old = _from({"dive_id": str(MISSION), "images": []}, ClusterMissionFramesInput)
    new = _from({"mission_id": str(MISSION), "images": []}, ClusterMissionFramesInput)

    assert old.mission_id == new.mission_id == MISSION


def test_inside_the_code_a_model_has_its_new_names():
    model = ClusterMissionFramesInput(mission_id=MISSION, images=[])

    assert model.model_dump() == {"mission_id": MISSION, "images": []}


def test_the_workers_and_clients_use_this_converter():
    options = connect_options(
        TemporalConnection(address="localhost:7233", namespace="fishsense")
    )

    assert options["data_converter"] is DATA_CONVERTER


def test_the_rename_is_version_7_and_later_versions_keep_its_wire_names():
    """v7 was the rename alone; v8 (render versions) added optional fields."""
    assert CONTRACT_VERSION >= 7

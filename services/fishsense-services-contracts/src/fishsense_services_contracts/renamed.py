"""A payload field renamed, its old name kept on the wire.

Phase 2a renamed dives to missions (PLAN.md §9.26, migration 0046). Payloads
cross Temporal as JSON, and Temporal keeps every one it has seen: a workflow
that was running at the deploy replays its history with the old names, and a
processor a version behind (its NRP pods are torn down only when idle) reads
and writes them. So a renamed field is accepted under either name, and a
pydantic model (with ``serialize_by_alias``) still writes the old one:

    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
"""

from pydantic import AliasChoices, Field
from pydantic.fields import FieldInfo

__all__ = ["renamed_from"]


def renamed_from(old: str, new: str) -> FieldInfo:
    # The old name first: it is what the JSON schema shows, unchanged.
    return Field(validation_alias=AliasChoices(old, new), serialization_alias=old)

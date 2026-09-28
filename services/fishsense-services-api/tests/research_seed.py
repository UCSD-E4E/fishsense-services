"""Seeding for the research-view tests: fish-model measurements in the lab.

Ported in spirit from fishsense-lite@77e8f8e5 services/fishsense-api/tests/
test_fish_model_reference.py (`_seed_measurement`), test_fish_length_estimate_
view.py (`_seed`) and test_fish_model_mislabel_suspects.py (`_measure`): a
measurement of a named model in a dive, with the dive, the frame and the model's
Fish created on first use. v1 had one Fish per model (`uq_fish_name`); here one
per model per tenant, found by its fish model.

The views read the lab tenant only (research_02), so everything is seeded there.
Constructors, not fixtures -- except `references`, which isolates the global
reference lengths a test sets.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from depth_measure_seed import (
    _one,
    calibrated_dive,
    capture,
    exec_,
    fish,
)
from depth_measure_seed import measurement as _measurement

#: A research login: a member of the NOLOGIN group role the research
#: migrations create, the way an operator provisions one (PLAN.md §9.20).
#: conftest.py creates it once per session.
RESEARCH_GROUP = "fishsense_research"
RESEARCH_LOGIN = "research_reader"
RESEARCH_PASSWORD = "research_reader"
#: What a research session changes, and all it changes: v1's table names first.
RESEARCH_SEARCH_PATH = "v1, public"

_REFERENCE_COLUMNS = (
    "name, known_length_m, is_provisional, notes, valid_from, v1_id, number"
)


@pytest.fixture
async def references(
    owner_engine: AsyncEngine,
) -> AsyncIterator[Callable[..., Awaitable[None]]]:
    """Set a model's known length (a new reference version) for this test only.

    Reference lengths are global and other tests leave theirs behind, and the
    mislabel view compares a frame against *every* reference -- so this test's
    references must be the only ones. The rest are set aside and put back."""
    async with owner_engine.begin() as conn:
        kept = (
            await conn.execute(
                text(f"SELECT {_REFERENCE_COLUMNS} FROM fish_model_references")
            )
        ).mappings()
        kept = [dict(r) for r in kept]
        await conn.execute(text("DELETE FROM fish_model_references"))

    async def add(name, known_length_m, *, provisional=False, valid_from=None):
        await exec_(
            owner_engine,
            "INSERT INTO fish_models (name) VALUES (:n) ON CONFLICT DO NOTHING",
            n=name,
        )
        await exec_(
            owner_engine,
            "INSERT INTO fish_model_references (name, known_length_m, "
            "is_provisional, valid_from) VALUES (:n, :l, :p, "
            "coalesce(:at, now()))",
            n=name,
            l=known_length_m,
            p=provisional,
            at=valid_from and datetime.fromisoformat(valid_from).replace(tzinfo=UTC),
        )

    yield add
    async with owner_engine.begin() as conn:
        await conn.execute(text("DELETE FROM fish_model_references"))
        if kept:
            await conn.execute(
                text(
                    f"INSERT INTO fish_model_references ({_REFERENCE_COLUMNS}) "
                    "VALUES (:name, :known_length_m, :is_provisional, :notes, "
                    ":valid_from, :v1_id, :number)"
                ),
                kept,
            )


class Lab:
    """The lab tenant's dives and model fish, created on first use."""

    def __init__(self, owner_engine: AsyncEngine, tenant_id) -> None:
        self.owner_engine = owner_engine
        self.tenant_id = tenant_id
        self._dives: dict[int, tuple] = {}
        self._fish: dict[str | None, object] = {}

    async def dive(self, key: int):
        """(dive id, its laser calibration) for the test's dive `key`."""
        if key not in self._dives:
            self._dives[key] = await calibrated_dive(self.owner_engine, self.tenant_id)
        return self._dives[key]

    async def model_fish(self, model: str):
        if model not in self._fish:
            self._fish[model] = await fish(
                self.owner_engine, self.tenant_id, model=model
            )
        return self._fish[model]

    async def measure(self, *, dive: int, model: str, length_m, v1_id=None):
        """One frame of `model` in dive `dive`, measured at `length_m`.
        Returns (capture id, measurement id)."""
        dive_id, calibration_id = await self.dive(dive)
        capture_id = await capture(self.owner_engine, self.tenant_id, dive_id)
        measurement_id = await _measurement(
            self.owner_engine,
            self.tenant_id,
            capture_id,
            await self.model_fish(model),
            calibration_id,
            length_m=length_m,
            v1_id=v1_id,
        )
        return capture_id, measurement_id

    async def number(self, table: str, row_id) -> int:
        return await _one(
            self.owner_engine, f"SELECT number FROM {table} WHERE id = :id", id=row_id
        )


async def rows(engine: AsyncEngine, sql: str, **params) -> list[dict]:
    async with engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(text(sql), params)).mappings()]

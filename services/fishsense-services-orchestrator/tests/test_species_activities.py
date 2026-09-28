"""The species stage's thin activities: the two selectors, the flag clear, and
creating the project.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/ (test_select_next_high_priority_dive_for_species_preprocessing_activity.py,
test_select_dives_needing_species_population_activity.py) and the activities
they had no tests for (clear_species_reprocess_flags_activity.py,
create_species_label_studio_project_activity.py). v1's were thin SDK calls;
v2's call the species catalog, which owns the cohorts (tested on Postgres in
the API's test_species_store.py).

v2 changes, pinned here: the selectors take the oldest candidate across every
tenant the orchestrator serves (v1: the lowest dive id); the population
cohort is listed across tenants, oldest first; the flag clear is scoped by
capture (v1: by checksum -- the same frames, since only a canonical capture
is drawn); the project is found or created through `LabelProjects`, with v1's
suffix and XML.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from temporalio.testing import ActivityEnvironment

from fishsense_services_api.species_store import SpeciesCandidate
from fishsense_services_orchestrator.species.activities import SpeciesActivities
from fishsense_services_orchestrator.species.contracts import (
    ClearReprocessFlagsInput,
    SpeciesTarget,
)
from fishsense_services_orchestrator.species.labeling import (
    SPECIES_LABELING_CONFIG_XML,
)

from ._species import DIVE, TENANT, FakeSpeciesCatalog, FakeStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)
REEF = uuid.uuid4()


def _activities(catalog, **kwargs):
    return SpeciesActivities(catalog=catalog, store=FakeStore(), **kwargs)


async def test_passes_through_the_oldest_candidate_across_tenants():
    old, new = uuid.uuid4(), uuid.uuid4()
    catalog = FakeSpeciesCatalog(
        tenants=[TENANT, REEF],
        candidates={
            TENANT: SpeciesCandidate(new, T0 + timedelta(days=1)),
            REEF: SpeciesCandidate(old, T0),
        },
    )

    target = await ActivityEnvironment().run(
        _activities(catalog).select_next_dive_for_species_preprocessing
    )

    assert target == SpeciesTarget(REEF, old)


async def test_returns_none_when_no_tenant_has_a_candidate():
    catalog = FakeSpeciesCatalog(tenants=[TENANT, REEF])

    assert (
        await ActivityEnvironment().run(
            _activities(catalog).select_next_dive_for_species_preprocessing
        )
        is None
    )


async def test_passes_through_every_dive_needing_population_oldest_first():
    a, b, c = (uuid.uuid4() for _ in range(3))
    catalog = FakeSpeciesCatalog(
        tenants=[TENANT, REEF],
        population_candidates={
            TENANT: [SpeciesCandidate(a, T0), SpeciesCandidate(c, T0 + timedelta(2))],
            REEF: [SpeciesCandidate(b, T0 + timedelta(1))],
        },
    )

    targets = await ActivityEnvironment().run(
        _activities(catalog).select_dives_needing_species_population
    )

    assert targets == [
        SpeciesTarget(TENANT, a),
        SpeciesTarget(REEF, b),
        SpeciesTarget(TENANT, c),
    ]


async def test_returns_empty_list_when_cohort_empty():
    catalog = FakeSpeciesCatalog()

    assert (
        await ActivityEnvironment().run(
            _activities(catalog).select_dives_needing_species_population
        )
        == []
    )


async def test_the_clear_lowers_the_named_frames_or_the_whole_dive():
    """`None` is the whole dive (the no-work backstop); a list -- empty
    included -- is only those frames (v1's `ClearReprocessFlagsInput`)."""
    catalog = FakeSpeciesCatalog()
    activities = _activities(catalog)
    frame = uuid.uuid4()

    for scope in (None, [frame], []):
        await ActivityEnvironment().run(
            activities.clear_species_reprocess_flags,
            ClearReprocessFlagsInput(TENANT, DIVE, scope),
        )

    assert catalog.calls == [
        ("flag", DIVE, False, None),
        ("flag", DIVE, False, [frame]),
        ("flag", DIVE, False, []),
    ]


class FakeLabelProjects:
    def __init__(self):
        self.calls = []

    async def ensure_dive_project(
        self, tenant_id, dive_id, kind, *, suffix, labeling_config_xml
    ):
        self.calls.append((tenant_id, dive_id, kind, suffix, labeling_config_xml))
        return 4242


async def test_create_finds_or_creates_the_dives_species_project():
    """Title `{name} #{number} - Species Labeling`, healed onto v1's XML."""
    projects = FakeLabelProjects()

    project_id = await ActivityEnvironment().run(
        _activities(
            FakeSpeciesCatalog(), label_projects=projects
        ).create_species_label_studio_project,
        SpeciesTarget(TENANT, DIVE),
    )

    assert project_id == 4242
    assert projects.calls == [
        (TENANT, DIVE, "species", "Species Labeling", SPECIES_LABELING_CONFIG_XML)
    ]

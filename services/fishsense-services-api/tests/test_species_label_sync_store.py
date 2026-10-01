"""The species half of the Label Studio label sync's database side.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_sync_species_labels_activity.py (`_apply_parsed`'s tests) and
services/fishsense-api/tests/test_species_slate_reads_superseded.py (the
project ids). v1's semantics, kept:

* the projects are the distinct Label Studio projects of live species labels;
* a label is found by its task; a task with no label is skipped;
* a field the annotation doesn't give (parsed as None) keeps its stored value;
* columns the XML no longer has, and the operator's `fish_angle_degrees`, are
  never written by the sync.

v2 change, pinned here: **the sync writes only the columns it owns.** v1's
fetch-mutate-PUT carried `needs_reprocess` in its body, so a flag raised
mid-sync was silently lowered (v1's own docstring says so and leaves it).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.ingest_store import create_dive, register_capture
from fishsense_services_api.label_sync_store import (
    LabelSyncCatalog,
    SpeciesSync,
    apply_species_sync,
    label_studio_projects,
)

T0 = datetime(2026, 5, 2, 10, tzinfo=UTC)
ORCHESTRATOR = "service:fishsense-orchestrator"


async def _tenant(owner_engine, slug="lab") -> uuid.UUID:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text("INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id"),
                {"s": slug},
            )
        ).scalar_one()


async def _capture(app_engine, tenant) -> tuple[uuid.UUID, uuid.UUID]:
    async with tenant_transaction(app_engine, tenant) as conn:
        dive = await create_dive(
            conn, tenant, source_path=f"d-{uuid.uuid4()}", name="d", dived_at=T0
        )
        capture = (
            await register_capture(
                conn, tenant, dive_id=dive, device_id=None,
                source_path=f"{dive}/P.ORF", captured_at=T0,
                checksum=uuid.uuid4().hex,
            )  # fmt: skip
        ).capture_id
    return dive, capture


async def _species(owner_engine, tenant, capture, *, project, task, **columns):
    names = ", ".join(columns)
    values = ", ".join(f":{c}" for c in columns)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO species_labels (tenant_id, capture_id, source, "
                f"ls_project_id, ls_task_id{', ' + names if names else ''}) "
                f"VALUES (:t, :c, 'human', :p, :k{', ' + values if values else ''})"
            ),
            {"t": tenant, "c": capture, "p": project, "k": task, **columns},
        )


async def _label(owner_engine, task):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT * FROM species_labels WHERE ls_task_id = :k"),
                {"k": task},
            )
        ).one()


def _sync(**overrides) -> SpeciesSync:
    values = {
        "completed": True,
        "grouping": None,
        "top_three_photos_of_group": None,
        "content_of_image": None,
        "fish_measurable_category": None,
        "fish_angle_category": None,
        "fish_curved_category": None,
        "ls_labeler_id": 7,
        "ls_updated_at": T0,
        "ls_payload": {"id": 101},
    }
    values.update(overrides)
    return SpeciesSync(**values)


async def _apply(app_engine, tenant, task, sync):
    async with tenant_transaction(app_engine, tenant) as conn:
        return await apply_species_sync(conn, tenant, task, sync)


async def test_species_project_ids_exclude_superseded_only(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    _, a = await _capture(app_engine, lab)
    _, b = await _capture(app_engine, lab)
    _, c = await _capture(app_engine, lab)
    await _species(owner_engine, lab, a, project=70, task=1)
    await _species(owner_engine, lab, b, project=117, task=2, superseded=True)
    await _species(owner_engine, lab, c, project=None, task=None)

    async with tenant_transaction(app_engine, lab) as conn:
        assert await label_studio_projects(conn, lab, "species") == [70]


async def test_the_sync_writes_every_field_the_annotation_gives(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive, capture = await _capture(app_engine, lab)
    await _species(owner_engine, lab, capture, project=70, task=101)

    applied = await _apply(
        app_engine, lab, 101,
        _sync(grouping="Part of previous group", top_three_photos_of_group=True,
              content_of_image="Slate, Laser on slate",
              fish_measurable_category="no", fish_angle_category="x < 5°",
              fish_curved_category="No Curve"),
    )  # fmt: skip

    assert (applied.capture_id, applied.dive_id) == (capture, dive)
    row = await _label(owner_engine, 101)
    assert row.completed is True
    assert row.grouping == "Part of previous group"
    assert row.top_three_photos_of_group is True
    assert row.content_of_image == "Slate, Laser on slate"
    assert (row.fish_measurable_category, row.fish_angle_category,
            row.fish_curved_category) == ("no", "x < 5°", "No Curve")  # fmt: skip
    assert (row.ls_labeler_id, row.ls_updated_at) == (7, T0)
    assert row.ls_payload == {"id": 101}


async def test_apply_parsed_only_overwrites_specified_fields(owner_engine, app_engine):
    """A re-sync whose annotation drops a section doesn't clobber the stored
    value (v1's `_apply_parsed`)."""
    lab = await _tenant(owner_engine)
    _, capture = await _capture(app_engine, lab)
    await _species(
        owner_engine, lab, capture, project=70, task=101,
        grouping="Part of previous group", content_of_image="Fish, Hogfish",
        top_three_photos_of_group=True, ls_labeler_id=3,
    )  # fmt: skip

    await _apply(
        app_engine, lab, 101, _sync(fish_angle_category="Top", ls_labeler_id=None)
    )

    row = await _label(owner_engine, 101)
    assert row.fish_angle_category == "Top"
    assert row.grouping == "Part of previous group"
    assert row.content_of_image == "Fish, Hogfish"
    assert row.top_three_photos_of_group is True
    assert row.ls_labeler_id == 3


async def test_a_negative_top_three_answer_is_written(owner_engine, app_engine):
    """False is an answer, not an absence: only None keeps the stored value."""
    lab = await _tenant(owner_engine)
    _, capture = await _capture(app_engine, lab)
    await _species(owner_engine, lab, capture, project=70, task=101,
                   top_three_photos_of_group=True)  # fmt: skip

    await _apply(app_engine, lab, 101, _sync(top_three_photos_of_group=False))

    assert (await _label(owner_engine, 101)).top_three_photos_of_group is False


async def test_the_sync_writes_only_the_columns_it_owns(owner_engine, app_engine):
    """v2 change. A flag raised between v1's read and its PUT was lowered by
    the PUT; the operator's angle is entered by hand and no XML control
    produces it."""
    lab = await _tenant(owner_engine)
    _, capture = await _capture(app_engine, lab)
    await _species(owner_engine, lab, capture, project=70, task=101,
                   needs_reprocess=True, fish_angle_degrees=12.5,
                   image_url="s3://b/k.JPG")  # fmt: skip

    await _apply(app_engine, lab, 101, _sync(completed=False))

    row = await _label(owner_engine, 101)
    assert row.needs_reprocess is True
    assert row.superseded is False
    assert row.fish_angle_degrees == 12.5
    assert row.image_url == "s3://b/k.JPG"
    assert row.completed is False


async def test_a_task_with_no_label_is_skipped(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    assert await _apply(app_engine, lab, 999, _sync()) is None


async def test_a_superseded_row_is_not_found_by_its_task(owner_engine, app_engine):
    """v1 looked a task's row up among live rows only (404 otherwise), so a
    dead-lettered row is never revived by the sync."""
    lab = await _tenant(owner_engine)
    _, capture = await _capture(app_engine, lab)
    await _species(owner_engine, lab, capture, project=70, task=101, superseded=True)

    assert await _apply(app_engine, lab, 101, _sync()) is None
    assert (await _label(owner_engine, 101)).completed is False


async def test_the_catalog_applies_only_in_tenants_it_serves(
    owner_engine, app_engine, seed_memberships
):
    tenants = await seed_memberships({ORCHESTRATOR: {"lab": "member"}})
    lab = tenants["lab"]
    _, capture = await _capture(app_engine, lab)
    await _species(owner_engine, lab, capture, project=70, task=101)
    catalog = LabelSyncCatalog(app_engine, sub=ORCHESTRATOR)

    applied = await catalog.apply_species_sync(lab, 101, _sync())

    assert applied.capture_id == capture
    assert await catalog.label_studio_projects(lab, "species") == [70]

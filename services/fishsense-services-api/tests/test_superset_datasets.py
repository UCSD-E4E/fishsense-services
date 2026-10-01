"""Superset's pipeline datasets, run against `dive_pipeline_status` on Postgres.

Ported from fishsense-lite@77e8f8e5 deploy/incus/superset_volumes/docker/
assets/datasets/FishSense/pipeline_labeling_queue.yaml and
pipeline_partial_dives.yaml: their `sql`, kept verbatim in
deploy/superset/datasets/*.sql. v1 never ran them in a test -- an import
failure only WARNed (docker-init.sh:121) and a renamed column or a re-cased
enum returned zero rows silently. Here each runs, as the app role under RLS,
over a seeded corpus, and must return what the dashboard should show:

* `priority = 'HIGH'` must still match (v2 stores `high`; the view
  upper-cases it);
* a low-priority dive and another tenant's dive never appear.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    calibrate,
    cluster,
    forget_identities,
)
from fishsense_services_api.db import tenant_transaction
from test_dive_pipeline_status_view import (
    SLATE_MARKER,
    _capture,
    _dive,
    _head_tail,
    _laser,
    _slate_label,
    _slate_template,
    _species,
    _tenant,
    _valid_laser,
)

DATASETS = Path(__file__).resolve().parents[3] / "deploy" / "superset" / "datasets"


async def _run(app_engine, tenant, dataset: str) -> list[tuple]:
    sql = (DATASETS / f"{dataset}.sql").read_text()
    async with tenant_transaction(app_engine, tenant) as conn:
        return [tuple(r) for r in await conn.execute(text(sql))]


def test_the_datasets_are_the_ones_superset_imports():
    assert sorted(p.name for p in DATASETS.glob("*.sql")) == [
        "pipeline_labeling_queue.sql",
        "pipeline_partial_dives.sql",
    ]


# --- seeding: a dive at each place the dashboard distinguishes --------------------


async def _laser_open(owner_engine, tenant, **kwargs):
    """Laser tasks out, not all labeled."""
    dive = await _dive(owner_engine, tenant, **kwargs)
    await _laser(owner_engine, tenant, await _capture(owner_engine, tenant, dive))
    return dive


async def _headtail_open(owner_engine, tenant):
    """Lasers done; head/tail tasks out, not all labeled."""
    dive = await _dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, dive)
    await _valid_laser(owner_engine, tenant, capture_id)
    await _head_tail(owner_engine, tenant, capture_id)
    return dive


async def _species_open(owner_engine, tenant):
    """Lasers done, clustered; species tasks out, not all labeled."""
    dive = await _dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, dive)
    await _valid_laser(owner_engine, tenant, capture_id)
    await cluster(owner_engine, tenant, dive.id, [capture_id], formed_by="prediction")
    await _species(owner_engine, tenant, capture_id)
    return dive


async def _slate_open(owner_engine, tenant):
    """A marked slate frame with its slate task out, not labeled."""
    dive = await _dive(
        owner_engine, tenant, slate_template_id=await _slate_template(owner_engine)
    )
    capture_id = await _capture(owner_engine, tenant, dive)
    await _species(owner_engine, tenant, capture_id, SLATE_MARKER, completed=True)
    await _slate_label(owner_engine, tenant, capture_id)
    return dive


async def _calibrated(owner_engine, tenant, *, lasers_done=False):
    """Calibrated, nothing measured."""
    dive = await _dive(owner_engine, tenant)
    await calibrate(owner_engine, tenant, dive.id)
    if lasers_done:
        await _valid_laser(
            owner_engine, tenant, await _capture(owner_engine, tenant, dive)
        )
    return dive


async def _labeled(owner_engine, tenant, *, species, headtail, slate=False):
    """Lasers done; species and head/tail labeling done or not."""
    template = await _slate_template(owner_engine) if slate else None
    dive = await _dive(owner_engine, tenant, slate_template_id=template)
    capture_id = await _capture(owner_engine, tenant, dive)
    await _valid_laser(owner_engine, tenant, capture_id)
    await _species(owner_engine, tenant, capture_id, completed=species)
    await _head_tail(owner_engine, tenant, capture_id, completed=headtail)
    return dive


# --- pipeline_labeling_queue ------------------------------------------------------


async def test_labeling_queue_counts_high_priority_dives_per_stage(
    owner_engine, app_engine
):
    """Distinct counts per stage, so a stage reading another's column shows."""
    tenant = await _tenant(owner_engine)
    for _ in range(2):
        await _laser_open(owner_engine, tenant)
    await _species_open(owner_engine, tenant)
    for _ in range(3):
        await _headtail_open(owner_engine, tenant)
    await _slate_open(owner_engine, tenant)
    for _ in range(4):
        await _calibrated(owner_engine, tenant)
    # Neither a low-priority dive nor another tenant's is in anyone's queue.
    await _laser_open(owner_engine, tenant, priority="low")
    partner = await _tenant(owner_engine, "partner")
    await _laser_open(owner_engine, partner)

    assert await _run(app_engine, tenant, "pipeline_labeling_queue") == [
        ("1 laser", 2),
        ("2 species", 1),
        ("3 headtail", 3),
        ("4 slate", 1),
        ("5 measure", 4),
    ]


async def test_labeling_queue_is_all_zero_for_an_idle_tenant(owner_engine, app_engine):
    tenant = await _tenant(owner_engine)
    await _dive(owner_engine, tenant)

    rows = await _run(app_engine, tenant, "pipeline_labeling_queue")
    assert [count for _, count in rows] == [0, 0, 0, 0, 0]


# --- pipeline_partial_dives -------------------------------------------------------


async def test_partial_dives_names_each_unfinished_dives_blocker(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    expected = {
        (await _laser_open(owner_engine, tenant)).number: "needs laser",
        (await _calibrated(owner_engine, tenant, lasers_done=True)).number: (
            "ready to measure"
        ),
        (
            await _labeled(owner_engine, tenant, species=True, headtail=True)
        ).number: "blocked: no slate",
        (
            await _labeled(owner_engine, tenant, species=False, headtail=False)
        ).number: "needs species + headtail",
        (
            await _labeled(owner_engine, tenant, species=False, headtail=True)
        ).number: "needs species",
        (
            await _labeled(owner_engine, tenant, species=True, headtail=False)
        ).number: "needs headtail",
        (
            await _labeled(
                owner_engine, tenant, species=True, headtail=True, slate=True
            )
        ).number: "other",
    }
    await _laser_open(owner_engine, tenant, priority="low")
    partner = await _tenant(owner_engine, "partner")
    await _laser_open(owner_engine, partner)

    rows = await _run(app_engine, tenant, "pipeline_partial_dives")
    assert [(r[0], r[-1]) for r in rows] == sorted(expected.items())
    assert {r[1] for r in rows} == {"HIGH"}

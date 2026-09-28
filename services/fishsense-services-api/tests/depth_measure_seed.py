"""Seeding helpers for the laser-depth and stage-14 store tests.

Ported in spirit from fishsense-lite@77e8f8e5 services/fishsense-api/
tests_support/stage14_fixtures.py (`measurable_image`,
`fish_model_measurable_image`, `calibration`, `measurement`): one definition
of what "a measurable capture" seeds, shared by the cohort and persistence
tests, so a change to it cannot silently apply to only one of them.

Constructors, not fixtures. Everything is written as the schema owner (which
bypasses RLS), the way an admin or the migration would; the stores under test
then read and write as the app role, under RLS.
"""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

#: A real fish's `Common (Scientific)` leaf -- measurable.
REAL_FISH = "Fish, Hogfish (Lachnolaimus maximus)"
#: A pinhole camera matrix (v1's test intrinsics).
K = [[3000.0, 0.0, 2048.0], [0.0, 3000.0, 1536.0], [0.0, 0.0, 1.0]]
#: 10 cm, where every sound calibration in v1's fleet sits (0.097-0.145 m).
PLAUSIBLE_POSITION = [0.1, 0.0, 0.0]
#: 2.35 cm -- dive 522's real fitted baseline, the worst of v1's eight.
IMPLAUSIBLE_POSITION = [0.0141, 0.0188, 0.0]
PROVENANCE = {
    "algorithm": "laser_depth_fronto_parallel",
    "algorithm_version": "1",
    "core_version": "4.1.0",
}
T0 = datetime(2025, 3, 6, 17, 0, 15, tzinfo=UTC)

_counter = itertools.count(1)


@pytest.fixture(autouse=True)
async def forget_identities(owner_engine):
    """Remove the species and fish models a test created.

    They are global reference data, so the shared cleanup (which truncates
    tenants) leaves them, and a later test inserting the same name as the
    migration would fails on its unique key. Import this into a test module
    to use it there."""
    async with owner_engine.connect() as conn:
        species = list((await conn.execute(text("SELECT id FROM species"))).scalars())
        models = list(
            (await conn.execute(text("SELECT id FROM fish_models"))).scalars()
        )
    yield
    async with owner_engine.begin() as conn:
        # Fish (tenant data) reference them; the shared cleanup would remove
        # them after this teardown, so remove them first.
        await conn.execute(text("TRUNCATE tenants, users CASCADE"))
        await conn.execute(
            text("DELETE FROM species WHERE NOT (id = ANY(:keep))"), {"keep": species}
        )
        await conn.execute(
            text(
                "DELETE FROM fish_models WHERE NOT (id = ANY(:keep)) "
                "AND name NOT IN (SELECT name FROM fish_model_references)"
            ),
            {"keep": models},
        )


def _n() -> int:
    return next(_counter)


async def _one(owner_engine, sql: str, **params):
    async with owner_engine.begin() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def exec_(owner_engine, sql: str, **params) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(text(sql), params)


async def tenant(owner_engine, slug: str = "lab") -> uuid.UUID:
    return await _one(
        owner_engine,
        "INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id",
        s=slug,
    )


async def device(owner_engine, tenant_id, *, camera_matrix=None, model="pinhole"):
    """A Lite device and its current camera calibration."""
    device_id = await _one(
        owner_engine,
        "INSERT INTO devices (tenant_id, kind, serial) VALUES (:t, 'lite', :s) "
        "RETURNING id",
        t=tenant_id,
        s=f"BHW{_n()}",
    )
    if camera_matrix is not False:
        await camera_calibration(
            owner_engine, tenant_id, device_id, camera_matrix or K, model=model
        )
    return device_id


async def camera_calibration(owner_engine, tenant_id, device_id, matrix, *, model):
    return await _one(
        owner_engine,
        "INSERT INTO camera_calibrations (tenant_id, device_id, camera_model, "
        "port_model, camera_matrix, distortion_coefficients) "
        "VALUES (:t, :d, :m, :p, :k, '[0, 0, 0, 0, 0]') RETURNING id",
        t=tenant_id,
        d=device_id,
        m=model,
        p=None if model == "pinhole" else "flat",
        k=json.dumps(matrix),
    )


async def dive(
    owner_engine,
    tenant_id,
    *,
    device_id=None,
    priority="high",
    source_dive=None,
    created_at=None,
):
    n = _n()
    return await _one(
        owner_engine,
        "INSERT INTO dives (tenant_id, source_path, name, dived_at, priority, "
        "device_id, calibration_source_dive_id, created_at) VALUES (:t, :p, :p, "
        ":at, :priority, :device, :src, :created) RETURNING id",
        t=tenant_id,
        p=f"/dives/{n}",
        at=T0,
        priority=priority,
        device=device_id,
        src=source_dive,
        created=created_at or T0 + timedelta(seconds=n),
    )


async def calibrate(
    owner_engine,
    tenant_id,
    dive_id,
    *,
    position=PLAUSIBLE_POSITION,
    axis=(0.0, 0.0, 1.0),
    outcome="accepted",
):
    """Append a laser calibration to the dive (current = the latest)."""
    return await _one(
        owner_engine,
        "INSERT INTO laser_calibrations (tenant_id, dive_id, producer, outcome, "
        "laser_position, laser_axis, refusal_reason) VALUES (:t, :d, 'slate', :o, "
        ":p, :a, :r) RETURNING id",
        t=tenant_id,
        d=dive_id,
        o=outcome,
        p=None if outcome == "refused" else json.dumps(list(position)),
        a=None if outcome == "refused" else json.dumps(list(axis)),
        r="refused in a test" if outcome == "refused" else None,
    )


async def capture(owner_engine, tenant_id, dive_id, *, canonical=True):
    n = _n()
    return await _one(
        owner_engine,
        "INSERT INTO captures (tenant_id, dive_id, source_path, captured_at, "
        "checksum, is_canonical) VALUES (:t, :d, :p, :at, :sum, :canonical) "
        "RETURNING id",
        t=tenant_id,
        d=dive_id,
        p=f"/captures/{n}.ORF",
        at=T0 + timedelta(seconds=n),
        sum=f"{n:032x}",
        canonical=canonical,
    )


async def laser_label(
    owner_engine,
    tenant_id,
    capture_id,
    *,
    x=100.0,
    y=200.0,
    completed=True,
    superseded=False,
    number=None,
):
    return await _one(
        owner_engine,
        "INSERT INTO laser_labels (tenant_id, capture_id, source, ls_project_id, "
        "completed, superseded, x, y, number) VALUES (:t, :c, 'human', :p, :done, "
        ":gone, :x, :y, :number) RETURNING id",
        t=tenant_id,
        c=capture_id,
        p=_n(),
        done=completed,
        gone=superseded,
        x=x,
        y=y,
        number=number,
    )


async def head_tail_label(
    owner_engine,
    tenant_id,
    capture_id,
    *,
    head=(1.0, 2.0),
    tail=(3.0, 4.0),
    completed=True,
    superseded=False,
):
    return await _one(
        owner_engine,
        "INSERT INTO head_tail_labels (tenant_id, capture_id, source, "
        "ls_project_id, completed, superseded, head_x, head_y, tail_x, tail_y) "
        "VALUES (:t, :c, 'human', :p, :done, :gone, :hx, :hy, :tx, :ty) "
        "RETURNING id",
        t=tenant_id,
        c=capture_id,
        p=_n(),
        done=completed,
        gone=superseded,
        hx=head[0],
        hy=head[1],
        tx=tail[0],
        ty=tail[1],
    )


async def species_label(
    owner_engine,
    tenant_id,
    capture_id,
    content=REAL_FISH,
    *,
    top_three=True,
    superseded=False,
    sentinel=False,
):
    return await _one(
        owner_engine,
        "INSERT INTO species_labels (tenant_id, capture_id, source, "
        "ls_project_id, completed, superseded, top_three_photos_of_group, "
        "content_of_image) VALUES (:t, :c, :source, :p, true, :gone, :top, "
        ":content) RETURNING id",
        t=tenant_id,
        c=capture_id,
        source="import" if sentinel else "human",
        p=None if sentinel else _n(),
        gone=superseded,
        top=top_three,
        content=content,
    )


async def cluster(
    owner_engine,
    tenant_id,
    dive_id,
    captures=(),
    *,
    fish_id=None,
    formed_by="label_studio",
):
    cluster_id = await _one(
        owner_engine,
        "INSERT INTO dive_frame_clusters (tenant_id, dive_id, formed_by, fish_id) "
        "VALUES (:t, :d, :f, :fish) RETURNING id",
        t=tenant_id,
        d=dive_id,
        f=formed_by,
        fish=fish_id,
    )
    for capture_id in captures:
        await exec_(
            owner_engine,
            "INSERT INTO dive_frame_cluster_captures (tenant_id, cluster_id, "
            "capture_id) VALUES (:t, :k, :c)",
            t=tenant_id,
            k=cluster_id,
            c=capture_id,
        )
    return cluster_id


async def fish(owner_engine, tenant_id, *, model=None, species=None):
    """A fish: a model's (by name, registered if new) or a real one."""
    model_id = None
    if model is not None:
        await exec_(
            owner_engine,
            "INSERT INTO fish_models (name) VALUES (:n) ON CONFLICT DO NOTHING",
            n=model,
        )
        model_id = await _one(
            owner_engine, "SELECT id FROM fish_models WHERE name = :n", n=model
        )
    return await _one(
        owner_engine,
        "INSERT INTO fish (tenant_id, fish_model_id, species_id) "
        "VALUES (:t, :m, :s) RETURNING id",
        t=tenant_id,
        m=model_id,
        s=species,
    )


async def measurement(
    owner_engine,
    tenant_id,
    capture_id,
    fish_id,
    calibration_id,
    *,
    length_m=0.3,
    laser_label_id=None,
    head_tail_label_id=None,
    v1_id=None,
):
    """A server measurement. Without a v1 id it names its provenance, as
    0013 requires of anything v2 writes."""
    provenance = PROVENANCE if v1_id is None else dict.fromkeys(PROVENANCE)
    return await _one(
        owner_engine,
        "INSERT INTO measurements (tenant_id, capture_id, fish_id, source, "
        "length_m, laser_calibration_id, laser_label_id, head_tail_label_id, "
        "v1_id, algorithm, algorithm_version, core_version) VALUES (:t, :c, :f, "
        "'server', :len, :cal, :ll, :ht, :v1, :alg, :ver, :core) RETURNING id",
        t=tenant_id,
        c=capture_id,
        f=fish_id,
        len=length_m,
        cal=calibration_id,
        ll=laser_label_id,
        ht=head_tail_label_id,
        v1=v1_id,
        alg=provenance["algorithm"],
        ver=provenance["algorithm_version"],
        core=provenance["core_version"],
    )


async def depth(
    owner_engine,
    tenant_id,
    capture_id,
    laser_label_id,
    calibration_id,
    *,
    depth_m=2.0,
):
    return await _one(
        owner_engine,
        "INSERT INTO laser_depths (tenant_id, capture_id, laser_label_id, "
        "laser_calibration_id, depth_m, range_m, residual_m, core_version) "
        "VALUES (:t, :c, :l, :cal, :d, :d, 0, '4.1.0') RETURNING id",
        t=tenant_id,
        c=capture_id,
        l=laser_label_id,
        cal=calibration_id,
        d=depth_m,
    )


async def calibrated_dive(owner_engine, tenant_id, **dive_kwargs):
    """A high-priority dive on a calibrated device with its own accepted,
    plausible laser calibration. Returns (dive, laser calibration)."""
    device_id = await device(owner_engine, tenant_id)
    dive_id = await dive(owner_engine, tenant_id, device_id=device_id, **dive_kwargs)
    return dive_id, await calibrate(owner_engine, tenant_id, dive_id)


async def measurable_capture(
    owner_engine,
    tenant_id,
    dive_id,
    content=REAL_FISH,
    *,
    in_cluster=True,
    cluster_fish=None,
):
    """A capture stage 14 would attempt: a top-three species label, a valid
    laser, a valid head/tail -- and, for a real fish, a Label Studio cluster
    (v1's `measurable_image`; a model needs none, `fish_model_measurable_image`).
    Returns the capture id."""
    capture_id = await capture(owner_engine, tenant_id, dive_id)
    await laser_label(owner_engine, tenant_id, capture_id)
    await head_tail_label(owner_engine, tenant_id, capture_id)
    await species_label(owner_engine, tenant_id, capture_id, content)
    if in_cluster:
        await cluster(
            owner_engine, tenant_id, dive_id, [capture_id], fish_id=cluster_fish
        )
    return capture_id

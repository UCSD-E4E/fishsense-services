"""Seeding for the slate and calibration stores' tests, as the schema owner.

The v1 suites these port seeded SQLite through SQLModel objects; v2's stores
run raw SQL under RLS, so they are tested on real Postgres and seeded here the
way an admin would, as the owner. Reference rows (slate templates, calibration
targets) are global and are not truncated between tests, so their names are
made unique.
"""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

T0 = datetime(2026, 9, 1, tzinfo=UTC)
K = [[2855.29, 0.0, 2031.79], [0.0, 2881.07, 1447.69], [0.0, 0.0, 1.0]]
DISTORTION = [0.1, -0.2, 0.0, 0.0, 0.05]
TEMPLATE_POINTS = [[0.0, 0.0], [2400.0, 0.0], [0.0, 3000.0], [2400.0, 3000.0]]
#: 10.4 cm -- where every sound calibration in the fleet sits.
GOOD_POSITION = [0.0624, 0.0832, 0.0]
#: 2.35 cm -- dive 522's real fitted baseline, the worst of the eight.
BAD_POSITION = [0.0141, 0.0188, 0.0]
AXIS = [0.0, 0.0, 1.0]
SLATE_MARKER = "Slate, Laser on slate"

_tasks = itertools.count(1)


def next_task() -> int:
    """A Label Studio task id no other label in the session holds."""
    return next(_tasks)


async def _one(engine, sql: str, **params):
    async with engine.begin() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def run(engine, sql: str, **params) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(sql), params)


async def tenant(engine, slug: str = "lab") -> uuid.UUID:
    return await _one(
        engine, "INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id", s=slug
    )


async def device_with_camera(
    engine, tenant_id, *, matrix=K, distortion=DISTORTION
) -> tuple[uuid.UUID, uuid.UUID]:
    device = await _one(
        engine,
        "INSERT INTO devices (tenant_id, kind, serial) VALUES (:t, 'lite', :s) "
        "RETURNING id",
        t=tenant_id,
        s=f"TG6-{uuid.uuid4()}",
    )
    calibration = await _one(
        engine,
        "INSERT INTO camera_calibrations (tenant_id, device_id, camera_matrix, "
        "distortion_coefficients) VALUES (:t, :d, :m, :k) RETURNING id",
        t=tenant_id,
        d=device,
        m=json.dumps(matrix),
        k=json.dumps(distortion),
    )
    return device, calibration


async def slate_template(
    engine,
    *,
    dpi: int | None = 300,
    reference_points=TEMPLATE_POINTS,
    source_path: str | None = "slates/H-Slate.pdf",
) -> uuid.UUID:
    return await _one(
        engine,
        "INSERT INTO slate_templates (name, dpi, source_path, reference_points) "
        "VALUES (:n, :dpi, :p, :r) RETURNING id",
        n=f"H-Slate {uuid.uuid4()}",
        dpi=dpi,
        p=source_path,
        r=json.dumps(reference_points),
    )


async def calibration_target(
    engine,
    *,
    name: str | None = None,
    rows: int = 10,
    cols: int = 14,
    pitch_x_m: float = 0.042,
    pitch_y_m: float = 0.042,
    valid_from: datetime = T0,
) -> uuid.UUID:
    return await _one(
        engine,
        "INSERT INTO calibration_targets (name, interior_rows, interior_cols, "
        "pitch_x_m, pitch_y_m, valid_from) VALUES (:n, :r, :c, :px, :py, :v) "
        "RETURNING id",
        n=name or f"E4E Checkerboard {uuid.uuid4()}",
        r=rows,
        c=cols,
        px=pitch_x_m,
        py=pitch_y_m,
        v=valid_from,
    )


async def dive(
    engine,
    tenant_id,
    *,
    priority: str = "high",
    slate=None,
    target=None,
    device=None,
    source=None,
    created_at: datetime | None = None,
    name: str | None = None,
    v1_id: int | None = None,
) -> uuid.UUID:
    return await _one(
        engine,
        "INSERT INTO dives (tenant_id, source_path, name, dived_at, priority, "
        "slate_template_id, calibration_target_id, device_id, "
        "calibration_source_dive_id, created_at, v1_id) VALUES (:t, :p, :n, :at, "
        ":pr, :s, :g, :dev, :src, coalesce(:c, now()), :v1) RETURNING id",
        t=tenant_id,
        p=f"/dives/{uuid.uuid4()}",
        n=name,
        at=T0,
        pr=priority,
        s=slate,
        g=target,
        dev=device,
        src=source,
        c=created_at,
        v1=v1_id,
    )


async def capture(
    engine,
    tenant_id,
    dive_id,
    *,
    canonical: bool = True,
    checksum: str | None = None,
    v1_id: int | None = None,
    captured_at: datetime = T0,
) -> uuid.UUID:
    return await _one(
        engine,
        "INSERT INTO captures (tenant_id, dive_id, source_path, captured_at, "
        "checksum, is_canonical, v1_id) VALUES (:t, :d, :p, :at, :k, :c, :v1) "
        "RETURNING id",
        t=tenant_id,
        d=dive_id,
        p=f"/frames/{uuid.uuid4()}.ORF",
        at=captured_at,
        k=checksum or uuid.uuid4().hex,
        c=canonical,
        v1=v1_id,
    )


async def species(
    engine,
    tenant_id,
    capture_id,
    *,
    content: str | None = SLATE_MARKER,
    superseded: bool = False,
    project: int | None = 70,
) -> uuid.UUID:
    return await _one(
        engine,
        "INSERT INTO species_labels (tenant_id, capture_id, source, ls_project_id, "
        "ls_task_id, completed, superseded, content_of_image) VALUES (:t, :c, "
        "'human', :p, :k, true, :s, :content) RETURNING id",
        t=tenant_id,
        c=capture_id,
        p=project,
        k=next_task() if project is not None else None,
        s=superseded,
        content=content,
    )


async def slate_label(
    engine,
    tenant_id,
    capture_id,
    *,
    project: int | None = 66,
    task: int | None = None,
    completed: bool = False,
    superseded: bool = False,
    needs_reprocess: bool = False,
    reference_points=None,
    skipped_points=None,
    ls_updated_at: datetime | None = None,
    source: str = "human",
) -> uuid.UUID:
    return await _one(
        engine,
        "INSERT INTO slate_labels (tenant_id, capture_id, source, ls_project_id, "
        "ls_task_id, completed, superseded, needs_reprocess, reference_points, "
        "skipped_points, ls_updated_at) VALUES (:t, :c, :src, :p, :k, :done, :gone, "
        ":flag, :refs, :skip, :at) RETURNING id",
        t=tenant_id,
        c=capture_id,
        src=source,
        p=project,
        k=(task or next_task()) if project is not None else None,
        done=completed,
        gone=superseded,
        flag=needs_reprocess,
        refs=None if reference_points is None else json.dumps(reference_points),
        skip=None if skipped_points is None else json.dumps(skipped_points),
        at=ls_updated_at,
    )


async def laser_label(
    engine,
    tenant_id,
    capture_id,
    *,
    x: float | None = 100.0,
    y: float | None = 200.0,
    superseded: bool = False,
    completed: bool = True,
    ls_updated_at: datetime | None = None,
    project: int | None = 73,
    v1_id: int | None = None,
) -> uuid.UUID:
    return await _one(
        engine,
        "INSERT INTO laser_labels (tenant_id, capture_id, source, ls_project_id, "
        "ls_task_id, completed, superseded, x, y, ls_updated_at, v1_id) VALUES "
        "(:t, :c, 'human', :p, :k, :done, :gone, :x, :y, :at, :v1) RETURNING id",
        t=tenant_id,
        c=capture_id,
        p=project,
        k=next_task() if project is not None else None,
        done=completed,
        gone=superseded,
        x=x,
        y=y,
        at=ls_updated_at,
        v1=v1_id,
    )


async def laser_calibration(
    engine,
    tenant_id,
    dive_id,
    *,
    outcome: str = "accepted",
    position=GOOD_POSITION,
    producer: str | None = "slate",
    refusal_reason: str | None = None,
    inputs_as_of: datetime | None = None,
    slate_template_id=None,
    calibration_target_id=None,
    v1_refusal_dive_id: int | None = None,
) -> uuid.UUID:
    accepted = outcome == "accepted"
    return await _one(
        engine,
        "INSERT INTO laser_calibrations (tenant_id, dive_id, producer, outcome, "
        "laser_position, laser_axis, refusal_reason, inputs_as_of, "
        "slate_template_id, calibration_target_id, v1_refusal_dive_id) VALUES "
        "(:t, :d, :p, :o, :pos, :axis, :r, :at, :s, :g, :v1) RETURNING id",
        t=tenant_id,
        d=dive_id,
        p=producer,
        o=outcome,
        pos=json.dumps(position) if accepted else None,
        axis=json.dumps(AXIS) if accepted else None,
        r=None if accepted else (refusal_reason or "implausible baseline"),
        at=inputs_as_of,
        s=slate_template_id,
        g=calibration_target_id,
        v1=v1_refusal_dive_id,
    )


def later(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)

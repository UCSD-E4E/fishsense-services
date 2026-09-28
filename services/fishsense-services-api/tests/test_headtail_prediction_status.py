"""Every status v1's head/tail predictor emits is one the table accepts.

v1 (fishsense-lite@77e8f8e5 services/fishsense-data-processing-workflow-worker/
src/fishsense_data_processing_workflow_worker/activities/predict_headtail_image.py
`predict_from_jpeg`) abstains with `decode_failed` when the stage-5.1 JPEG
cannot be decoded. Migration 0011's CHECK omitted it, so the first undecodable
JPEG would have failed the insert -- and an abstention that cannot be written
is re-predicted every hour forever, because the cohort selects on the row's
absence (v1's persist_headtail_predictions_activity docstring). The rehearsal
passed only because production has none yet.

`skipped_no_upgrade_available` is *not* a status the table takes: it is a
statement about the worker, and the predict parent drops it before persisting.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

V1_STATUSES = [
    "predicted",
    "no_detections",
    "laser_off_all_fish",
    "headtail_failed",
    "decode_failed",
]


async def _capture(conn) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = (
        await conn.execute(
            text("INSERT INTO tenants (slug, name) VALUES ('lab', 'lab') RETURNING id")
        )
    ).scalar_one()
    capture = (
        await conn.execute(
            text(
                "INSERT INTO captures (tenant_id, source_path, captured_at, checksum) "
                "VALUES (:t, '/1.ORF', now(), '0123456789abcdef0123456789abcdef') "
                "RETURNING id"
            ),
            {"t": tenant},
        )
    ).scalar_one()
    return tenant, capture


async def _insert(conn, tenant, capture, status):
    points = status == "predicted"
    await conn.execute(
        text(
            "INSERT INTO head_tail_predictions (tenant_id, capture_id, "
            "predictor_version, status, head_x, head_y, tail_x, tail_y) "
            "VALUES (:t, :c, 2, :s, :h, :h, :h, :h)"
        ),
        {"t": tenant, "c": capture, "s": status, "h": 1.0 if points else None},
    )


@pytest.mark.parametrize("status", V1_STATUSES)
async def test_every_status_v1_emits_is_accepted(owner_engine, status):
    async with owner_engine.begin() as conn:
        tenant, capture = await _capture(conn)
        await _insert(conn, tenant, capture, status)


@pytest.mark.parametrize("status", ["skipped_no_upgrade_available", "nonsense"])
async def test_anything_else_is_refused(owner_engine, status):
    async with owner_engine.begin() as conn:
        tenant, capture = await _capture(conn)
        with pytest.raises(IntegrityError, match="head_tail_predictions_status_check"):
            await _insert(conn, tenant, capture, status)

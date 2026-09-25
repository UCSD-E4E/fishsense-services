"""End to end: stage 1 across the deployed stack.

Postgres, Temporal, the orchestrator and the processor, as compose runs them.
A tenant the orchestrator serves holds a high-priority dive whose canonical
captures carry valid laser labels; one run of the parent workflow selects it,
dispatches the processor's clustering, and persists prediction clusters --
through the processing contract, the shared queues and namespace, and the
orchestrator's membership, with nothing faked.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

pytestmark = pytest.mark.e2e

ORCHESTRATOR = "service:fishsense-orchestrator"
T0 = datetime(2024, 8, 21, 8, 0, tzinfo=UTC)


def _seed_a_dive_ready_for_clustering(stack) -> tuple[uuid.UUID, list[set]]:
    """Two bursts of three frames, ten minutes apart, all laser-labelled."""
    slug = stack.grant(ORCHESTRATOR)
    bursts: list[set] = [set(), set()]
    with stack.owner_engine.begin() as conn:
        tenant = conn.execute(
            text("SELECT id FROM tenants WHERE slug = :s"), {"s": slug}
        ).scalar_one()
        dive = conn.execute(
            text(
                "INSERT INTO dives (tenant_id, source_path, dived_at, priority) "
                "VALUES (:t, :p, :at, 'high') RETURNING id"
            ),
            {"t": tenant, "p": f"e2e/{slug}", "at": T0},
        ).scalar_one()
        for burst in range(2):
            for i in range(3):
                capture = conn.execute(
                    text(
                        "INSERT INTO captures (tenant_id, dive_id, source_path, "
                        "captured_at, checksum, is_canonical) "
                        "VALUES (:t, :d, :p, :at, :sum, true) RETURNING id"
                    ),
                    {"t": tenant, "d": dive, "p": f"e2e/{slug}/{burst}-{i}.ORF",
                     "at": T0 + timedelta(minutes=10 * burst, seconds=i),
                     "sum": uuid.uuid4().hex},
                ).scalar_one()  # fmt: skip
                conn.execute(
                    text(
                        "INSERT INTO laser_labels (tenant_id, capture_id, source, "
                        "ls_project_id, completed, x, y) "
                        "VALUES (:t, :c, 'human', 1, true, 10, 20)"
                    ),
                    {"t": tenant, "c": capture},
                )
                bursts[burst].add(capture)
    return dive, bursts


async def _run_stage_1(address: str) -> None:
    client = await Client.connect(
        address, namespace="fishsense", data_converter=pydantic_data_converter
    )
    await client.execute_workflow(
        "ClusterDiveFramesParentWorkflow",
        id=f"e2e-stage1-{uuid.uuid4()}",
        task_queue="fishsense_orchestrator",
        execution_timeout=timedelta(minutes=2),
    )


def test_stage_1_clusters_a_dive_across_the_stack(stack):
    dive, bursts = _seed_a_dive_ready_for_clustering(stack)

    asyncio.run(_run_stage_1(stack.temporal_address))

    with stack.owner_engine.connect() as conn:
        clusters = [
            set(row.captures)
            for row in conn.execute(
                text(
                    "SELECT array_agg(m.capture_id) AS captures "
                    "FROM dive_frame_clusters k JOIN dive_frame_cluster_captures m "
                    "ON m.cluster_id = k.id "
                    "WHERE k.dive_id = :d AND k.formed_by = 'prediction' "
                    "GROUP BY k.id"
                ),
                {"d": dive},
            )
        ]
    assert sorted(clusters, key=lambda c: min(map(str, c))) == sorted(
        bursts, key=lambda c: min(map(str, c))
    )

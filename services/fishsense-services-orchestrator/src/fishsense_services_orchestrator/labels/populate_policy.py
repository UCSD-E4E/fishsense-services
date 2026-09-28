"""The retry policies every per-dive Label Studio kind creates and populates
with (fishsense-lite@a8b2c3bc workflow-worker workflows/_populate.py:
`POPULATE_MAX_ATTEMPTS`, `_POPULATE_RETRY`).

Workflow-safe: nothing here touches I/O, so a workflow module may import it
outside `workflow.unsafe.imports_passed_through()`.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

__all__ = ["CREATE_PROJECT_RETRY", "POPULATE_MAX_ATTEMPTS", "POPULATE_RETRY"]

#: Bounded on purpose: unlimited retries let dive 424 reach attempt 10 and
#: leave 23 copies of three frames. The intervals matter as much as the cap:
#: from 30 s, doubling to 5 min, five attempts ride out an ordinary Label
#: Studio blip. A retry reconciles the import (`labels.populate.IMPORT_ISSUED`)
#: rather than re-importing.
POPULATE_MAX_ATTEMPTS = 5
POPULATE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=POPULATE_MAX_ATTEMPTS,
    non_retryable_error_types=["NotAMember", "ForeignRows"],
)

#: Creating (or finding) the project: Temporal's default retries, except that
#: a tenant the orchestrator no longer serves is final.
CREATE_PROJECT_RETRY = RetryPolicy(non_retryable_error_types=["NotAMember"])

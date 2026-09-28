"""Workflows for the object-store wiring test: a parent that stages and cleans
up the way every raw-reading parent will, and a child that stands in for a raw
reader until it is told to finish. Kept apart from the test module so the
workflow sandbox imports only what a real parent would."""

from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    # The pydantic converter loads these lazily; as in the clustering parent.
    import annotated_types  # noqa: F401  pylint: disable=unused-import
    import pydantic  # noqa: F401  pylint: disable=unused-import

    from fishsense_services_orchestrator.object_store.contracts import (
        CleanupRawBytesResult,
        StageRawBytesResult,
        StagingTarget,
    )
    from fishsense_services_orchestrator.object_store.steps import (
        cleanup_raw,
        stage_raw,
    )


@workflow.defn
class StageThenCleanUpWorkflow:
    @workflow.run
    async def run(
        self, target: StagingTarget
    ) -> tuple[StageRawBytesResult, CleanupRawBytesResult]:
        staged = await stage_raw(target)
        cleaned = await cleanup_raw(target)
        return staged, cleaned


@workflow.defn
class RawReaderStandInWorkflow:
    def __init__(self) -> None:
        self._done = False

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._done)

    @workflow.signal
    def finish(self) -> None:
        self._done = True

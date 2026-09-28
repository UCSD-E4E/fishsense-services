"""A heartbeat pump for activities that block in a thread.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-backup-worker/
src/fishsense_backup_worker/activities/_heartbeat.py.

The backup activities wrap a long blocking call (the `pg_dump` subprocess, the
NAS list and delete) in `asyncio.to_thread`, so the body can't heartbeat inline.
A background task heartbeats on a fixed cadence while the work runs. There is
no import marker to keep here (that is populate's), so a plain heartbeat is
right.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from temporalio import activity

__all__ = ["DEFAULT_HEARTBEAT_INTERVAL_SECONDS", "heartbeat_pump"]

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0


@asynccontextmanager
async def heartbeat_pump(
    interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> AsyncIterator[None]:
    """Heartbeat on a ticker for the duration of the block. The pump is
    cancelled on exit, can't outlive the block, and suppresses nothing raised
    inside it."""

    async def _pump() -> None:
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                activity.heartbeat()
        except asyncio.CancelledError:
            return

    pump = asyncio.create_task(_pump())
    try:
        yield
    finally:
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass

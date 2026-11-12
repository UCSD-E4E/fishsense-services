"""How long each API request takes, and how much of it is Postgres (0091).

The owner, 2026-11-12: "things are so slow ... it would be nice to log this
information for trying to improve the performance of the website". A web page
waits on several API requests in a row, so a slow page is mostly slow
requests, and a slow request is mostly its SQL or its waiting for something
else (the IdP's keys, the geocoder, an object store). This measures each:

* :class:`TimingMiddleware` times every request, from its arrival to its
  response's headers (the time to first byte the web waits on), and gives the
  response a ``Server-Timing`` header (``app``, and ``db`` with its statement
  count), which a browser's devtools show for a request made from a page;
* :func:`instrument` counts the statements a request runs and the time they
  take, through SQLAlchemy's cursor events, on the request's own
  :class:`RequestTiming` (a context variable: the statements run in
  SQLAlchemy's greenlets and in Starlette's threadpool, both of which carry the
  request's context);
* :class:`TimingRecorder` keeps each request's row under its route template
  and writes them to ``api_timings`` in batches, off the request's path. A
  write that fails loses its batch and is logged: a timing is never worth a
  failed request.

Health checks are not recorded: the platform asks every few seconds, and they
would outnumber every other route.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

#: Paths never recorded.
UNRECORDED = frozenset({"/healthz"})
#: A request that matched no route, under one name rather than its path.
UNMATCHED = "(unmatched)"


@dataclass
class RequestTiming:
    started: float = field(default_factory=time.perf_counter)
    db_seconds: float = 0.0
    db_queries: int = 0

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000

    @property
    def db_ms(self) -> float:
        return self.db_seconds * 1000


_current: contextvars.ContextVar[RequestTiming | None] = contextvars.ContextVar(
    "fishsense_request_timing", default=None
)
_STARTED = "_fishsense_statement_started"


def _before(conn, cursor, statement, parameters, context, executemany) -> None:
    conn.info.setdefault(_STARTED, []).append(time.perf_counter())


def _after(conn, cursor, statement, parameters, context, executemany) -> None:
    started = conn.info.get(_STARTED)
    if not started:
        return
    elapsed = time.perf_counter() - started.pop()
    timing = _current.get()
    if timing is not None:
        timing.db_seconds += elapsed
        timing.db_queries += 1


def instrument(engine: AsyncEngine) -> None:
    """Count the time each request's statements take; idempotent per engine."""
    sync = engine.sync_engine
    if not event.contains(sync, "before_cursor_execute", _before):
        event.listen(sync, "before_cursor_execute", _before)
        event.listen(sync, "after_cursor_execute", _after)


class TimingRecorder:
    """Requests' timings, written to ``api_timings`` a batch at a time.

    A batch is written once ``flush_rows`` rows wait, or the first time a row
    is recorded ``flush_seconds`` after the last write; and at shutdown.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        flush_rows: int = 100,
        flush_seconds: float = 10.0,
    ) -> None:
        self._engine = engine
        self._flush_rows = flush_rows
        self._flush_seconds = flush_seconds
        self._rows: list[dict] = []
        self._last_flush = time.monotonic()
        self._pending: set[asyncio.Task] = set()

    def record(
        self,
        *,
        method: str,
        route: str,
        status: int,
        total_ms: float,
        db_ms: float,
        db_queries: int,
    ) -> None:
        self._rows.append(
            {
                "method": method,
                "route": route,
                "status": status,
                "total_ms": round(total_ms, 3),
                "db_ms": round(db_ms, 3),
                "db_queries": db_queries,
            }
        )
        due = time.monotonic() - self._last_flush >= self._flush_seconds
        if len(self._rows) >= self._flush_rows or due:
            # Its own context: the write's statements are no request's.
            task = asyncio.get_running_loop().create_task(
                self.flush(), context=contextvars.Context()
            )
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def flush(self) -> None:
        rows, self._rows = self._rows, []
        self._last_flush = time.monotonic()
        if not rows:
            return
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    text("SELECT record_api_timings(CAST(:rows AS jsonb))"),
                    {"rows": json.dumps(rows)},
                )
        except Exception:
            log.warning("api timings: lost %d rows", len(rows), exc_info=True)

    async def wait(self) -> None:
        """Every write already started has finished."""
        if self._pending:
            await asyncio.gather(*self._pending)

    async def close(self) -> None:
        await self.wait()
        await self.flush()


def _route(scope: Scope) -> str:
    route = scope.get("route")
    return getattr(route, "path", None) or UNMATCHED


def _server_timing(timing: RequestTiming) -> bytes:
    return (
        f"app;dur={timing.elapsed_ms():.1f}, "
        f'db;dur={timing.db_ms:.1f};desc="{timing.db_queries} queries"'
    ).encode()


class TimingMiddleware:
    """Times every HTTP request; see the module docstring."""

    def __init__(self, app: ASGIApp, recorder: TimingRecorder) -> None:
        self.app = app
        self.recorder = recorder

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in UNRECORDED:
            await self.app(scope, receive, send)
            return

        timing = RequestTiming()
        token = _current.set(timing)
        status = 500
        # (ms, db ms, statements) when the headers went: the time to first byte.
        first_byte: tuple[float, float, int] | None = None

        async def timed_send(message: Message) -> None:
            nonlocal status, first_byte
            if message["type"] == "http.response.start":
                status = message["status"]
                first_byte = (timing.elapsed_ms(), timing.db_ms, timing.db_queries)
                headers = list(message.get("headers", []))
                headers.append((b"server-timing", _server_timing(timing)))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        finally:
            _current.reset(token)
            total_ms, db_ms, db_queries = first_byte or (
                timing.elapsed_ms(),
                timing.db_ms,
                timing.db_queries,
            )
            self.recorder.record(
                method=scope["method"],
                route=_route(scope),
                status=status,
                total_ms=total_ms,
                db_ms=db_ms,
                db_queries=db_queries,
            )

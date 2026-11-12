"""How long API requests and web pages take (the owner, 2026-11-12).

The owner: "things are so slow, can we add time to first byte measurements for
every page? ... it would be nice to log this information for trying to improve
the performance of the website". Nothing measured it: the API logged only
uvicorn's access lines, the web nothing, and Docker's logs reach back only to a
container's last restart, which every deploy is. So the timings are kept here,
where p50/p95 per route can be asked for over weeks.

* ``api_timings``: one row per API request (``timings.TimingMiddleware``): its
  method, its route template (``/tenants/{slug}/devices``, never the path, so
  ids don't split one route into thousands), its status, the milliseconds to
  its response's headers, and the milliseconds and statements it spent in
  Postgres.
* ``page_timings``: what visitors' browsers measured, posted by the web
  (``POST /telemetry/page-timings``): on a page load (``kind = 'load'``) time
  to first byte and the Core Web Vitals; on a click within the app
  (``'navigation'``) how long until the next page showed (``NAV``). The route
  is the page's template (``/[tenant]/fish``), with the web's release.

Neither is tenant data, and neither names a person. Both are function-only,
as ``api_tokens`` is (0066): forced RLS with no policy and no grant, so the app
role reaches them only through ``record_api_timings(jsonb)`` and
``record_page_timings(jsonb)``, ``SECURITY DEFINER``, each of which also prunes
its table's rows older than ``KEEP_DAYS``. They are read as the admin.

Revision ID: 0091
Revises: 0090
"""

from alembic import context, op

revision = "0091"
down_revision = "0090"

#: How long a timing is kept. ``timings.py`` doesn't repeat it: the prune is
#: here alone.
KEEP_DAYS = 90
#: What a browser reports (web-vitals' names, and the web's own ``NAV``).
METRICS = ("TTFB", "FCP", "LCP", "INP", "CLS", "NAV")
KINDS = ("load", "navigation")

_PINNED = "SECURITY DEFINER SET search_path = pg_catalog, public"
FUNCTIONS = ("record_api_timings(jsonb)", "record_page_timings(jsonb)")


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE api_timings (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            method text NOT NULL,
            route text NOT NULL,
            status smallint NOT NULL,
            total_ms real NOT NULL CHECK (total_ms >= 0),
            db_ms real NOT NULL CHECK (db_ms >= 0),
            db_queries integer NOT NULL CHECK (db_queries >= 0)
        )
        """)
    op.execute(f"""
        CREATE TABLE page_timings (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            route text NOT NULL,
            metric text NOT NULL CONSTRAINT page_timings_metric_check
                CHECK (metric IN ({_in(METRICS)})),
            value real NOT NULL CHECK (value >= 0),
            kind text NOT NULL CHECK (kind IN ({_in(KINDS)})),
            release text
        )
        """)
    for table in ("api_timings", "page_timings"):
        op.execute(f"CREATE INDEX {table}_recorded_at_idx ON {table} (recorded_at)")
        op.execute(f"CREATE INDEX {table}_route_idx ON {table} (route, recorded_at)")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(f"""
        CREATE FUNCTION record_api_timings(p_rows jsonb) RETURNS void
        LANGUAGE sql {_PINNED} AS $$
            DELETE FROM public.api_timings
            WHERE recorded_at < now() - interval '{KEEP_DAYS} days';
            INSERT INTO public.api_timings
                (method, route, status, total_ms, db_ms, db_queries)
            SELECT r.method, r.route, r.status, r.total_ms, r.db_ms, r.db_queries
            FROM jsonb_to_recordset(p_rows) AS r (
                method text, route text, status smallint, total_ms real,
                db_ms real, db_queries integer
            );
        $$
        """)
    op.execute(f"""
        CREATE FUNCTION record_page_timings(p_rows jsonb) RETURNS void
        LANGUAGE sql {_PINNED} AS $$
            DELETE FROM public.page_timings
            WHERE recorded_at < now() - interval '{KEEP_DAYS} days';
            INSERT INTO public.page_timings (route, metric, value, kind, release)
            SELECT r.route, r.metric, r.value, r.kind, r.release
            FROM jsonb_to_recordset(p_rows) AS r (
                route text, metric text, value real, kind text, release text
            );
        $$
        """)
    for fn in FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {_app_role()}")


def downgrade() -> None:
    for fn in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION {fn}")
    op.execute("DROP TABLE page_timings")
    op.execute("DROP TABLE api_timings")

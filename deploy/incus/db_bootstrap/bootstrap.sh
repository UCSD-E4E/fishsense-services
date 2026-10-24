#!/bin/sh
# v2's database and roles, in v1's Postgres -- idempotent, on every converge.
#
# The `db-bootstrap` one-shot (compose.yml) runs this as the cluster's admin
# role (`postgres`) before `migrate`. It exists because the production volume
# is v1's `pgdata` (PLAN.md §6.1: v2 lives in a new database in the SAME
# instance), and the image's /docker-entrypoint-initdb.d runs only on an EMPTY
# data directory -- v1 learnt that and ships no init SQL at all
# (fishsense-lite deploy/incus/pg_volumes/scripts/README.md). Locally the same
# roles come from deploy/local/initdb/; here nothing would create them.
#
# What it makes, and why each is shaped so:
#
#   fishsense_owner    LOGIN, BYPASSRLS, not superuser. Owns fishsense_services
#                      and runs `migrate` and `migrate-v1`. BYPASSRLS because
#                      FORCE ROW LEVEL SECURITY binds the table owner too, so a
#                      plain owner is blocked by the policies it writes under
#                      (PLAN.md §6.4; migrate-v1's preflight refuses without
#                      it), and the lab lookups in migrations 0030/0032 rely on
#                      it. No statement_timeout: v1's postgres.conf sets 30 s
#                      for everyone, and a migration or migrate-v1 (~43 s on
#                      the 2026-09-25 dump) must not be cut off by it.
#   fishsense_app      LOGIN, NOBYPASSRLS. The API and the orchestrator.
#   fishsense_backup   LOGIN, BYPASSRLS + pg_read_all_data: reads every table
#                      in every database (v2's under forced RLS, v1's archive,
#                      superset's) and writes none (02-backup-role.sh's
#                      reasoning). Also migrate-v1's read-only v1 source.
#   fishsense_analytics, fishsense_research
#                      NOLOGIN groups. Migrations 0030/0032 create them if
#                      missing; made here first, with the same attributes, so
#                      the logins below can join them before the first migrate.
#   fishsense_superset Superset's login, retired 2026-10-23: where it exists it
#                      is made NOLOGIN, with no password and no tenant binding.
#                      Not created on a new cluster.
#   fishsense_smoke    LOGIN in fishsense_research: the smoke test reads a dive
#                      the way the research repos do (ops/smoke.py).
#
# Every run re-asserts each role's attributes and password, so a password
# rotated in OpenBao reaches Postgres on the next converge (v1's roles came from
# a restored dump, and a rotation meant psql by hand). It never drops anything,
# and never touches v1's `fishsense` database or v1's roles: that database is
# the rollback and the archive.
#
# Environment (compose.yml; passwords from db-bootstrap.env, secrets.nix):
#   PGHOST PGUSER PGPASSWORD       the admin connection (libpq)
#   FISHSENSE_*_PASSWORD           the login roles' passwords
#   FISHSENSE_DATABASE             v2's database (default fishsense_services)
set -eu

: "${PGHOST:?the Postgres host}"
: "${PGUSER:?the admin role}"
: "${PGPASSWORD:?the admin role's password}"
: "${FISHSENSE_OWNER_PASSWORD:?}"
: "${FISHSENSE_APP_PASSWORD:?}"
: "${FISHSENSE_BACKUP_PASSWORD:?}"
: "${FISHSENSE_SMOKE_PASSWORD:?}"
DB="${FISHSENSE_DATABASE:-fishsense_services}"

psql_() {
    psql -X --no-psqlrc -v ON_ERROR_STOP=1 -q "$@"
}

# The server may still be starting (compose waits for pg_isready, which says
# "accepting" slightly before a login does on a first boot).
tries=0
until psql_ -d postgres -c 'SELECT 1' >/dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -ge 30 ]; then
        echo "db-bootstrap: Postgres at $PGHOST never accepted $PGUSER" >&2
        exit 1
    fi
    sleep 2
done

echo "db-bootstrap: roles"
psql_ -d postgres \
    -v owner_password="$FISHSENSE_OWNER_PASSWORD" \
    -v app_password="$FISHSENSE_APP_PASSWORD" \
    -v backup_password="$FISHSENSE_BACKUP_PASSWORD" \
    -v smoke_password="$FISHSENSE_SMOKE_PASSWORD" <<'SQL'
DO $$
DECLARE
    name text;
BEGIN
    FOREACH name IN ARRAY ARRAY[
        'fishsense_owner', 'fishsense_app', 'fishsense_backup',
        'fishsense_analytics', 'fishsense_research',
        'fishsense_smoke'
    ] LOOP
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = name) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', name);
        END IF;
    END LOOP;
END
$$;

ALTER ROLE fishsense_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS
    PASSWORD :'owner_password';
ALTER ROLE fishsense_owner SET statement_timeout = 0;
ALTER ROLE fishsense_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
    PASSWORD :'app_password';
ALTER ROLE fishsense_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS
    PASSWORD :'backup_password';
GRANT pg_read_all_data TO fishsense_backup;

ALTER ROLE fishsense_analytics NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
ALTER ROLE fishsense_research NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'fishsense_superset') THEN
        ALTER ROLE fishsense_superset NOLOGIN PASSWORD NULL;
    END IF;
END
$$;
ALTER ROLE fishsense_smoke LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
    PASSWORD :'smoke_password';
GRANT fishsense_research TO fishsense_smoke;
SQL

echo "db-bootstrap: database $DB"
psql_ -d postgres -v db="$DB" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER fishsense_owner', :'db')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db')
\gexec
ALTER DATABASE :"db" OWNER TO fishsense_owner;
-- v2's database is v2's roles' alone: v1's roles (and any future tenant's
-- stray login) don't even connect. The groups carry CONNECT for their logins.
REVOKE CONNECT, TEMPORARY ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db"
    TO fishsense_app, fishsense_backup, fishsense_analytics, fishsense_research;
-- No JIT for anyone in v2's database. Our views stack DISTINCT ONs whose
-- estimated cost crosses the JIT thresholds, and compiling costs more than
-- running: 9.0 s of the Today summary's 13.5 s on production (2026-10-21).
-- Here rather than in postgres.conf, which reaches the running server only
-- on a restart; this reaches every new session from the next converge.
ALTER DATABASE :"db" SET jit = off;
SQL

# Superset's retired login carries no tenant binding in v2's database (it had
# app.tenant_id = the lab until 2026-10-23).
psql_ -d postgres -v db="$DB" <<'SQL'
SELECT format('ALTER ROLE fishsense_superset IN DATABASE %I RESET ALL', :'db')
WHERE EXISTS (SELECT FROM pg_roles WHERE rolname = 'fishsense_superset')
\gexec
SQL

echo "db-bootstrap: done"

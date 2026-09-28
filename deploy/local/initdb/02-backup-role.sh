#!/bin/bash
# Create the role the nightly backup dumps as (ops.backup; fishsense-lite's
# deploy/pg_volumes/scripts/2026-05-01_create_backup_role.sql, changed for v2).
#
# pg_read_all_data reads every table and writes none -- v1's grant. v2 adds
# BYPASSRLS: every tenant-scoped table forces row-level security, and pg_dump
# runs with row_security off, so without it the dump refuses outright ("query
# would be affected by row-level security policy"). This role is the backup
# process's alone; the API and the orchestrator never bypass RLS.
#
# Like 01-app-role.sh this runs only on a fresh data volume. On an existing
# cluster (production), an operator runs the same statements by hand, as a
# superuser, once. tests/test_backup_postgres.py runs this file.
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v backup_password="${FISHSENSE_BACKUP_PASSWORD:-backup-dev-only}" <<'SQL'
CREATE ROLE fishsense_backup LOGIN PASSWORD :'backup_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
GRANT pg_read_all_data TO fishsense_backup;
SQL

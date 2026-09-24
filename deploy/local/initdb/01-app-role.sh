#!/bin/bash
# Create the unprivileged role the API runs as. Migrations (run as the owner)
# grant it exactly its runtime privileges; it never owns a table.
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v app_password="$FISHSENSE_APP_PASSWORD" <<'SQL'
CREATE ROLE fishsense_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOBYPASSRLS;
SQL

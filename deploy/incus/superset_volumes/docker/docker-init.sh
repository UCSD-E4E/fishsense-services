#!/usr/bin/env bash
#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
set -e

#
# Always install local overrides first
#
/app/docker/docker-bootstrap.sh

if [ "$SUPERSET_LOAD_EXAMPLES" = "yes" ]; then
    STEP_CNT=4
else
    STEP_CNT=3
fi

echo_step() {
cat <<EOF
######################################################################
Init Step ${1}/${STEP_CNT} [${2}] -- ${3}
######################################################################
EOF
}
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
# If Cypress run – overwrite the password for admin and export env variables
if [ "$CYPRESS_CONFIG" == "true" ]; then
    ADMIN_PASSWORD="general"
    export SUPERSET_TESTENV=true
    export POSTGRES_DB=superset_cypress
    export SUPERSET__SQLALCHEMY_DATABASE_URI=postgresql+psycopg2://superset:superset@db:5432/superset_cypress
fi
# Initialize the database
echo_step "1" "Starting" "Applying DB migrations"
superset db upgrade
echo_step "1" "Complete" "Applying DB migrations"

# Create an admin user
echo_step "2" "Starting" "Setting up admin user ( admin / $ADMIN_PASSWORD )"
if [ "$CYPRESS_CONFIG" == "true" ]; then
    superset load_test_users
else
    superset fab create-admin \
        --username admin \
        --email admin@superset.com \
        --password "$ADMIN_PASSWORD" \
        --firstname Superset \
        --lastname Admin
fi
echo_step "2" "Complete" "Setting up admin user"
# Create default roles and permissions
echo_step "3" "Starting" "Setting up roles and perms"
superset init
echo_step "3" "Complete" "Setting up roles and perms"

if [ "$SUPERSET_LOAD_EXAMPLES" = "yes" ]; then
    # Load some data to play with
    echo_step "4" "Starting" "Loading examples"


    # If Cypress run which consumes superset_test_config – load required data for tests
    if [ "$CYPRESS_CONFIG" == "true" ]; then
        superset load_examples --load-test-data
    else
        superset load_examples
    fi
    echo_step "4" "Complete" "Loading examples"
fi

# ── Step 5: import committed dashboard assets (IaC) ──────────────────────────
# Superset dashboards-as-code: databases/datasets/charts/dashboards live as YAML
# under docker/assets/ and are re-imported on every converge (idempotent —
# import overwrites by UUID). The FishSense DB connection's password is injected
# from $ANALYTICS_DATABASE_PASSWORD at import time via a placeholder, so the
# secret never lives in git. A failed import must NOT fail init (Superset still
# comes up), so this is best-effort.
#
# fishsense-services: kept from fishsense-lite except the connection. It is v2's
# `fishsense_services` as the `fishsense_superset` login (a member of
# `fishsense_analytics`, bound to the lab -- migration 0030, db_bootstrap), not
# v1's `superset` metadata role, whose $DATABASE_PASSWORD v1 injected here.
ASSETS_SRC=/app/docker/assets
if [ -d "$ASSETS_SRC" ]; then
    echo_step "5" "Starting" "Importing committed dashboard assets"
    # Whole block runs in a subshell with `set +e` and a trailing `|| echo` so a
    # bad bundle can NEVER fail init — Superset (which depends on init completing
    # successfully) must still come up. The script runs under `set -e`, so
    # without this containment a single failed command here takes Superset down.
    (
        set +e
        BUNDLE="$(mktemp -d)"
        # Superset's v1 importer runs remove_root() on every zip path (strips the
        # top-level folder) and then requires metadata.yaml, so the assets MUST
        # live under one. Without it every path is mangled, metadata.yaml is
        # never found, v1 raises IncorrectVersionError, and the dispatcher
        # silently falls back to the legacy v0 JSON importer — which then chokes
        # on our YAML ("Expecting value: line 1 column 1").
        ROOT="$BUNDLE/fishsense_assets"
        mkdir -p "$ROOT"
        cp -r "$ASSETS_SRC"/. "$ROOT"/
        # nix-store bind mounts carry 1970 mtimes and `zip` rejects timestamps
        # before 1980 ("ZIP does not support timestamps before 1980"), so bump
        # every copied file to now before archiving.
        find "$BUNDLE" -exec touch {} +
        # Inject the analytics login's password into the FishSense connection URI.
        sed -i "s|__ANALYTICS_DB_PASSWORD__|${ANALYTICS_DATABASE_PASSWORD}|g" "$ROOT"/databases/*.yaml
        ZIP=/tmp/fishsense-assets.zip
        python -c "import shutil,sys; shutil.make_archive('/tmp/fishsense-assets','zip',sys.argv[1])" "$BUNDLE"
        # `import-dashboards` (not `import-assets`, which doesn't exist in 6.0.0)
        # imports the whole export bundle — databases/datasets/charts/dashboards.
        superset import-dashboards -p "$ZIP" -u admin
        rm -rf "$BUNDLE" "$ZIP"
    ) || echo "WARN: dashboard asset import failed — Superset still starts (fix the bundle + re-converge)"
    echo_step "5" "Complete" "Importing committed dashboard assets"
fi

#!/usr/bin/env bash
# The UI harness (compose.ui.yml): the API with a mock sign-in and a local
# Garage, the synthetic `lab` tenant, then the web on http://localhost:3000.
#
# Sign in with any username. dev-admin is the tenant's admin and dev-member a
# member; anyone else has no membership until an `org` claim (typed into the
# login form's claims box) names a tenant that claims it.
#
#   deploy/local/ui/up.sh          # start (or resume) and seed
#   deploy/local/ui/up.sh --reset  # start from an empty database and bucket
set -euo pipefail
cd "$(dirname "$0")/../../.."

compose=(docker compose -f compose.yml -f compose.ui.yml)
if [[ "${1:-}" == "--reset" ]]; then
  "${compose[@]}" down --volumes
fi
"${compose[@]}" up --build --detach --wait api oidc garage
"${compose[@]}" --profile seed run --rm seed

cd apps/web
[[ -d node_modules ]] || npm ci
set -a
# shellcheck source=web.env
. ../../deploy/local/ui/web.env
# Made up per launch: the mock accepts anything, and nothing here is a
# credential worth committing.
made_up() { head -c 24 /dev/urandom | base64 | tr -d '/+='; }
AUTH_SECRET="$(made_up)"
AUTH_AUTHENTIK_SECRET="$(made_up)"
FISHSENSE_API_SERVICE_PASSWORD="$(made_up)"
set +a
echo "web: http://localhost:3000 -- sign in as dev-admin or dev-member"
exec npm run dev

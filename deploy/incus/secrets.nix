# App-secret delivery for the fishsense interior (HANDOFF §9) -- fishsense-services.
#
# Mirrors fishsense-lite deploy/incus/secrets.nix: nixosModules.tenant's
# vault-agent renders only the CERTS (fishsense.vm, temporal); app secrets are
# renders we add here (the list merges with the platform's). The tenant AppRole
# reads secret/data/tenants/fishsense/*, so it can render everything under there.
#
# ONE CHANGE OF SHAPE: v1 rendered one consolidated app.env into every service
# ("vars a service doesn't recognize are ignored"). v2 renders ONE FILE PER
# CONSUMER, because v2's credentials have boundaries v1's didn't:
#   * the API runs as the app role and must never be configured with the
#     schema owner's DSN (PLAN.md §9.10; settings.MigrationSettings);
#   * the backup role bypasses RLS and reads every tenant; only the backup
#     process holds it (ops/backup/settings.py);
#   * the web is internet-facing and holds no database credential at all.
# A shared file would hand every container every credential. deploy/tests pins
# who holds which (test_production_compose.py).
#
# ── OpenBao KV layout under secret/tenants/fishsense/ (KV-v2) ───────────────────
# v1's paths, REUSED where the value is the same (seeded already; v1 keeps
# reading them through the rollback window):
#   postgres      { password }                   # the admin `postgres` role (v1's)
#   superset      { secret_key, db_password }    # flask secret + v1's `superset` metadata role
#   web           { auth_secret }                # next-auth cookie/JWT signing
#   label_studio  { api_key }                    # the service account's (e4e+fishsense@ucsd.edu)
#   object_store  { access_key, secret_key }     # Garage; also Label Studio's presign key (v1's fallback)
#   nas           { username, password }         # Synology FileStation (read for ingest, write for backups)
# NEW for v2, owner-seeded (docs/cutover.md "Seed OpenBao"):
#   nrp_orchestrator   { kubeconfig }            # the fishsense-orchestrator SA token kubeconfig
#                                                # (deploy/nrp/deployer-rbac.yaml). NOT v1's `nrp`:
#                                                # v1's scales via deployments/scale, v2's creates and
#                                                # deletes; each side keeps its own through the rollback.
#   model_weights      { access_key, secret_key }  # NOT rendered here -- the NRP processor's Secret
#                                                  # (docs/cutover.md); listed so the layout is whole.
# PLATFORM writes (tofu — do NOT seed):
#   generated/services_db { owner_password, app_password, backup_password,
#                           analytics_password, smoke_password }
#                   # v2's DB logins; generate-once, [a-z0-9]{64} (krg-infra terraform/secrets #554)
#   oidc/web-service-account { username, password }   # svc_fishsense's app password (krg-infra #550)
#   oidc/web        { client_id, client_secret, issuer_url }   (#438)
#   oidc/analytics  { client_id, client_secret, issuer_url }   (#438)
# No longer read: api {username, password} (v1's basic-auth service account) and
# oidc/proxy-outpost-token (v2 has no forwardAuth outpost). Leave them for v1's
# rollback; retire after the 48 h window.
#
# ⚠️ vault-agent is FAIL-CLOSED (errorOnMissingKey): a referenced path/field that
# isn't seeded takes the whole stack down rather than starting empty -- including
# the fishsense.vm cert, so the inner Traefik too. Seed every path above before
# the first v2 converge. Only the NRP kubeconfig is soft (below).
#
# Rotating one: `bao kv patch`, then `systemctl restart openbao-agent.service`,
# then recreate the consumers -- env_file is read at create time (v1's
# deploy/incus/README.md, "Rotating an app secret", holds for v2 unchanged).
# A services_db password also needs the converge's db-bootstrap to run, which
# re-asserts it in Postgres: `systemctl restart fishsense.service` does both.
#
# Paths are spelled out in full, as v1's are, so `grep services_db` finds every
# consumer of a secret.
{
  krg.vaultAgent.renders = [
    {
      # The postgres container. On an existing volume the image ignores it; it
      # matters to a fresh one (rehearsal, DR) and documents the admin identity.
      destination = "/run/tenant/secrets/postgres.env";
      contents = ''
        {{ with secret "secret/data/tenants/fishsense/postgres" }}POSTGRES_PASSWORD={{ .Data.data.password }}{{ end }}
      '';
    }
    {
      # db-bootstrap: the admin connection, and every v2 login's password, which
      # it re-asserts on each converge.
      destination = "/run/tenant/secrets/db-bootstrap.env";
      contents = ''
        {{ with secret "secret/data/tenants/fishsense/postgres" }}PGPASSWORD={{ .Data.data.password }}{{ end }}
        {{ with secret "secret/data/tenants/fishsense/generated/services_db" }}FISHSENSE_OWNER_PASSWORD={{ .Data.data.owner_password }}
        FISHSENSE_APP_PASSWORD={{ .Data.data.app_password }}
        FISHSENSE_BACKUP_PASSWORD={{ .Data.data.backup_password }}
        FISHSENSE_ANALYTICS_PASSWORD={{ .Data.data.analytics_password }}
        FISHSENSE_SMOKE_PASSWORD={{ .Data.data.smoke_password }}{{ end }}
      '';
    }
    {
      # migrate (and migrate-v1, run by hand in the same container): the owner,
      # and v1's database read as the backup role -- pg_read_all_data, no
      # writes, so migrate-v1 cannot modify v1 even by mistake.
      destination = "/run/tenant/secrets/migrate.env";
      contents = ''
        {{ with secret "secret/data/tenants/fishsense/generated/services_db" }}FISHSENSE_MIGRATION_DATABASE_URL=postgresql+psycopg://fishsense_owner:{{ .Data.data.owner_password | urlquery }}@postgres:5432/fishsense_services
        FISHSENSE_V1_DATABASE_URL=postgresql+psycopg://fishsense_backup:{{ .Data.data.backup_password | urlquery }}@postgres:5432/fishsense{{ end }}
      '';
    }
    {
      # The API: the app role, and the issuer + audience of the tokens it
      # accepts -- the web's confidential client (oidc/web), whose client id is
      # the `aud` of the tokens the web sends. Add the mobile client's id to
      # the audiences when it exists.
      destination = "/run/tenant/secrets/api.env";
      contents = ''
        {{ with secret "secret/data/tenants/fishsense/generated/services_db" }}FISHSENSE_DATABASE_URL=postgresql+asyncpg://fishsense_app:{{ .Data.data.app_password | urlquery }}@postgres:5432/fishsense_services{{ end }}
        {{ with secret "secret/data/tenants/fishsense/oidc/web" }}FISHSENSE_OIDC_ISSUER={{ .Data.data.issuer_url }}
        FISHSENSE_OIDC_AUDIENCES={{ .Data.data.client_id }}{{ end }}
      '';
    }
    {
      # The orchestrator: the app role (as the API), the NAS (read-only use),
      # Label Studio, and Garage -- whose key is also the one Label Studio
      # presigns with (v1's `presign_*` fallback to `object_store`).
      destination = "/run/tenant/secrets/orchestrator.env";
      contents = ''
        {{ with secret "secret/data/tenants/fishsense/generated/services_db" }}FISHSENSE_DATABASE_URL=postgresql+asyncpg://fishsense_app:{{ .Data.data.app_password | urlquery }}@postgres:5432/fishsense_services{{ end }}
        {{ with secret "secret/data/tenants/fishsense/nas" }}FISHSENSE_NAS_USERNAME={{ .Data.data.username }}
        FISHSENSE_NAS_PASSWORD={{ .Data.data.password }}{{ end }}
        {{ with secret "secret/data/tenants/fishsense/label_studio" }}FISHSENSE_LABEL_STUDIO_API_KEY={{ .Data.data.api_key }}{{ end }}
        {{ with secret "secret/data/tenants/fishsense/object_store" }}FISHSENSE_OBJECT_STORE_ACCESS_KEY_ID={{ .Data.data.access_key }}
        FISHSENSE_OBJECT_STORE_SECRET_ACCESS_KEY={{ .Data.data.secret_key }}
        FISHSENSE_LABEL_STUDIO_S3_ACCESS_KEY={{ .Data.data.access_key }}
        FISHSENSE_LABEL_STUDIO_S3_SECRET_KEY={{ .Data.data.secret_key }}{{ end }}
      '';
    }
    {
      # The backup: its own role (BYPASSRLS + pg_read_all_data) and the NAS it
      # writes dumps to. The orchestrator never holds this file.
      destination = "/run/tenant/secrets/backup.env";
      contents = ''
        {{ with secret "secret/data/tenants/fishsense/generated/services_db" }}FISHSENSE_BACKUP_DATABASE_PASSWORD={{ .Data.data.backup_password }}{{ end }}
        {{ with secret "secret/data/tenants/fishsense/nas" }}FISHSENSE_NAS_USERNAME={{ .Data.data.username }}
        FISHSENSE_NAS_PASSWORD={{ .Data.data.password }}{{ end }}
      '';
    }
    {
      # The web: next-auth, its Authentik client, its service account (the
      # public landing page's calls to the API), and Label Studio (triage).
      # No database credential.
      destination = "/run/tenant/secrets/web.env";
      contents = ''
        {{ with secret "secret/data/tenants/fishsense/web" }}AUTH_SECRET={{ .Data.data.auth_secret }}{{ end }}
        {{ with secret "secret/data/tenants/fishsense/oidc/web" }}AUTH_AUTHENTIK_ID={{ .Data.data.client_id }}
        AUTH_AUTHENTIK_SECRET={{ .Data.data.client_secret }}
        AUTH_AUTHENTIK_ISSUER={{ .Data.data.issuer_url }}{{ end }}
        {{ with secret "secret/data/tenants/fishsense/oidc/web-service-account" }}FISHSENSE_API_SERVICE_USERNAME={{ .Data.data.username }}
        FISHSENSE_API_SERVICE_PASSWORD={{ .Data.data.password }}{{ end }}
        {{ with secret "secret/data/tenants/fishsense/label_studio" }}LABEL_STUDIO_API_KEY={{ .Data.data.api_key }}{{ end }}
      '';
    }
    {
      # Superset x4 (profile `superset`): v1's flask secret and metadata role
      # (superset_config.py DATABASE_USER=superset), the analytics OIDC client
      # (names read by superset_config.py OAUTH_PROVIDERS), and the password
      # docker-init.sh injects into the dashboards' connection to v2.
      destination = "/run/tenant/secrets/superset.env";
      contents = ''
        {{ with secret "secret/data/tenants/fishsense/superset" }}SUPERSET_SECRET_KEY={{ .Data.data.secret_key }}
        DATABASE_PASSWORD={{ .Data.data.db_password }}{{ end }}
        {{ with secret "secret/data/tenants/fishsense/oidc/analytics" }}AUTHENTIK_KEY={{ .Data.data.client_id }}
        AUTHENTIK_SECRET={{ .Data.data.client_secret }}
        AUTHENTIK_ISSUER={{ .Data.data.issuer_url }}{{ end }}
        {{ with secret "secret/data/tenants/fishsense/generated/services_db" }}ANALYTICS_DATABASE_PASSWORD={{ .Data.data.analytics_password }}{{ end }}
      '';
    }
    {
      # The smoke test's research login (profile `ops`, run by hand).
      destination = "/run/tenant/secrets/smoke.env";
      contents = ''
        {{ with secret "secret/data/tenants/fishsense/generated/services_db" }}FISHSENSE_SMOKE_RESEARCH_DATABASE_URL=postgresql+psycopg://fishsense_smoke:{{ .Data.data.smoke_password | urlquery }}@postgres:5432/fishsense_services{{ end }}
      '';
    }
    {
      # NRP token kubeconfig for the orchestrator's `fishsense-orchestrator`
      # ServiceAccount (ns e4e-fishsense; deploy/nrp/deployer-rbac.yaml): it
      # stands the processor up and tears it down, and the cert sync writes the
      # processor's Temporal Secret with it. Rendered to a WRITABLE runtime path
      # and mounted into the orchestrator and the cert sync (compose.yml).
      #
      # SOFT render (errorOnMissingKey=false) — v1's reasoning, unchanged: an
      # un-seeded kubeconfig must NOT fail-close the whole agent (that would also
      # block the fishsense.vm cert → inner Traefik → the entire stack). Unseeded,
      # the file renders EMPTY: the cert sync no-ops, and the orchestrator's
      # wakes fail loudly (every stage needing the processor), not the slot.
      #
      # The token expires (a bound token is 7 days; see deployer-rbac.yaml) —
      # reseed on rotation, then restart openbao-agent and the two consumers.
      destination = "/run/tenant/nrp/kubeconfig";
      errorOnMissingKey = false;
      contents = ''
        {{ with secret "secret/data/tenants/fishsense/nrp_orchestrator" }}{{ .Data.data.kubeconfig }}{{ end }}
      '';
    }
  ];
}

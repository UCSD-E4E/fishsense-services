# Cutover runbook: fishsense-lite (v1) → fishsense-services (v2) on the fishsense slot

The concrete runbook for PLAN.md §6.6, for the deploy this repo ships:
`flake.nix` + `deploy/incus/` (the slot's interior), `.github/workflows/`
(build → release → promote → deploy), and `deploy/nrp/` (the processor, stood up
on NRP by the orchestrator). Target weekend: **~2026-10-11**. Rollback window:
**48 h**.

Conventions below:

```bash
# From the operator's machine: one command on the slot, as v1's runbook does it.
slot() { ssh krg-admin@krg-nat.ucsd.edu "incus exec fishsense --project fishsense -- $*"; }

# Everything written `dc ...` or `tctl ...` runs in a root shell ON the slot:
#   ssh -t krg-admin@krg-nat.ucsd.edu incus exec fishsense --project fishsense -- bash
# with these two defined there.
#
# docker compose exactly as the stack unit runs it (never reconstruct it:
# --project-directory is load-bearing, and the -f store path changes every
# converge -- fishsense-lite deploy/incus/README.md):
dc() {
  eval "$(systemctl show fishsense.service -p ExecStart --value \
    | grep -oE '/nix/store/[^ ]+/bin/docker compose --project-directory [^ ]+ -f [^ ]+')" "$@"
}
# The Temporal CLI against krg-prod, with the slot's own client cert:
tctl() {
  nix shell nixpkgs#temporal-cli -c temporal \
    --address krg-prod.ucsd.edu:7233 --namespace fishsense \
    --tls-cert-path /run/tenant/temporal/tls.crt --tls-key-path /run/tenant/temporal/tls.key \
    --tls-ca-path /run/tenant/temporal/ca.crt --tls-server-name workflows.krg.ucsd.edu "$@"
}
# v1's schedules (fishsense-lite, every ensure_schedule; the smoke test's V1_SCHEDULE_IDS):
V1_SCHEDULES="cluster-dive-frames compute-laser-depths evaluate-laser-auto-accept measure-fish
  perform-checkerboard-calibration perform-laser-calibration populate-headtail-labels
  populate-laser-labels populate-species-labels predict-headtail-images predict-laser-images
  predict-slate-images preprocess-headtail-images preprocess-laser-images preprocess-slate-images
  preprocess-species-images reconcile-labeling-configs scale-down-idle-data-worker
  sync-label-studio-dive-slate-labels sync-label-studio-headtail-labels
  sync-label-studio-laser-labels sync-label-studio-species-labels"   # each + "-workflow-schedule"
# v2's (the orchestrator's registry with BioCLIP off, plus the backup's; the smoke
# test's expected_schedule_ids() is the source of truth):
V2_SCHEDULES="cluster-dive-frames compute-laser-depths evaluate-laser-auto-accept measure-fish
  perform-checkerboard-calibration perform-laser-calibration populate-headtail-labels
  populate-laser-labels populate-species-labels predict-headtail-images predict-laser-images
  preprocess-headtail-images preprocess-laser-images preprocess-slate-images
  preprocess-species-images reconcile-labeling-configs sync-label-studio-dive-slate-labels
  sync-label-studio-head-tail-labels sync-label-studio-laser-labels
  sync-label-studio-species-labels tear-down-idle-processors backup-databases"
```

## 0. What changes, and what doesn't

| | v1 (fishsense-lite) | v2 (fishsense-services) |
|---|---|---|
| Slot, tenant, hostname, quota, image, SSO group | `fishsense`, e4e, fishsense.e4e.ucsd.edu, 6/12 GiB, krg-golden, FishSense | **unchanged** |
| krg-infra pin | ec7e4b60 | **the same rev at cutover** (§1.2) |
| Runner scope / selfupdate / autoUpgrade | `UCSD-E4E/fishsense-lite` | `UCSD-E4E/fishsense-services` — all three follow mkTenant's `repo` after the first switch |
| `fishsense.e4e.ucsd.edu` | fishsense-lite-web | `web` (Next.js, in-app OIDC) |
| `api.fishsense.e4e.ucsd.edu` | fishsense-api behind a forwardAuth outpost | `api`, bearer tokens validated in-app; **no outpost** |
| `analytics.fishsense.e4e.ucsd.edu` | Superset (running) | Superset, **behind the `superset` profile, off until §3 step 5c** |
| Postgres | `postgres:17.10`, volume `pgdata` | **the same container and volume**; v2 in a new database `fishsense_services`; v1's `fishsense` untouched (rollback, then archive) |
| Workers | api-workflow-worker, backup-worker | `orchestrator`, `backup` |
| NRP | 4 standing Deployments, scaled 0↔N, `kubectl apply -k` from CI | stood up per wake and **deleted** when idle, from the orchestrator image's manifests at `FISHSENSE_NRP_IMAGE_TAG` |
| Temporal | krg-prod, ns `fishsense`, mTLS | the same; v2's queues and schedule ids are distinct (§5) |
| Secrets | one `app.env` | one render per consumer (`deploy/incus/secrets.nix`) |

The compose project stays `fishsense` (the composeStack's working directory), so
the switch's `up -d --remove-orphans` removes v1's app containers and keeps
`postgres`, its volume and the network. v1's Superset containers are not
orphans (their services exist in v2's file, behind a profile), so they keep
running until the profile is turned on and recreates them (§4).

## 1. Before the weekend

### 1.1 Blockers only an admin or the owner can clear

- [ ] **Repo public** (or the admin gives the slot a GitHub token): selfupdate
      and autoUpgrade fetch `github:UCSD-E4E/fishsense-services` anonymously, as
      they fetch fishsense-lite today.
- [ ] **GHCR packages public** — `fishsense-services-{api,orchestrator,backup,web,processor,processor-gpu}`.
      Neither the slot nor NRP has a pull secret (v1's images are public; new
      GHCR packages start private).
- [ ] **GitHub App** (ADR 0022) installed on `UCSD-E4E/fishsense-services`, and
      the org secrets/vars this repo's workflows use: `APP_ID`,
      `APP_PRIVATE_KEY` (release.yml, update-flake.yml). If `main` is protected,
      add the App to its bypass list (update-flake.yml pushes the lock to main).
- [ ] **A first release cut**: merge release-please's PR (→ `v0.1.0` or
      whatever it proposes), let promote.yml retag all six images, **merge the
      `auto-deploy/fishsense-services-v…` PR**. Until then the compose pins
      `v0.0.0`, which doesn't exist: `grep -c v0.0.0 deploy/incus/compose.yml`
      must print 0.
- [ ] **Runner re-scope prepared** (admin): the token broker must mint
      registration tokens for `UCSD-E4E/fishsense-services` for the `fishsense`
      tenant from the switch on. mkTenant's `repo` already says so; the runner
      re-registers (`replace = true`) on the first converge that carries it.
- [ ] **Authentik** (admin, platform-owned apps):
  - the web's client (`oidc/web`) allows the `refresh_token` grant and maps the
    `offline_access`, `groups` and **`org`** scopes (v2's web requests all
    four; PLAN.md §4.2). Redirect URI unchanged:
    `https://fishsense.e4e.ucsd.edu/api/auth/callback/authentik`;
  - a **web service account** with an app password, allowed to use the web
    client's `client_credentials` grant. Its username + app password go to
    OpenBao `web_service_account` (§1.3), its `sub` gets a lab membership (§3
    step 4d). (v1's `api` basic-auth account may be reused if it is the same
    Authentik account; then seed its values under the new path.)
- [ ] **Decide** (owner): Fish Measurements dashboard — `GRANT fishsense_research
      TO fishsense_superset` or leave it broken (§4). PLAN.md §9.18 is open.
- [x] ~~**Fix or accept** dive 509~~: fixed in v1 on 2026-09-16. 509 now has
      its own calibration, and its 162 measurements are current in both
      (PLAN.md §6.4).

### 1.2 The krg-infra pin

v2's `flake.lock` must carry the rev fishsense-lite's does on the day, so the
switch changes the interior and nothing of the platform's:

```bash
v1=$(git -C ../fishsense-lite show origin/main:flake.lock | jq -r '.nodes["krg-infra"].locked.rev')
v2=$(jq -r '.nodes["krg-infra"].locked.rev' flake.lock)
[ "$v1" = "$v2" ] && echo same || {
  nix flake lock --override-input krg-infra "github:KastnerRG/krg-infra/$v1?dir=nix"
  git commit -m "chore(flake): pin krg-infra to fishsense-lite's rev for the cutover" flake.lock
}
nix eval --raw .#nixosConfigurations.fishsense.config.system.build.toplevel.drvPath
```

Both repos' weekly `update-flake.yml` bump to krg-infra `main` on Mondays, so
they normally agree. Freeze both bumps (disable the workflow) for the weekend.

### 1.3 Seed OpenBao (`secret/tenants/fishsense/*`)

vault-agent is **fail-closed**: one missing path/field keeps every render —
including the `fishsense.vm` cert — from landing, and the slot serves nothing.
Seed everything before the switch. New passwords are hex (they are interpolated
into URLs and a `sed`):

```bash
gen() { openssl rand -hex 32; }
# New paths: `put` (they don't exist). Read secrets from stdin, never argv.
bao kv put secret/tenants/fishsense/services_db \
  owner_password="$(gen)" app_password="$(gen)" backup_password="$(gen)" \
  analytics_password="$(gen)" smoke_password="$(gen)"
read -r -p 'web service account username: ' U
read -rs -p 'its app password: ' P && echo
bao kv put secret/tenants/fishsense/web_service_account username="$U" password=- <<<"$P"; unset P
bao kv put secret/tenants/fishsense/nrp_orchestrator kubeconfig=@nrp-orchestrator.kubeconfig   # §1.4
bao kv put secret/tenants/fishsense/model_weights access_key=- secret_key=...                 # §1.5 (processor only)
# Confirm every field WITHOUT printing values:
for p in postgres superset web label_studio object_store nas services_db \
         web_service_account nrp_orchestrator oidc/web oidc/analytics; do
  printf '%s: ' "$p"; bao kv get -format=json "secret/tenants/fishsense/$p" | jq -c '.data.data | keys'
done
```

| Path | Fields | Status | Read by |
|---|---|---|---|
| `postgres` | `password` | **v1's, reuse** | postgres (fresh volume only), db-bootstrap (`PGPASSWORD`) |
| `superset` | `secret_key`, `db_password` | **v1's, reuse** | superset ×4 (metadata DB as v1's `superset` role) |
| `web` | `auth_secret` | **v1's, reuse** | web |
| `label_studio` | `api_key` | **v1's, reuse** (the service account's) | orchestrator, web |
| `object_store` | `access_key`, `secret_key` | **v1's, reuse** — needs read **and write** on `labels-fishsense-lite` (v2's one bucket: its scratch and JPEGs go there, under `tenants/`) | orchestrator (also Label Studio's presign key), smoke |
| `nas` | `username`, `password` | **v1's, reuse** — needs read on `/fishsense_data/REEF/data` and write on `/fishsense_process_work/database_backups_v2` | orchestrator, backup |
| `oidc/web` | `client_id`, `client_secret`, `issuer_url` | platform (tofu) — **do not seed** | web; api (issuer, audience) |
| `oidc/analytics` | `client_id`, `client_secret`, `issuer_url` | platform — **do not seed** | superset |
| `services_db` | `owner_password`, `app_password`, `backup_password`, `analytics_password`, `smoke_password` | **new** | db-bootstrap (all), migrate (owner, backup), api + orchestrator (app), backup (backup), superset (analytics), smoke (smoke) |
| `web_service_account` | `username`, `password` | **new** | web |
| `nrp_orchestrator` | `kubeconfig` | **new** — *soft* render | orchestrator, nrp-temporal-cert-sync |
| `model_weights` | `access_key`, `secret_key` | **new** — not rendered on the slot | the NRP processor's Secret (§1.4) |

v1's `api {username, password}`, `nrp {kubeconfig}` and `oidc/proxy-outpost-token`
are not read by v2. Leave them until the rollback window closes.

### 1.4 NRP (namespace `e4e-fishsense`)

Nothing here touches v1's Deployments: v2's objects are `fishsense-processor*`
and `fishsense-orchestrator`. The Temporal Secret is shared by name
(`fishsense-data-worker-temporal-certs`, same CN): the slot's cert sync keeps it.

```bash
nix develop   # kubectl + kubelogin-oidc, pinned with the slot
# a. The orchestrator's identity (namespace-admin, once; re-apply when the Role changes).
kubectl apply -f deploy/nrp/deployer-rbac.yaml
# b. Its kubeconfig, from the token Secret (empty .data.token => NRP stripped it;
#    use `kubectl -n e4e-fishsense create token fishsense-orchestrator --duration=168h`
#    and plan the weekly re-mint + reseed).
TOKEN=$(kubectl -n e4e-fishsense get secret fishsense-orchestrator-token -o jsonpath='{.data.token}' | base64 -d)
kubectl config view --minify --raw > nrp-orchestrator.kubeconfig   # then swap the user for the SA token
#    Verify it can do exactly its job, then seed it (§1.3) and shred the file:
KUBECONFIG=nrp-orchestrator.kubeconfig kubectl -n e4e-fishsense auth can-i delete deployments   # yes
KUBECONFIG=nrp-orchestrator.kubeconfig kubectl -n e4e-fishsense auth can-i list secrets         # no
# c. The processor's Secret: everything its pods read from FISHSENSE_* that isn't in
#    the manifests (envFrom `fishsense-processor-secrets`, optional: missing => pods
#    start and fail their first object-store/weights call).
kubectl -n e4e-fishsense create secret generic fishsense-processor-secrets \
  --from-literal=FISHSENSE_OBJECT_STORE_ENDPOINT_URL=https://s3.e4e.ucsd.edu \
  --from-literal=FISHSENSE_OBJECT_STORE_REGION=garage \
  --from-literal=FISHSENSE_OBJECT_STORE_BUCKET=labels-fishsense-lite \
  --from-literal=FISHSENSE_OBJECT_STORE_LEGACY_LABELS_PREFIX=fishsense-lite \
  --from-literal=FISHSENSE_OBJECT_STORE_ACCESS_KEY_ID="$(bao kv get -field=access_key secret/tenants/fishsense/object_store)" \
  --from-literal=FISHSENSE_OBJECT_STORE_SECRET_ACCESS_KEY="$(bao kv get -field=secret_key secret/tenants/fishsense/object_store)" \
  --from-literal=FISHSENSE_MODEL_WEIGHTS_ENDPOINT_URL=https://s3.e4e.ucsd.edu \
  --from-literal=FISHSENSE_MODEL_WEIGHTS_ACCESS_KEY_ID="$(bao kv get -field=access_key secret/tenants/fishsense/model_weights)" \
  --from-literal=FISHSENSE_MODEL_WEIGHTS_SECRET_ACCESS_KEY="$(bao kv get -field=secret_key secret/tenants/fishsense/model_weights)" \
  --from-literal=FISHSENSE_SAM3_SHA256="<§1.5>" --from-literal=FISHSENSE_SAM3_SIZE="<§1.5>"
#    (FISHSENSE_BIOCLIP_* join it only when BioCLIP is enabled, after its evaluation.)
```

The processor's other settings are in the manifests (`deploy/nrp/*.yaml`, baked
into the orchestrator image): role, krg-prod Temporal, `/certs`, the weights cache.

### 1.5 Model weights (`model-weights` bucket, `{name}/{version}/{filename}`)

- **Laser detector run3** — v1 baked it into its image from Hugging Face; v2
  reads it from Garage, verified against fishsense-core's manifest
  (`laser-detector/run3@bd3ab8f5e273`, `run3_epoch_021.pt`, 294 473 278 bytes):

  ```bash
  uv run python -m fishsense_core.models prefetch --dest ./weights --name laser-detector
  sha256sum weights/laser-detector/run3/run3_epoch_021.pt   # bd3ab8f5e273da37...
  aws s3 cp weights/laser-detector/run3/run3_epoch_021.pt \
    s3://model-weights/laser-detector/run3/run3_epoch_021.pt --endpoint-url https://s3.e4e.ucsd.edu
  ```
- **SAM 3.1** — already in the bucket from v1 (`sam3/3.1/sam3.1_multiplex.pt`).
  fishsense-core 4.1.0 has no manifest entry for it, so v2 pins it from
  settings; **measure it once**:

  ```bash
  aws s3 cp s3://model-weights/sam3/3.1/sam3.1_multiplex.pt . --endpoint-url https://s3.e4e.ucsd.edu
  sha256sum sam3.1_multiplex.pt; stat -c %s sam3.1_multiplex.pt   # -> FISHSENSE_SAM3_SHA256 / _SIZE (§1.4c)
  ```
- **BioCLIP** — not needed at cutover (ships disabled).

### 1.6 Rehearse (≥ 2 clean runs; PLAN.md §6.4)

The production compose, locally, against a restored production dump, touching
nothing shared (PLAN.md §6.5). `deploy/rehearsal/stage.py` renders
`secrets.nix` with the rehearsal's own values, rewrites `/run/tenant`, and layers
`compose.rehearsal.yml`: a local Temporal dev server (never krg-prod's
namespace — v2's scheduled workflows share v1's class names and minutes), no NRP
(the processor runs locally), no edge, project `fishsense-rehearsal`.

```bash
# Images: a release's (as it will deploy) or local builds.
python3 deploy/rehearsal/stage.py --out ~/fs-rehearsal --version v0.1.0 [--values rehearsal.toml]
#   or: for t in api orchestrator backup processor; do docker build --target $t -t fishsense-services-$t:rehearsal .; done
#       docker build -f apps/web/Dockerfile -t fishsense-services-web:rehearsal .
#       python3 deploy/rehearsal/stage.py --out ~/fs-rehearsal --local-images
R=~/fs-rehearsal/dc
$R config --quiet
$R up -d postgres
# v1 as the slot has it: its database (and, for fidelity, its superset DB).
$R exec -T postgres createdb -U postgres fishsense
$R exec -T postgres pg_restore -U postgres -d fishsense --no-owner --no-privileges < fishsense.dump
$R up -d                                        # db-bootstrap, migrate, api, web, orchestrator, backup
$R ps -a                                        # db-bootstrap + migrate: Exited (0)
$R run --rm migrate fishsense-services-api migrate-v1      # -> GO
$R up -d --force-recreate db-bootstrap          # binds the Superset login to the lab
$R run --rm smoke --dive 491 --min-measurements 1          # temporal/db/api/web PASS; externals need real keys
$R down -v                                      # the rehearsal's volumes only
```

`rehearsal.toml` may carry **read-only** Garage keys (`[object_store]`) and a
throwaway Label Studio token; never production write keys. A service given no
credential is pointed at an `.invalid` host, so a rehearsal without values
never calls app.heartex.com, s3.e4e.ucsd.edu or the NAS. A rehearsal passes
when: bootstrap and migrate exit 0 twice in a row (idempotence), migrate-v1
prints GO, and the smoke test passes every check it has credentials for.

First run (2026-09-30, local images, v1's committed schema, no credentials):
bootstrap, migrate and the cert sync exit 0 on the first and second converge;
migrate-v1 GO; the smoke test PASSes api, openapi, head, audit, lab, research,
and all 22 schedules, and FAILs label studio, object store (`.invalid`) and the
web — whose landing page 500'd when the Authentik issuer was unreachable (it
mints its service token per request). Fixed since: a kind the web cannot ask
about is left out and the page says so (`lib/active-projects.ts`), so an
Authentik outage costs the labeling cards and sign-in, not the public page.

Rehearsals 1 and 2 (2026-10-01): the released `v0.1.0` images, v1's nightly
dump `database_backups/fishsense/2026-10-01T03-00-31Z.dump` (525 dives,
134,662 images, 3,130 measurements), no credentials. Each from empty volumes:
- db-bootstrap, migrate and the cert sync exit 0, then exit 0 again on a
  forced re-converge.
- migrate-v1 GO in ~3m50s: every v1 row accounted for, tenancy audit clean,
  parity 3,128 = 3,128.
- In run 2, a second migrate-v1 is also GO and duplicates nothing (still 525
  dives, 134,662 captures, 3,130 measurements).
- Smoke with `--dive 491` passes 8 of 10, every check it has credentials for:
  api, openapi, head (0034), audit, lab, research (28 measurements), 22
  schedules, and the web (200, now that the outage fix is in). Label Studio and
  the object store fail by design (`.invalid`).
- No errors in any service's logs, except the web's per-kind "could not list"
  lines, which are the outage fix logging the unreachable `.invalid` Authentik.

Use dive 491, not 490, for the smoke test: v1 has no measurements on 490 now.

## 2. Stop v1 (T-0, Friday evening)

1. Announce the window; freeze merges to both repos.
2. Turn off v1's nightly converge for the weekend (it would rebuild from
   fishsense-lite at 04:00): `slot systemctl stop nixos-upgrade.timer`.
3. Pause every v1 schedule (paused, not deleted: the rollback needs them):

   ```bash
   for s in $V1_SCHEDULES; do tctl schedule toggle --pause --schedule-id "$s-workflow-schedule" --reason cutover; done
   tctl schedule toggle --pause --schedule-id fishsense-daily-db-backup --reason cutover
   ```
4. Drain: wait until `tctl workflow list --query 'ExecutionStatus="Running"'`
   shows none of v1's (task queues `fishsense_api_queue`,
   `fishsense_data_processing*_queue`, `fishsense_backup_queue`).
5. Scale v1's NRP data-worker to 0:
   `kubectl -n e4e-fishsense scale deploy fishsense-data-processing-workflow-worker{,-gpu,-gpu-cpu-fallback,-light} --replicas=0`.
6. Maintenance: stop v1's writers, keep its database up:
   `slot docker stop fishsense-web-1 fishsense-fishsense-api-1 fishsense-fishsense-api-workflow-worker-1 fishsense-fishsense-backup-worker-1`
   (the edge then answers 502 for the public hosts).

## 3. The weekend

### Step 3 — back up (off the slot)

```bash
slot 'docker exec fishsense-postgres-1 pg_dumpall -U postgres --globals-only' > globals-$(date -u +%F).sql
for db in fishsense superset; do
  slot "docker exec fishsense-postgres-1 pg_dump -U postgres -Fc $db" > $db-$(date -u +%F).dump
  pg_restore -l $db-$(date -u +%F).dump > /dev/null && echo "$db ok"
done
# Copy them OFF the slot and off krg-nat (NAS + a second place). v1's nightly
# dumps at /fishsense_process_work/database_backups stay as they are.
```

### Step 4 — switch the slot to v2, then migrate

a. **Admin, the first converge** (v1's selfupdate still targets fishsense-lite):

   ```bash
   slot nixos-rebuild switch --flake github:UCSD-E4E/fishsense-services#fishsense --refresh
   ```
   It renders v2's secrets, links v2's config, and `up -d --remove-orphans
   --force-recreate`s v2's interior: **db-bootstrap** (v2's roles, the
   `fishsense_services` database — nothing of v1's), **migrate** (schema to head
   + the tenancy audit, as `fishsense_owner`), then api, web, orchestrator
   (which creates v2's schedules), backup, and the cert sync. v1's app
   containers are removed as orphans; `postgres` is recreated on the same
   volume. From here selfupdate, autoUpgrade and the runner all point at this
   repo. Check: `slot systemctl show fishsense.service -p Result` → `success`,
   `slot docker ps -a` → db-bootstrap and migrate `Exited (0)`.

   Exit 4 from `switch-to-configuration` is not a deploy signal (v1,
   2026-07-17); `fishsense.service`'s Result is.

b. **Hold v2 still** until GO: pause v2's schedules and keep the portal closed.

   ```bash
   for s in $V2_SCHEDULES; do tctl schedule toggle --pause --schedule-id "$s" --reason cutover; done
   dc stop web
   ```
   Also stop the nightly converge again (the switch installed v2's timer, which
   would `up -d` the web at 04:00): `systemctl stop nixos-upgrade.timer`.

c. **migrate-v1** — v1's `fishsense` → v2's lab tenant, one transaction, read
   from v1 as the read-only backup role (it cannot write v1):

   ```bash
   dc run --rm migrate fishsense-services-api migrate-v1 | tee migrate-v1-$(date -u +%F).log
   ```
   Exit 0 and `GO:` on the last line — every v1 row accounted for, the tenancy
   audit clean, measurement parity (rehearsed: 2 968 = 2 968). Anything else is
   **NO-GO → §6 rollback** (v2's database can simply be dropped; v1's is intact).

d. **Memberships** (as the owner; there is no API for them yet):

   A `sub` is what Authentik issues for the web client (the provider's subject
   mode — by default the user's hashed id, Authentik → Directory → Users → UID).
   Never an email. Admins are the people in v1's `FishSense-Prod-Admins`. Fill
   in the subs, then run (idempotent: re-running updates the roles):

```bash
dc exec -T postgres psql -U postgres -d fishsense_services -v ON_ERROR_STOP=1 <<'SQL'
WITH lab AS (SELECT id FROM tenants WHERE slug = 'lab'),
grants(sub, role) AS (VALUES
    ('service:fishsense-orchestrator', 'member'),  -- the orchestrator acts only as a member
    ('<web service account sub>',      'member'),  -- the public landing page
    ('<lab admin sub>',                'admin')    -- one row per admin: triage + calibration links
),
users_ AS (
    INSERT INTO users (sub) SELECT sub FROM grants
    ON CONFLICT (sub) DO UPDATE SET sub = EXCLUDED.sub
    RETURNING id, sub
)
INSERT INTO memberships (tenant_id, user_id, role)
SELECT lab.id, users_.id, grants.role FROM lab, users_ JOIN grants USING (sub)
ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = EXCLUDED.role;
SQL
```

   **Research logins** (people; PLAN.md §9.20), in the same `psql`: `CREATE ROLE
   alice LOGIN PASSWORD '…' IN ROLE fishsense_research; ALTER ROLE alice SET
   search_path = v1, public;` — CONNECT comes with the group, and v1's research
   SQL runs unchanged (lab only, read-only). v1's `psql -U postgres` habit ends.

### Step 5 — complete the switch

a. Delete v1's schedules (they are v1's rollback only now; the smoke test fails
   while any remain):
   `for s in $V1_SCHEDULES; do tctl schedule delete --schedule-id "$s-workflow-schedule"; done;
    tctl schedule delete --schedule-id fishsense-daily-db-backup`.
b. Re-run the bootstrap so Superset's login is bound to the lab (it now exists):
   `dc up -d --force-recreate db-bootstrap` → its log says `binding
   fishsense_superset to the lab`.
c. **Superset on**: commit `COMPOSE_PROFILES=superset` in
   `deploy/incus/compose.env` (PR, merge), then **Deploy → Run workflow →
   incus** (the runner is ours now). If the owner decided so (§1.1), first
   `GRANT fishsense_research TO fishsense_superset;` for Fish Measurements.
d. Unpause v2's schedules: `for s in $V2_SCHEDULES; do tctl schedule toggle --unpause --schedule-id "$s"; done`.
e. `dc start web`.
f. NRP: nothing to roll out. The first stage with work stands the processor up
   at `FISHSENSE_NRP_IMAGE_TAG` (watch `kubectl -n e4e-fishsense get deploy
   -l app.kubernetes.io/part-of=fishsense`); the hourly sweeper deletes it when
   idle. v1's Deployments stay at 0 (NRP's GC takes them in two weeks; the
   rollback re-applies them).

### Step 6 — verify: GO / NO-GO

```bash
dc run --rm smoke --dive 491 --min-measurements 28   # 491 has 28 in v1; 490 now has none
```
Exit 0 and `GO: all 10 checks passed`: API `/healthz` and OpenAPI; schema at
head; tenancy audit; lab tenant; dive 491's measurements read **as the research
role** through the `v1` views; every v2 schedule on krg-prod and none of v1's;
Label Studio (workspace `FishSense`); the web's landing page; a key under v1's
JPEG prefix in Garage. Then by hand (PLAN.md §6.6):

- a login on fishsense.e4e.ucsd.edu, and the portal as a lab admin;
- one portal triage action (it writes to Label Studio);
- one research query (`psql` as a research login, `search_path = v1, public`);
- one pipeline firing end to end: `tctl schedule trigger --schedule-id cluster-dive-frames`
  and watch the processor come up on NRP and go away;
- a Label Studio sync: `tctl schedule trigger --schedule-id sync-label-studio-laser-labels`;
- the backup: `tctl schedule trigger --schedule-id backup-databases`, then
  three new dumps under `/fishsense_process_work/database_backups_v2/{fishsense_services,fishsense,superset}/`.

### Step 7 — reopen

Re-enable the nightly converge (`slot systemctl start nixos-upgrade.timer`; it
now builds this repo), unfreeze merges, announce. Watch the first full hourly
cycle in the Temporal UI (`https://workflows.krg.ucsd.edu/namespaces/fishsense`).

## 4. What breaks, or differs, from v1

- **Fish Measurements dashboard** (`fish_measurements` dataset, 3 charts): it
  reads the `v1` research views; `fishsense_analytics` (Superset's login's role,
  migration 0030) can read only `dive_pipeline_status` and beneath. Until
  `GRANT fishsense_research TO fishsense_superset`, it errors with "permission
  denied" (pinned by `deploy/tests/test_bootstrap_postgres.py`). The **Pipeline
  Status** dashboard and its three datasets work (same SQL as v1, tested on v2).
- **Superset is v2's only from step 5c** (its login must be bound to the lab,
  which exists only after migrate-v1). Until then v1's Superset containers keep
  running: compose's `--remove-orphans` leaves a profile-disabled service's
  containers alone (checked with compose 5.5), and traefik still reaches them
  as `superset`. So analytics.fishsense stays up across the switch, showing v1's
  frozen data, until step 5c recreates the containers from v2's definition.
  *Note:* v1's compose runs Superset unconditionally (since 2026-07-15), although
  v1's README still says "off by default"; v2 gates it for real.
- **v1's Superset connection** is repointed (same uuid) at v2 by the first
  import; the v1 archive is reachable with `psql` only.
- **No forwardAuth on the API.** Any client that relied on the outpost's
  session cookie must send a bearer token. v1's NRP data-worker did (basic auth
  through the outpost); it is retired.
- **The web's portal gate** is the lab `admin` membership, not
  `PORTAL_ALLOWED_GROUPS=FishSense-Prod-Admins` — a missing membership reads as
  "no access" (§3 step 4d).
- **Scheduled workflow ids collide with v1's** (`<WorkflowClass>-workflow-<time>`,
  the same class names at the same minutes: 20 of v2's 22). Harmless once v1's
  schedules are paused and deleted (steps 2, 5a); fatal to a rehearsal on
  krg-prod — which is why rehearsals use a local Temporal. Schedule ids and task
  queues are fully distinct (`*-workflow-schedule` vs bare ids;
  `fishsense_api_queue`/`fishsense_data_processing*`/`fishsense_backup_queue` vs
  `fishsense_orchestrator`/`fishsense_processor*`/`fishsense_backup`); child
  workflow ids carry v2's UUID dive ids where v1's carried integers.
- **`apps/web/package.json`'s version** follows the monorepo's release version
  from the first release (it was v1's 0.14.0).

## 5. Where every setting comes from

Rendered (OpenBao → `/run/tenant/secrets/<consumer>.env`, `deploy/incus/secrets.nix`)
or committed (`deploy/incus/compose.yml`). `deploy/tests/test_settings_are_satisfied.py`
builds each service's settings from exactly these and fails if one is missing.

| Service | Rendered | Committed |
|---|---|---|
| postgres | `POSTGRES_PASSWORD` | image, config |
| db-bootstrap | `PGPASSWORD`, `FISHSENSE_{OWNER,APP,BACKUP,ANALYTICS,SMOKE}_PASSWORD` | `PGHOST`, `PGUSER`, `FISHSENSE_DATABASE` |
| migrate | `FISHSENSE_MIGRATION_DATABASE_URL`, `FISHSENSE_V1_DATABASE_URL` | `FISHSENSE_APP_ROLE` |
| api | `FISHSENSE_DATABASE_URL`, `FISHSENSE_OIDC_ISSUER`, `FISHSENSE_OIDC_AUDIENCES` | — (JWKS defaults to `{issuer}jwks/`) |
| web | `AUTH_SECRET`, `AUTH_AUTHENTIK_{ID,SECRET,ISSUER}`, `FISHSENSE_API_SERVICE_{USERNAME,PASSWORD}`, `LABEL_STUDIO_API_KEY` | `FISHSENSE_API_URL`, `FISHSENSE_TENANT`, `AUTH_URL`, `LABEL_STUDIO_{URL,ENABLED}`, `FISHSENSE_OBJECT_STORE_ENDPOINT_URL` |
| orchestrator | `FISHSENSE_DATABASE_URL`, `FISHSENSE_NAS_{USERNAME,PASSWORD}`, `FISHSENSE_LABEL_STUDIO_API_KEY`, `FISHSENSE_OBJECT_STORE_{ACCESS_KEY_ID,SECRET_ACCESS_KEY}`, `FISHSENSE_LABEL_STUDIO_S3_{ACCESS_KEY,SECRET_KEY}` | `FISHSENSE_ORCHESTRATOR_SUB`, `FISHSENSE_TEMPORAL_*`, `FISHSENSE_NAS_{URL,RAW_ROOT_PATH,STAGE_CONCURRENCY}`, `FISHSENSE_LABEL_STUDIO_{URL,WORKSPACE,BOT_USER_ID}`, `FISHSENSE_LABEL_STUDIO_S3_{BUCKET,PREFIX,ENDPOINT_URL,REGION}`, `FISHSENSE_OBJECT_STORE_{ENDPOINT_URL,REGION,BUCKET,LEGACY_LABELS_PREFIX}`, `FISHSENSE_NRP_{KUBECONFIG_PATH,NAMESPACE,IMAGE_TAG}`, `FISHSENSE_SPECIES_PREDICTION_ENABLED` |
| backup | `FISHSENSE_BACKUP_DATABASE_PASSWORD`, `FISHSENSE_NAS_{USERNAME,PASSWORD}` | `FISHSENSE_BACKUP_{DATABASE_HOST,DATABASE_USER,DATABASES,NAS_ROOT_PATH}`, `FISHSENSE_NAS_URL`, `FISHSENSE_TEMPORAL_*` |
| nrp-temporal-cert-sync | (the kubeconfig and the leaf are files) | `FISHSENSE_TEMPORAL_CLIENT_*`, `FISHSENSE_NRP_{KUBECONFIG_PATH,NAMESPACE}` |
| smoke | migrate's + orchestrator's + `FISHSENSE_SMOKE_RESEARCH_DATABASE_URL` | `FISHSENSE_SMOKE_{API,WEB}_URL`, Temporal, Label Studio, object store |
| superset ×4 | `SUPERSET_SECRET_KEY`, `DATABASE_PASSWORD`, `AUTHENTIK_{KEY,SECRET,ISSUER}`, `ANALYTICS_DATABASE_PASSWORD` | `superset_volumes/docker/.env` |
| processor (NRP) | the `fishsense-processor-secrets` k8s Secret (§1.4c) | `deploy/nrp/*.yaml` |

## 6. Rollback (within 48 h of reopening)

v1's database was never modified; **anything written in v2 after reopening is
lost**. In order:

1. Pause v2's schedules (`$V2_SCHEDULES`, as step 4b), then delete them — left
   behind they fire into a namespace with no v2 worker, under v1's workflow ids.
2. Admin: `slot nixos-rebuild switch --flake github:UCSD-E4E/fishsense-lite#fishsense --refresh`
   (or `--rollback` to the previous generation, which needs no network). v1's
   interior comes back on the same `pgdata`; v2's containers are removed as
   orphans; v2's database and roles stay, unused. Re-scope the runner to
   fishsense-lite (the broker must mint for it again).
3. v1's workers recreate v1's schedules at startup (`ensure_schedule`); if step
   5a hasn't run, unpause them instead.
4. NRP: delete v2's processors if up
   (`kubectl -n e4e-fishsense delete deploy fishsense-processor fishsense-processor-light fishsense-processor-gpu fishsense-processor-gpu-cpu-fallback --ignore-not-found`),
   then restore v1's with fishsense-lite's Deploy → `data-worker`.
5. Superset: v1's init re-imports its bundle and repoints the connection back.
6. Smoke v1 by hand (its portal, one sync), reopen, write down what was lost.

## 7. After the window

- Retire v1's OpenBao paths (`api`, `nrp`, `oidc/proxy-outpost-token`) and ask the
  platform to retire the `fishsense_orchestrator` proxy provider/outpost (#440).
- v1's `fishsense` database is the read-only archive (and the backup keeps
  dumping it): revoke v1's `superset` and `backup` roles' logins when nothing
  needs them.
- Archive `fishsense-lite` (PLAN.md §9.9); v1's NRP Deployments go to NRP's GC.
- The web service account and every other `sub` is a membership row: grant and
  revoke there, not in Authentik groups.
- **Revive the eroded laser labels (fishsense-lite #932), in v2.** Decided
  2026-10-01: never applied in v1. On the 2026-10-01 dump, 15,263 of 50,266
  laser labels are superseded, and every `superseded_reason` is NULL. v1's dry
  run (~2026-09-26) proposed reviving 9,615 that the corrected validator keeps.
  The migration carries every flag across.
  1. Dry-run: `python -m fishsense_services_orchestrator.laser.remediate
     dry-run --out report.json [--exclusions excl.json]`. It writes nothing.
  2. The owner reviews the report and lists any dives to exclude.
  3. `apply --report report.json`. It refuses anything but a reviewed dry-run
     report, and records the revivals as `superseded_reason = remediation`.

  Revived labels change laser depths, so v2 marks the affected lengths stale and
  re-measures them. Live research queries will move; the frozen imwut/cscw CSVs
  won't. Announce it before applying.

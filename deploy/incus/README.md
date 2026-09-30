# fishsense-services interior — KRG Incus platform tenant `fishsense`

The **repo-owned interior** for the fishsense tenant on the KRG Incus platform
(ADR 0017 / 0020), replacing fishsense-lite's on the same slot at the cutover.
`fishsense-selfupdate` converges the instance with
`nixos-rebuild switch --flake github:UCSD-E4E/fishsense-services#fishsense`,
reading the repo-root [`flake.nix`](../../flake.nix), which imports this
directory. Mirrors fishsense-lite's `deploy/incus/`; the differences are listed
in [`docs/cutover.md`](../../docs/cutover.md) §0, which is also the runbook.

> **Status: not converging anything yet.** Until the admin's first switch
> (docs/cutover.md §3 step 4a) the slot builds fishsense-lite, and this repo's
> Deploy workflow has no runner to land on — by design.

## What runs here

| Service | Route / role | Auth / credential |
|---|---|---|
| `traefik` | inner edge `:443`, `fishsense.vm` cert | — |
| `web` | `fishsense.e4e.ucsd.edu` | in-app OIDC (`oidc/web`); API calls as the user, or the web's service account |
| `api` | `api.fishsense.e4e.ucsd.edu` | bearer tokens validated in-app (issuer + audience = the web's client). **No forwardAuth.** |
| `orchestrator` | every schedule; stands the NRP processor up and down | app role; krg-prod Temporal (mTLS) |
| `backup` | nightly dumps of `fishsense_services`, `fishsense` (v1's archive), `superset` → NAS | backup role (BYPASSRLS + pg_read_all_data); krg-prod Temporal |
| `nrp-temporal-cert-sync` | one-shot: forwards the rotated Temporal leaf to NRP | the orchestrator's NRP kubeconfig |
| `db-bootstrap` | one-shot, every converge: v2's database and roles | the admin `postgres` role |
| `migrate` | one-shot, every converge: schema to head + tenancy audit | the owner role |
| `postgres` | v1's container and `pgdata` volume | — |
| `smoke` (profile `ops`) | by hand: GO / NO-GO | owner + research login |
| `superset` ×4 + `valkey` (profile `superset`) | `analytics.fishsense.e4e.ucsd.edu` | in-app OIDC (`oidc/analytics`); data as `fishsense_superset` |

**Off-slot:** the processor on NRP (`deploy/nrp/`, applied and deleted by the
orchestrator), Garage (`s3.e4e.ucsd.edu`), Temporal (krg-prod), Label Studio
(app.heartex.com), the NAS.

## Files

| File | What |
|---|---|
| `compose.yml` | the interior. Every `fishsense-services-*` pin and `FISHSENSE_NRP_IMAGE_TAG` are one release version (promote.yml → `deploy/bump_pins.py`). |
| `secrets.nix` | vault-agent renders, **one env file per consumer**; the OpenBao layout and what to seed. |
| `workdir.nix` | links the committed config into `/var/lib/krg/fishsense` (compose's project directory). |
| `prune.nix` | v1's image prune after each converge (the 20 GB disk). |
| `cert-sync-timer.nix` | re-runs the NRP cert sync every 6 h, between rotations. |
| `compose.env` | the project's `.env`: `COMPOSE_PROFILES` (Superset's switch). |
| `traefik-dynamic.yml` | TLS + Host → service; no middlewares. |
| `db_bootstrap/bootstrap.sh` | v2's roles and database, idempotent, as the admin. |
| `pg_volumes/config/` | v1's `postgres.conf` and `pg_hba.conf`, plus one line for v2's roles. |
| `superset_volumes/docker/` | v1's Superset bootstrap, and the dashboards-as-code bundle repointed at v2. |

`deploy/tests/` pins the invariants between these files (the Temporal reload
list, every setting each service requires, who holds which credential, pinned
images, v1's volume names, the renders' shape) and runs the bootstrap and
`migrate` against a Postgres shaped like the slot's. `deploy/rehearsal/` stages
this compose on a dev box without touching production (docs/cutover.md §1.6).

## Operating notes (v1's, still true)

- **The converge is a trigger, not a CI gate**: `deploy.yml`'s `verify-incus`
  compares the deployed pins with main's; the smoke test is the GO/NO-GO.
- **Committed config applies only via force-recreate** (krg-infra #459): a
  changed converge recreates the whole stack, postgres included.
- **Rotating an app secret is three steps**: `bao kv patch`, restart
  `openbao-agent.service`, recreate the consumers (`systemctl restart
  fishsense.service` — which also re-runs db-bootstrap, so a `services_db`
  password reaches Postgres too). Never `cat` a render.
- **Temporal schedules are create-if-missing**: to change one's cadence, delete
  it (`temporal schedule delete --schedule-id <id>`) and restart the orchestrator.

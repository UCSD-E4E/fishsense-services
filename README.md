# FishSense Services

The backend for [FishSense](https://e4e.ucsd.edu/fishsense): it ingests the
underwater images divers take with FishSense cameras, runs them through the
measurement pipeline (laser calibration, fish detection, head/tail, species,
length), sends what needs a human to Label Studio, and serves the results to a
per-tenant web portal.

This is **v2**. It replaced `fishsense-lite` (v1) in a big-bang cutover on
2026-10-05, on the same KRG Incus slot. [`PLAN.md`](PLAN.md) is the design of
record. It covers why v2 exists, the locked decisions, the architecture, and the
open questions. [`DIAGRAMS.md`](DIAGRAMS.md) draws it.

## How it fits together

```
 divers' cameras ──► e4e NAS ◄────────────── orchestrator ──────► Label Studio (hosted)
                    (raw .ORF)     ingest,    (Temporal worker,     tasks out, labels back
                                   staging     KRG Incus slot)
                                                   │  ▲
                              Temporal (krg-prod)  │  │ results
                                                   ▼  │
                                              processor ◄── Garage (S3): raw scratch,
                                    (Temporal worker on NRP,      JPEGs, model weights
                                     stood up per wake: CPU,
                                     light and GPU pods)

 browser ──► web portal (Next.js) ──► API (FastAPI) ──► Postgres (row-level security per tenant)
```

| Package | What it is |
|---|---|
| [`services/fishsense-services-api`](services/fishsense-services-api) | FastAPI app and the **only owner of the database**: every table, migration (Alembic), SQL statement and RLS policy lives here. Also the `fishsense-services-api` CLI (`migrate`, `migrate-v1`). |
| [`services/fishsense-services-orchestrator`](services/fishsense-services-orchestrator) | The Temporal worker that runs the pipeline. Workflows are grouped into stages, and each stage registers its own schedules. It reaches the database only through the API package's catalogs, acting as a tenant member. It owns NAS access, Label Studio sync, and standing NRP processors up and down. |
| [`services/fishsense-services-processor`](services/fishsense-services-processor) | The Temporal worker that does the image work: raw decode, laser detection, SAM 3.1, head/tail, species, and the slate detector, built on `fishsense-core`. It runs on NRP under one of three roles: `per_image` (CPU), `light`, and `gpu` (with a CPU fallback). |
| [`services/fishsense-services-contracts`](services/fishsense-services-contracts) | The pydantic payloads that cross Temporal between the orchestrator and the processor, versioned (`CONTRACT_VERSION`, with schemas under `schemas/`). |
| [`apps/web`](apps/web) | The portal: Next.js, with OIDC through Authentik. Its API types are generated from the API's OpenAPI spec (`apps/web/openapi.json`). |
| [`deploy/`](deploy) | Production: the Incus slot's compose file and NixOS interior (`deploy/incus/`), the NRP manifests (`deploy/nrp/`), the cutover rehearsal harness, and the tests that pin all of it (`deploy/tests/`). |
| [`tests/`](tests) | Repo-wide architecture rules, e.g. [`test_database_ownership.py`](tests/test_database_ownership.py). |

**Tenancy.** Every row belongs to a tenant, and Postgres row-level security
keeps tenants apart. The API acts for the signed-in user's memberships. The
orchestrator acts as the service principal `service:fishsense-orchestrator`,
which must be granted a membership in a tenant before it can work on that
tenant's data. Partner organizations get their own tenants; see
[`docs/partner-tenants.md`](docs/partner-tenants.md).

## Running it locally

You need [uv](https://docs.astral.sh/uv/), Docker, and Node 22 for the portal.
`nix develop` gives you a shell with all of that, plus `kubectl` and the
Temporal CLI.

```sh
uv sync --all-packages            # every workspace package and its dev tools
docker compose up --build         # Postgres, migrations, the API, Temporal's dev
                                  # server, and the orchestrator and processor
curl localhost:8000/healthz
open http://localhost:8233        # Temporal's UI
```

The local stack starts without any outside services. Ingest needs the NAS, label
sync needs Label Studio, and signing in needs a real OIDC issuer. Set
`FISHSENSE_NAS_*`, `FISHSENSE_LABEL_STUDIO_*` and `FISHSENSE_OIDC_ISSUER` in your
shell or a `.env` file before `up`. The comments at the top of
[`compose.yml`](compose.yml) list what each one needs.

The portal:

```sh
cd apps/web && npm ci && npm run dev
```

### Testing the UI locally

[`compose.yml`](compose.yml) has no sign-in or object store of its own. The
UI harness, [`compose.ui.yml`](compose.ui.yml), adds both:

- a mock OIDC issuer that accepts any username, with optional claims such as
  `name`, `org` or `groups` typed into its login form's claims box;
- a local Garage.

It also seeds a synthetic `lab` tenant:
- Ginny, the decoy, in a measured Lite dive with made-up labels;
- three FishSense Mobile captures with their files in Garage.

Then it runs the web on the host, with hot reload (Node needs to be on your
PATH, e.g. via `nix develop`). Nothing in it reaches production.

```sh
deploy/local/ui/up.sh            # then open http://localhost:3000
deploy/local/ui/up.sh --reset    # start again from an empty database and bucket
```

Sign in as `dev-admin` (the tenant's admin) or `dev-member` (a member). Any
other name has no membership. The seed is
[`dev_seed.py`](services/fishsense-services-api/src/fishsense_services_api/dev_seed.py)
(`fishsense-services-api dev-seed`). It refuses any database that isn't local,
or that holds migrated v1 data.

## Tests

The rule is **test first**: write a failing test, then the code that makes it
pass. Database behavior is tested against real Postgres (testcontainers, so
Docker must be running), because RLS can't be faked.

```sh
uv run black --check services deploy tests        # formatting (CI enforces it)

uv run pytest                                      # repo-wide rules + deploy invariants
cd services/fishsense-services-api && uv run pytest              # unit + integration (Docker)
cd services/fishsense-services-orchestrator && uv run pytest -m "not integration"
cd services/fishsense-services-processor && uv run pytest
cd services/fishsense-services-contracts && uv run pytest

cd apps/web && npm run lint && npm run typecheck && npm test && npm run api:check
```

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs all of this, plus
image builds and an end-to-end run of the built images under compose.

## From merge to production

Releases are cut by [release-please](https://github.com/googleapis/release-please)
from conventional commits (`feat:`, `fix:`, `chore:` and so on). One version
covers the whole repo.

1. A merge to `main` updates the **release PR** (`chore(main): release X.Y.Z`).
2. Merging the release PR publishes the release. **Promote** retags the images
   built from `main`, then opens two PRs: `auto-lock/uv-refresh` (lockfile) and
   `auto-deploy/fishsense-services-vX.Y.Z` (bumps the pinned images in
   `deploy/incus/compose.yml`).
3. Merging the **auto-deploy PR** runs **Deploy** on the slot's self-hosted
   runner: `deploy-incus` converges the slot, and `verify-incus` checks it
   serves the new version.

Production runs on the KRG Incus slot `fishsense`, reached through
`krg-admin@krg-nat.ucsd.edu`:
- the portal at `fishsense.e4e.ucsd.edu`;
- the API at `api.fishsense.e4e.ucsd.edu`;
- Temporal namespace `fishsense` on `krg-prod`.

Secrets come from OpenBao, rendered by vault-agent into `/run/tenant/...`. They
are never in this repo. [`docs/cutover.md`](docs/cutover.md) is the operations
runbook: the cutover as it was run, rehearsals, the krg-infra handoff, and the
post-cutover steps.

## API tokens and the MCP endpoint (AI agents)

Signed-in users make personal API tokens on the web app's **API tokens** page
(`/tokens`). A token acts as the person who made it, and comes in two kinds,
fixed when it is made:

- **read-only**, the default: reads what its owner can read (their tenants,
  or every tenant read-only for a system admin, with each visit recorded) and
  is refused every write request. Reads keep their server-side effects, such
  as that visit record and the Fish list's place names;
- **edit**, an opt-in: also writes what its owner's role allows, through the
  REST API only, and only in tenants its owner is a member of.

No token can unlock a tenant or make, list or revoke tokens. Only a SHA-256 of
each token is stored, and every token expires (90 days by default, a year at
most).

The API serves a read-only [MCP](https://modelcontextprotocol.io) endpoint at
`https://api.fishsense.e4e.ucsd.edu/mcp`. It accepts read-only tokens only:
edit tokens and sign-in sessions are refused. To connect Claude Code (the
tokens page shows this command with your new token filled in):

```sh
claude mcp add --transport http fishsense https://api.fishsense.e4e.ucsd.edu/mcp \
  --header "Authorization: Bearer fishsense_..."
```

Each tool is a thin wrapper over a REST read: tenants, summaries, regions,
missions and their captures, observations, deployments, devices, reviews, and
automatic results. Automatic lengths are listed a page at a time, for a tenant
(optionally one species) or a mission. Each length comes with its reviews and
with the human length on the same frame. Automatic results come back marked
`validated: false`. The
design follows e4e-reference-manager@a12bcca's (`api/app/mcp_server.py`);
[`mcp_server.py`](services/fishsense-services-api/src/fishsense_services_api/mcp_server.py)
says what differs.

## Operating notes

- **Schedules are created if missing and never updated in place.** To change
  one, delete it in Temporal and redeploy; the worker recreates it at startup.
- **NRP processors are stood up on demand and torn down when idle.** NRP
  deletes Deployments older than two weeks, and it expects a second GPU to be
  requested only once the first is fully used.
- **On-demand ops workflows**, started with `temporal workflow start` on the
  `fishsense_orchestrator` queue:
  - `VerifyDiveChecksumsWorkflow` / `VerifyAllDivesChecksumsWorkflow`: re-hash
    captures against the NAS;
  - `RepairMovedCapturePathsWorkflow`: re-point captures whose files moved on
    the NAS. It is a dry run unless given `true` as its second input.

  Each module's docstring has the exact command.
- **The NAS client prefers SMB and logs its transport at login.** A fallback to
  FileStation is logged as a WARNING, and it shouldn't be ignored. The SMB
  login must be on the `KRG` domain (`SYNOLOGY_FS_SMB_DOMAIN`). On 2026-10-07,
  a bare username failing SMB three times got the NAS to block krg-nat.

## Further reading

- [`PLAN.md`](PLAN.md): the design, decisions, and open questions.
- [`DIAGRAMS.md`](DIAGRAMS.md): components, domain model, and sequences.
- [`docs/port-plan.md`](docs/port-plan.md) and [`docs/port-map/`](docs/port-map): how v1 was ported, slice by slice.
- [`docs/cutover.md`](docs/cutover.md): the cutover and operations runbook.
- [`docs/partner-tenants.md`](docs/partner-tenants.md): onboarding a partner organization.
- [`CHANGELOG.md`](CHANGELOG.md): what each release changed.

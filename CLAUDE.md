# CLAUDE.md

This is the working guide for agents in this repo. [`README.md`](README.md) covers
what the system is and how to run it, and [`PLAN.md`](PLAN.md) is the design of
record. This file covers the rules, conventions and gotchas you need to change it
safely. v2 has been in production since the 2026-10-05 cutover, and **production
is the only environment**: there is no staging.

## Non-negotiables

- **Test first.** Write a failing test, run it and watch it fail, then write the
  smallest change that makes it pass. Database behavior, RLS above all, is
  tested against real Postgres (testcontainers), never mocks or SQLite. When a
  test pins a guard, mutation-check it: break the guard and confirm the suite
  fails.
- **Only `fishsense-services-api` touches the database.** Every table, migration,
  SQL statement and RLS policy lives there. The orchestrator calls the API
  package's catalogs as a service principal; the processor and the contracts
  never touch the database. [`tests/test_database_ownership.py`](tests/test_database_ownership.py)
  enforces this. Need a query in the orchestrator? Add it to the API package.
- **Production data changes are the user's call.**
  - Confirm a reported bug in prod read-only before fixing it.
  - Write changes as tools with a dry-run default: run them dry, show the
    result, and wait for an explicit go-ahead for that specific change.
  - Hand-written SQL must match on exact old values and roll back on an
    unexpected row count.
  - A permission denial on a prod write means stop and hand over the command.
    Never reshape a command to get past it.
- **Never read secrets.** Don't open, print or grep credential files:
  `/run/tenant/secrets/*`, the `/run/tenant/temporal` keys, the NRP kubeconfig,
  OpenBao values, the `fishsense-processor-secrets` Secret, or any
  `.secrets.toml`. When a command needs a credential, read it into a shell
  variable inside that command or pass it through the environment. Never echo
  it or put it on a command line.
- **Never change the coordinates of a human label** (laser dots, head/tail
  points, slate corners): no snapping to a fitted line, no derived positions.
  Data is fixed through flags (`superseded`, with `superseded_reason`) or in
  Label Studio. Labels are superseded, never deleted.
- **Stay in scope.** Deliver what was asked. Read an ambiguous request narrowly
  and ask. Don't merge or land other people's branches.

## Commands

```sh
uv sync --all-packages
uv run black services deploy tests                      # CI runs black --check
uv run pytest                                           # tests/ + deploy/tests
cd services/<package> && uv run pytest                  # each package's own suite
cd services/fishsense-services-orchestrator && uv run pytest -m "not integration"
cd apps/web && npm run lint && npm run typecheck && npm test && npm run api:check
```

Integration tests need Docker. The host has no `python3`; use `uv run python`.
`nix develop` provides `kubectl`, the Temporal CLI and node.

## Conventions

- **American spelling, everywhere.** The owner is American: write color, center,
  meter, labeled, canceled, gray, behavior, analyze, organize; not `colour`,
  `centre`, `metre`, `labelled`, `cancelled`, `grey`, `behaviour`, `analyse`,
  `organise`. This covers the website's text, API messages, docs, comments,
  identifiers and file names. Keep a name someone else spells (Label Studio's
  `was_cancelled`, HTML's `aria-labelledby`) as they spell it, and keep our own
  names that are stored or on the wire (Temporal workflow classes and ids,
  tables, columns, API schemas, contract fields: `ReadLaserColoursWorkflow`,
  `LaserColoursView`); renaming one is a migration or a contract change. Two
  tests fail on British spellings: `apps/web/lib/american-spelling.test.ts` in
  the web's source, and `tests/test_american_spelling.py` in Python comments,
  docstrings, test names and the markdown docs. Add a word to them when one
  gets through.
- **Conventional commits.** release-please derives the version from them: a
  `feat:` bumps the minor version, a `fix:` the patch. Commit messages and PR
  descriptions explain *why*, with the incident or the numbers.
- **Migrations** live in `services/fishsense-services-api/src/fishsense_services_api/migrations/versions/`.
  They are hand-written, numbered `NNNN_name.py`, and each has a correct
  `down_revision`. Results tables are append-only (calibrations, predictions,
  measurements), and a `current_*` view picks the live row. Don't upsert over
  history.
- **Contracts** (the Temporal payloads between the orchestrator and the
  processor) are versioned. Changing a payload bumps `CONTRACT_VERSION` and
  adds `schemas/vN.json`.
- **Docstrings carry the history.** Ported code says what it was ported from
  (`fishsense-lite@<sha>`), which v1 rules it keeps, and what v2 changed and
  why. Keep that pattern, including dated incidents.
- **New Temporal work goes in a stage** (`registry.Stage`): workflows, activity
  builders and schedules. Child workflow IDs are deterministic. Activity
  signature changes must keep replaying for in-flight workflows, so add
  optional arguments with defaults.
- **A mission is what a dive used to be** (Phase 2a, migration 0046): one
  device's recording session. Write `missions` / `mission_id` in new code.
  What Temporal or an outside reader sees keeps the old name:
  - Temporal type names are pinned in their decorators
    (`@workflow.defn(name="IngestDiveWorkflow")`); workflow, schedule and error
    type names stay as they were. Renaming a Python class or function that
    Temporal runs means pinning its old name the same way.
  - A renamed payload field keeps its old name on the wire:
    `Annotated[..., renamed_from("dive_id", "mission_id")]`, written by the
    contracts' `DATA_CONVERTER` (serializes by alias) and read under either
    name. Every client and worker uses `DATA_CONVERTER`.
  - Stored values stay: `dive_ineligible` (a gate verdict), `describes_dive`
    (a gate key), Label Studio titles.
  - Outside readers' surfaces stay: the research views (schema `v1`,
    `fish_length_estimate`, `fish_model_*`), `automatic_results_export`, and
    Superset's `dive_pipeline_status`.

## Production access

The slot is the Incus instance `fishsense`. To reach it:
1. ssh to `krg-admin@krg-nat.ucsd.edu`;
2. copy the script there and run
   `incus file push <script> fishsense/root/<script> --project fishsense`;
3. run `incus exec fishsense --project fishsense -- bash -l /root/<script>`.

`bash -l` matters: `docker` is only on the login shell's PATH. Inside the script:

```sh
T() { nix shell nixpkgs#temporal-cli -c temporal --address krg-prod.ucsd.edu:7233 \
  --namespace fishsense --tls-cert-path /run/tenant/temporal/tls.crt \
  --tls-key-path /run/tenant/temporal/tls.key --tls-ca-path /run/tenant/temporal/ca.crt \
  --tls-server-name workflows.krg.ucsd.edu "$@"; }
K() { KUBECONFIG=/run/tenant/nrp/kubeconfig nix shell nixpkgs#kubectl -c kubectl -n e4e-fishsense "$@"; }
Q() { docker exec fishsense-postgres-1 psql -U postgres -d fishsense_services -Atc "$1"; }
```

- `Q` is read-only unless the user has approved a specific write.
- The orchestrator's NRP account can `get` and `delete` Deployments by name but
  **cannot list** them. Ask for each processor Deployment by name
  (`fishsense-processor-gpu`, `-light`, `-gpu-cpu-fallback`, `-per-image`).
- Orchestrator logs (`docker logs fishsense-orchestrator-1`) only reach back to
  the container's last restart.

## Operating rules learned the hard way

- **Schedules are created if missing and never updated in place.** To change
  one: delete it in Temporal just before the deploy, and the restarted worker
  recreates it. A schedule's pending firings and its running workflows don't
  survive a recreate.
- **NRP:**
  - A "Deployment" here is the Kubernetes object, a *processor Deployment*;
    a field trip is a *deployment* in the schema (Phase 2b). Never read one
    for the other.
  - Processors are stood up per wake and torn down when idle; NRP deletes
    Deployments older than two weeks.
  - Request another GPU only once the first is fully used. A slow GPU queue is
    usually CPU decode, so fix that instead.
  - The sweeper keeps a processor while any parent that woke it within the
    grace is still running, and treats the start timeout after a wake as a
    cold start rather than a wedge. After that, idleness decides (#52, #58).
- **NAS:**
  - The client prefers SMB. The SMB login must be on the `KRG` domain
    (`SYNOLOGY_FS_SMB_DOMAIN=KRG`): a bare username fails SMB as a local
    account, and three failures in 24 h get the NAS to block krg-nat, every KRG
    tenant's address (2026-10-07).
  - A fallback to FileStation is logged as a WARNING. Treat it as an incident:
    FileStation alone broke the NAS under v1.
  - Staging is one stream, and the NAS is read-only.
  - Files do get moved after ingest. `RepairMovedCapturePathsWorkflow` repairs
    those paths, dry run first.
- **Label Studio (hosted):**
  - It rate-limits; the client throttles, and a throttled sync fails without
    advancing its cursor.
  - Imports are asynchronous, and re-importing creates duplicate tasks that
    split a row from its annotation.
  - Sync cursors never move backwards, so a parser fix does **not** repair
    rows that are already synced. There is no rewind tool yet.
  - About 33% of empty head/tail completions is normal.
- **Processor:** `worker.py` imports `torch` before anything else, because
  fishsense-core preloads libnvblas and that crashes torch's CPU code. Don't
  move it into a package `__init__`, which the Temporal sandbox re-imports.
- **Verify the real path.** Run code that has never executed once with real
  inputs before shipping it. Reproduce problems through our own code, not a raw
  client. Judge results, not statuses: read the child workflow's result and the
  rows it wrote, not "RUNNING" or a rollout timeout.

## Measurement work

- **A mission's camera port is `unknown` unless something says otherwise**
  (migration 0047; the owner's rules, 2026-10-16). Data ingested before the
  rules, and any device labeled with the `D` suffix (`FSL-10D`), is the
  corrective optic. An operator or a lens calibration can state a port.
  The `D` counts only in file names (the path), never in the EXIF Artist,
  which is free text typed into the camera. Nothing else counts: a missing
  `D` does not mean flat, and lab notes are not evidence. Never write a port without its
  `port_source`. Only `corrective_optic` is processed. Every stage admits
  missions through one of `camera_sql`'s three terms, never its own check:
  - `RECTIFIABLE_MISSION`: it rectifies frames.
  - `ADMITTED_PORT`: it projects through a possibly borrowed calibration, so
    it reads this term in its resolver and persist check too.
  - `CORRECTIVE_OPTIC`: it only acts on what those stages produced.

  A change to these terms needs a migration that recreates
  `dive_pipeline_status`'s function (see 0050).
- **A mission's purpose is what it was for, never whether it calibrates**
  (0048). Nearly every dive has a calibration board in it. Never select laser
  calibration by purpose; find the board in the frames.

Calibration, accuracy and validation claims follow the user's rigour rules:
- every estimator gets a control with a known answer;
- count observations against unknowns before computing;
- state what each fit absorbs;
- report what was measured, against what, the sample size, the control, and
  what the test cannot see;
- correct earlier numbers in the open.

Excluding a dive needs direct evidence of error. A new gate must be proven
across every dive before it ships. Old exports and fixtures are not benchmarks.

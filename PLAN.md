# FishSense Services — v2 Plan

## 1. Purpose

FishSense is scaling along two axes at once:

1. **Multiple tenants** — no longer one lab. We serve our own team, a test/staging
   environment, and external **partners and customers**. Data and access must be
   isolated per tenant. FishSense itself becomes a **"customer" of the phone-cluster
   platform by end of 2026**.
2. **Multiple device types** — length data now comes from several kinds of hardware,
   with more coming.

`fishsense-services` (this repo) is the **v2 backend** that supersedes the current
single-tenant `fishsense-api`. It must **receive, process, store, and serve** capture
data from all device types, support **login for the mobile and web apps**, and enforce
**multi-tenant** isolation. The driving requirement is a **fundamental restructuring of
how we receive and store data** — tenant-scoped, device-agnostic, reproducible.

### Devices

| Device | Status | Description |
|---|---|---|
| **FishSense Lite** | Current | Fixed laser + camera (Olympus **TG-6**); laser-to-camera geometry gives distance, calibration estimates length underwater. SD-card offload — **no live user at capture**. |
| **FishSense Mobile** | Current | iPhone Pro (+ Android via Flutter); **LiDAR** above water; on-device measurement (Rust ML). |
| FishSense Lite — flat port | Research (wuwnet) | Same TG-6 + laser, **air lens removed**: the camera sits behind a bare flat pane, so it is an **axial (non-central) camera**, not a pinhole. Calibrated **in air** (dry, commodity 3-D target) + an analytic per-pixel refraction correction driven by the water index; geometry read in **raw sensor coordinates** (the TG-6's JPEGs carry an in-camera lens warp). Laser calibrated from the dive's own dots + a rigid object's apparent size — no slate. §2.7. |
| FishSense Mobile (Multilens) | Future | Multiple lenses, length without LiDAR, above water. |
| FishSense Mono | Future | Built on Lite; **ML monocular** depth. |
| FishSense Scout | Future | ROV / camera-trap on FishSense Mono. |

## 2. Current system (prior art, from repo reads)

*Snapshot: `fishsense-lite` @ `a8b2c3bc` (2026-09-21), `fishsense-core` v4.0.0 (`78d6814`).
v1 keeps changing until its pre-cutover freeze (§6) and moves fast (~500 commits between the first draft of this plan
and this snapshot) — re-verify a detail before building on it. v1's own `CLAUDE.md` is its
most current doc; `docs/measurement_pipeline.md` is stale. Research repos (§2.7):
`wuwnet-fishsense2026` @ `440c6a4`, `imwut_2026_fishsense_lite` @ `64c08bf`. Mobile and
krg-infra were **not** re-read for this snapshot (still as of 2026-07-22).*

The current stack is single-tenant and **production-only (no staging)**. It is the
system v2 replaces at a big-bang cutover, and the code v2 ports (§6) — not a clean slate.

### 2.1 `fishsense-api` (API v3.6.0, SDK v2.4.1)
FastAPI + SQLModel + asyncpg, Postgres, Alembic (startup: `create_all` → `alembic upgrade`
→ rebuild views → seed reference data). Tables:

| Group | Tables |
|---|---|
| Core | `user, camera, cameraintrinsics, dive, diveframecluster(+imagemapping), diveslate, divelaserline, fish, species, image, measurement` |
| Labels (human, via Label Studio) | `laserlabel, headtaillabel, diveslatelabel, specieslabel, labelstudiosynccursor` |
| Model output | `laserprediction, slateprediction, headtailprediction` |
| Calibration & derived | `laserextrinsics, laserdepth` |
| Global reference (seeded on boot) | `fishmodelreference, calibrationtarget` |

Plus four SQL views that Superset reads directly (§2.6).

**Findings that drive v2:**
- **Zero auth in the API** — the only dependency is `get_async_session`; it trusts a
  forward-auth proxy and returns every row unfiltered.
- **No `tenant_id` anywhere.** No device abstraction (`camera` = `serial_number` + `name`).
- **Nothing is append-only.** `measurement` is a destructive upsert on `(image_id, fish_id)`
  (plus a `DELETE` for species relabels). `laserextrinsics` — append-then-latest when this plan
  was first drafted — is now **unique per dive and overwritten in place** (2026-08-01). The
  prediction tables upsert per image.
- **…but derived rows name their inputs.** `measurement.laser_extrinsics_id`,
  `laserdepth(laser_label_id, laser_extrinsics_id)`; the selectors pick rows whose recorded
  input no longer matches the current one, so a recompute **drains on its own**. This is v1's
  working reproducibility model, and v2 should build on it (§4.6). Known gaps:
  - an in-place refit keeps the row id, so the mismatch never fires;
  - `laserextrinsics` records neither its producer (slate vs checkerboard) nor its residual;
  - `measurement` names no label ids and no algorithm/model/core version;
  - `laserlabel` cannot tell a gate auto-accept from a human accept after the fact.
- **Model provenance exists on predictions only** — `predictor_version`, `checkpoint`,
  `core_version` on the prediction tables (latest row only).
- **State v2 must represent** (not in the v2 model yet — §9.16):
  - labels: `superseded` vs `needs_reprocess` (re-render the JPEG in place) vs **sentinel**
    rows (no LS project) — three distinct states every reader must honour;
  - dives: `Priority` **LOW / HIGH / NONE** (NONE = deliberately parked) + `notes`;
    **calibration refusals** (columns on `dive`, expire when a newer label arrives);
    **borrowed calibration** (`calibration_dive_id`);
  - images: the same frame can sit under several dives; one copy is `is_canonical`
    (partial unique index on checksum).
- **Selectors are `ORDER BY id LIMIT 1` over all HIGH dives** (~17 `select-next/*` /
  `needing-*` endpoints) → head-of-line blocking. A child that "completes" with `computed: 0`
  never clears its condition: dive 32 blocked 49 dives.
- **The API never handles image bytes and never presigns** — `image` stores `path` +
  `checksum`; Label Studio presigns reads with its own read-only key. Config: Dynaconf,
  `E4EFS_` prefix.
- **A typed SDK already exists**: hand-written httpx clients over models codegen'd from the
  API's OpenAPI, with drift/contract tests. Worker DTOs live in
  `libs/fishsense-shared/preprocess_contracts.py` (~560 lines, incl. checkerboard and
  auto-accept) — prior art for §9.1.

### 2.2 Workers (Temporal, mTLS to shared `krg-prod`, namespace `fishsense`, CN `fishsense-worker`)
- **`fishsense-api-workflow-worker`** (queue `fishsense_api_queue`) — owns **21 hourly
  Temporal Schedules**: 17 staggered with `overlap=SKIP` + 4 Label-Studio syncs
  (`ALLOW_ALL`). Selectors filter `priority=HIGH`. Runs LS create/populate/sync, the laser
  auto-accept gate, stages raw `.ORF` / slate PDFs into Garage scratch, and **scales the four
  data-worker Deployments 0↔N** (idle sweep, 15-min cooldown, time-bounded GPU→CPU
  fallback). `ensure_schedule` still never updates in place (delete + redeploy).
- **Ingest is operator-run** — `temporal workflow start IngestDiveWorkflow` over an **NAS
  folder** (one dir per dive) → preflight → create dive at LOW → download + **md5** each ORF +
  register `image` rows (server sets `is_canonical`) → finalize promotes to the requested
  priority (**priority is the commit flag**). Checksum-audit workflows re-hash on demand.
  **No user upload and no presigned PUT exist anywhere.**
- **`fishsense-data-processing-workflow-worker`** — three queues across **four NRP
  Deployments**, all `amd64`:
  - `…_queue` (cpu): per-image ORF decode/preprocess, checkerboard calibration;
  - `…_gpu_queue` (gpu, `nvidia.com/gpu: 1`, SM ≥ 7.5; plus a CPU-fallback Deployment):
    laser + head/tail prediction;
  - `…_light_queue`: clustering, laser calibration, laser depth, measure, label validation,
    auto-accept.

  Imports **`fishsense-core` v4.0.0** (GitHub release wheel, **cp313 only** — torch has no
  cp314 wheel; extras `laser-detector,slate`). Owns **zero** schedules (scale-to-zero safe).
- **`fishsense-backup-worker`** — nightly 03:00 UTC `pg_dump` of `fishsense` + `superset` →
  **NAS**, retention 14.

### 2.3 Models (no MLflow anywhere in v1)
- **Laser detector** (ResNet-34 checkpoint) — **baked into the image** from Hugging Face
  `ucsde4e/fishsense-laser-detector`. `LASER_PREDICTOR_VERSION=2`.
- **SAM 3.1** (head/tail, laser-centred crop) — pulled from Garage bucket **`model-weights`**
  at `{name}/{version}/{filename}`, version pinned in config, volume-cached.
  `HEADTAIL_PREDICTOR_VERSION=2`.
- **`fishsense-core` `FishSegmentation`** (ONNX, weights embedded in the `.so`) — CPU fallback.
- Slate `BoardMasker` detector **retired** 2026-08-03.

### 2.4 Labeling & calibration
- **Label Studio is the hosted Enterprise SaaS (`app.heartex.com`)** — one workspace shared
  with unrelated projects (e.g. Coral Gardeners), **one LS project per dive per stage**.
- **Model-assisted:** laser (predict → auto-accept gate fits the dive's own line;
  auto-accepted frames land as `ground_truth: false`), head/tail (SAM 3.1 pre-annotations),
  species (pre-annotated from stored human judgements — not a model).
- **Still human, required:** head/tail review of **every** frame; **all** species labeling
  (grouping / exclude / top3 are never pre-annotated); dive slate; laser frames the gate
  declines or audit-samples; stage 6.1 regroup (on demand).
- **Two calibration producers** of `laserextrinsics`: slate labels (stage 13) and
  **checkerboard** (added 2026-09-07). Four gates — observation geometry → self-consistency →
  baseline 9.7–14.5 cm → describes-dive; an implausible calibration counts as none and is
  recorded as a refusal on the dive.
- Stage map: DIAGRAMS §8.

### 2.5 Libraries
- **`fishsense-core`** (v4.0.0; we own it) — Rust + PyO3: laser calibration, world point,
  fish geometry / head-tail, PCA, plane fit, segmentation, length. **Preprocessing (raw decode
  → auto-gamma → contrast stretch → CLAHE → undistort) is still Python** (rawpy/cv2/skimage;
  CLAHE and the new underwater enhancements are off by default). `RectifiedImage` (and
  `_laser_detector.py`) **still import `fishsense_api_sdk`**, now behind an optional
  `rectified` extra — partly untangled (§5).
- **`pixel-finch`** (we own it) — pure-**Rust** Pixel-Fold **Edge TPU** driver from Debian
  userspace (WIP). No Python bindings yet; cheap to add.

### 2.6 Storage, analytics, web, mobile, infra
- **Garage** (`s3.e4e.ucsd.edu`, path-style) — **three buckets**, provisioned by krg-infra
  with **per-worker grants**:
  - `fishsense-lite` — scratch: `raw/{checksum}.ORF`, `slate_pdf/`; deleted after use;
  - `labels-fishsense-lite` (prefix `fishsense-lite`) — **durable** processed JPEGs, presigned
    by Label Studio;
  - `model-weights` — checkpoints.
- **NAS** (Synology) is load-bearing **three ways**: durable raw `.ORF` archive, **ingest
  source**, and **backup target**. **Decision: NAS is out, Garage is the forward path** (§4.6,
  §9.15).
- **Superset 6.0** (in the compose stack) connects as a `superset` role **directly to the
  `fishsense` DB** via the views; dashboards `Fish_Measurements`,
  `FishSense_Pipeline_Status`.
- **Web** — `apps/fishsense-lite-web`: Next.js 15.5 + **next-auth v5** (Authentik OIDC, JWT
  session). The portal is **gated fail-closed** on `PORTAL_ALLOWED_GROUPS`
  (`FishSense-Prod-Admins` in prod) — coarse, single-group, but enforced. **Not read-only:**
  writes to the API (calibration source) and to Label Studio (triage accept/undo). Calls the
  API via interior basic-auth; the image proxy allowlists its hosts.
- **Mobile** — `fishsense-mobile`: Flutter; captures image + LiDAR + on-device
  measurement (Rust); local SQLite; "cloud sync" stubbed, **not wired to a backend**.
- **Identity/infra (`KastnerRG/krg-infra`)** — **Authentik** (Terraform IaC, AD-backed
  via LDAP). The invitation/enrollment flow (ADR 0013) is **built in PR #504 — merged, not
  yet applied to live Authentik** as of 2026-07-22 (§4.2). Also runs **MLflow, Temporal,
  OpenBao, Traefik**. Substrate is **x86 Proxmox + Incus + Docker-Compose + NixOS — no
  Kubernetes, no ARM64.** The only k8s is the data-worker kustomize in `fishsense-lite`,
  targeting **NRP** (external).

### 2.7 Research repos — the other consumers of v1 data
The paper repos are where calibration and measurement methods are developed **before**
(and sometimes instead of) production. They drift from v1 in method, in constants, and in
how they read data. v2 must serve them as first-class consumers, not break them.

- **`imwut_2026_fishsense_lite`** (P1 — Lite accuracy, 2,927-measurement pool corpus):
  - *Reads prod as superuser* — raw `psql -U postgres` over SSH into the Incus container, or
    `pg_restore` of the nightly dump. `sql/extract_*.sql` hit `measurement, image, dive,
    specieslabel, laserextrinsics, laserdepth, laserlabel, headtaillabel, cameraintrinsics,
    fishmodelreference`, with **v1 dive ids hard-coded** in SQL, tests and figures. Exports
    (`corpus.csv` + dated frozen copies, `laser_labels_cleaned.csv`) are hand-run psql
    output; the frozen CSVs are the **only record** of pre-refit states. Some prod fixes
    (dive split, un-supersede) were hand-written SQL.
  - *Methods not in prod:* the **accuracy-cohort rule** (design exclusions → range-trend
    filter → median polish over (dive, model) p90 cells), measured-reference overrides, φ
    repair, a borrow map. *Ported to prod:* range trend (audit script only, not a gate),
    baseline floor 9.7 cm, robust trimming, lever-arm and describes-the-dive gates.
  - *Identity drift:* target identity comes from `split_part(content_of_image, ', ', 2)`
    joined to `fishmodelreference.name` **by string**; prod's view joins through `fish.name`.
    Live reference values have **drifted from the seeds** (Weasly Fish 0.310 → 0.313, Ruler
    0.3429 vs 0.341, checkerboard pitch 0.042 vs 0.04217) with no history.
  - *Needs that prod doesn't store:* session/era/pool, designed yaw, diver, per-frame pose,
    an "accuracy evidence" flag + the rule version that set it, repeatability grouping.
    `HANDOFF_TO_P2.md` adds: **segmentation masks**, pose-from-mask, frames-per-animal,
    volunteer identity.
  - Pins `fishsense-meta` @ `e4167bf` (2025-12, pre-v0.2); corpus analysis re-implements the
    geometry in numpy rather than calling core.
- **`wuwnet-fishsense2026`** (in-air calibration without an in-water reference):
  - *Camera:* calibrated **in air** + per-pixel **flat/dome-port refraction** correction
    (Pinax / exact axial model), with a **water index from salinity, temperature and
    wavelength**. Prod has **no port or refraction concept**; in-water checkerboard
    intrinsics silently absorb the port.
  - *Laser:* calibrated from the **dots alone** + the **apparent size of any rigid object**
    (unknown true size) + a bench-measured laser origin `|O|` — no slate, no board. Prod's own
    docstring says the dots can't fix the in-plane angle; this method supplies it.
  - *Targets:* a LEGO brick tower read from a versioned 3-D model file; a roll test shows the
    E4E board is **~0.7 % anisotropic**, which prod's scalar `square_size_m` can't express —
    the fleet fx/fy ≈ 0.991 is probably target-side bias baked into every prod intrinsics row.
  - *Other drift:* outlier rule (3× median residual vs prod's 3× scaled-MAD + floors), raw
    decode (`bayer_upsample="bilinear"` vs core's `"repeat"` default). Reads raw frames from the
    NAS mount; everything else from committed `data/*.npz` caches exported once without a
    recorded query. No handoff to core/lite yet — the method goes to P2 as paper text.
- **`cscw-fishsense2027`** (P2 — citizen-science deployability) reads imwut's `corpus.csv`,
  `field.csv` and `laser_labels_cleaned.csv` **by relative path**. (Not read in depth.)

## 3. Locked decisions

| Area | Decision |
|---|---|
| **Role of this repo** | **The monorepo.** v2 is built here, v1's pipeline is ported in, and at cutover the fishsense Incus slot is repointed to build from this repo (§6, §9.9). |
| **Delivery strategy** | **Big-bang cutover on the existing fishsense Incus slot** *(decided 2026-09-23; supersedes the strangler plan)*. v1 keeps changing until a ~2-week freeze; v2 reaches parity and rehearses the migration, then replaces v1 over one weekend, with a 48 h rollback window. See §6–7. |
| **Tenancy** | **Single Postgres DB**, `tenant_id` on all domain rows. App-layer mandatory scoping **+ Postgres RLS** backstop. Users ↔ tenants **many-to-many**; a capture/dataset may be associated to multiple tenants. |
| **Identity** | **Authentik OIDC** for **web (confidential)** and **mobile (public + PKCE)**. Lab users via AD-backed Authentik. **External partners via tenant-scoped invite links → Authentik-LOCAL `external` accounts** (not AD) — **built in krg-infra PR #504** (flow `krg-collaborator-enrollment`). Tenant/org arrives as an **`org` OIDC claim**; **v2 clients must request the `org` scope**. Per-org isolation stays **app-side** (§4.2). |
| **Authorization** | v2 API **validates the OIDC/JWT in-app** and derives tenant from **its own** membership + RBAC tables, keyed on the stable `sub` (`hashed_user_id`), **not email**. Drives the RLS session variable per request. |
| **Device model** | Shared **core `Capture`** + **per-`DeviceKind` `CaptureExtension`** seams. Support Lite + Mobile now; design seams for Multilens / Mono / Scout. |
| **Lite ingestion** | **User-uploaded dive batches** attributed to uploader's tenant + user; the TG-6 is **metadata**, not an identity. |
| **Storage** | **The e4e Garage is the single durable, tenant-partitioned source of truth. NAS retired.** Three *independent* Garages exist (e4e / krg / phone-cluster); **e4e owns the data** → v2 starts on e4e, **may migrate to the phone-cluster Garage later** (an explicit data move, not sync). *(The NAS is also v1's ingest source and backup target — retirement order is §9.15.)* |
| **Reproducibility** | Raw capture = durable truth. **Measurements are append-only and versioned** by `algorithm + version + run_id + core_version + model_version`, and **name every input** (calibration, labels) — v1's input-naming + mismatch-recompute model (§2.1), plus history. Device-provided measurements stored alongside raw. *("Current" semantics open — §9.13.)* |
| **Models** | Processor is **model-heavy** (laser detector, SAM 3.1 landed in v1). Models are **versioned source-of-truth inputs**; every measurement records the model version. Registry: **MLflow (already in krg) backed by Garage** — *under review: v1 versions weights in a plain Garage `model-weights` bucket, not MLflow (§9.12)*. §4.7. |
| **Language — app tier** | **Python** — API + orchestrator + data-worker (thin shell), FastAPI. Rationale and the TypeScript alternative: §3.1. |
| **Data layer** | **SQLAlchemy 2.0 typed models** (`Mapped[]`) + separate Pydantic schemas + asyncpg + Alembic — **not SQLModel** (a deliberate departure from v1): RLS needs explicit control of the transaction and `SET LOCAL`, and v2 must not import v1's model classes (§3.1). |
| **Development practice** | **Always TDD.** Every change starts with a failing test, then the minimum code to pass it, then refactor. No production code without a test that demanded it. Isolation and RLS are tested against **real Postgres** (not mocks or SQLite), because the property under test lives in the database. |
| **Language — libraries** | **Rust** (`fishsense-core`, `pixel-finch`) via **PyO3 bindings**. All real logic lives here. |
| **Language — web** | **TypeScript** (Next.js). |
| **API↔frontend typing** | **Generated from the FastAPI OpenAPI schema, done right**: `openapi-typescript` (types) + `openapi-fetch` (tiny typed client) + **zod** for runtime validation at the boundary (zod schemas must themselves be generated, not hand-kept). Requires cleaning up FastAPI `operation_id`s. v1's Python SDK already codegens models from OpenAPI with drift tests — reuse that CI pattern. (Not `openapi-generator` — the class-soup output that soured the v1 attempt.) tRPC-style *inferred* types are unavailable because the API is Python. |
| **Ruled out** | **Go** (nobody in-org), **Rust for the app tier** (Rust talent is CV/systems), and **TypeScript for the app tier** (for now — §3.1 has the case both ways and the triggers to revisit). |
| **Processing** | Keep the **two-worker split** (Temporal **workflow** orchestrator + **activity** processor). **Processor floats** — NRP today, phones later — and is model/GPU/TPU-capable. |
| **Deployment** | **Control plane + state** (API, orchestrator, Postgres) → **the existing fishsense krg Incus slot, Docker-Compose** (replacing v1 there at cutover, §6; *no kube*). **Processor** → **Kubernetes**: NRP `amd64` now → junkyard/Pixel-Fold **ARM64** later. **Garage** and **Temporal** are external/shared. **ARM64/Knative are processor-only, future concerns.** |

### 3.1 Why Python, not TypeScript, for the app tier *(decided 2026-09-23)*

The processor is **forced to Python**: `fishsense-core`'s bindings are PyO3, the models are
PyTorch (SAM 3.1, the laser detector), and preprocessing is rawpy/cv2/skimage. The real
question was the **API and orchestrator**.

**For TypeScript:**
- The API needs nothing from Python: it never touches bytes or `fishsense-core`. Tenancy,
  authz, CRUD, presign and enqueue are TypeScript's home ground.
- One language with the Next.js web app, shared zod schemas and inferred end-to-end types
  (Hono RPC / ts-rest) would remove the OpenAPI codegen pipeline that soured v1.
- The TypeScript Temporal SDK is arguably the strongest (a V8-isolate determinism sandbox),
  and Temporal is designed to be polyglot: TS workflows can dispatch to Python activity
  queues along the orchestrator/processor seam v1 already has.
- A language break would *enforce* the strangler discipline: v2 couldn't import v1's
  models, only the language-neutral contract §9.1 already demands.
- Explicit SQL builders (Kysely, Drizzle) keep the transaction and `SET LOCAL` visible, and
  SQLModel is a leaky layer for RLS.
- Web-facing contributors tend to write TypeScript.

**Against:**
- **The orchestrator is where v1's hard-won, weekly-churning domain logic lives**: the
  auto-accept gate, Label Studio populate/sync, sentinel handling, the drain and wedge rules,
  k8s scaling, and ~17 selector endpoints of careful SQL (v1's selectors live in the *API*).
  A TypeScript v2 would port all of it and keep re-porting as v1 changes, turning
  "v2 **inherits** PhD progress" into "v2 **chases** it" (§6).
- **There are two backend languages regardless**, because the processor is Python: two
  toolchains, CI pipelines, dependency ecosystems and image builds. The contract is still
  generated into both, so codegen moves rather than disappears.
- **Inferred types only help the smallest client.** Mobile is Flutter/Dart and needs an
  OpenAPI spec and a generated client anyway; the web app is an admin/review portal.
- **The people are Python people**: the lab, the PhD, three research repos, the backfill and
  the research DB role (§9.20). In a student lab with turnover, a TypeScript backend in a
  Python science org has a bus factor of whoever wrote it.
- The TypeScript wins are mostly available in Python. The v1 codegen failure was
  `openapi-generator` specifically. SQLAlchemy 2.0 gives explicit session control. Temporal's
  Python sandbox already runs v1's 21 schedules. Contract-first is discipline, not language.

**Decision: Python for the API and orchestrator; TypeScript only for the web.** It turns on
the first "against" point: TypeScript wins clearly only on the API, but the API and
orchestrator share the domain logic, so a TypeScript API alone would split the domain model
across two languages, and a TypeScript orchestrator would chase v1.

*Note (after the big-bang decision, §6):* the argument above was written for the strangler
plan, but the move to a big-bang cutover strengthens it. v2 reaches parity by **porting v1's
pipeline code** into this repo (§6.3), and that port is only cheap because both sides are
Python.

**What the TypeScript case earned — binding commitments on the Python stack:**
- **Contract-first, language-neutral schema** (§9.1). v2 does not import v1's models. It
  ports logic deliberately, and shared rules move into a library both versions depend on.
- **SQLAlchemy 2.0 typed models, not SQLModel**, for explicit transaction and `SET LOCAL`
  control.
- **Generated, drift-tested clients**: `openapi-typescript` + `openapi-fetch` + generated zod
  for web, a generated Dart client for mobile, and a CI check that fails when spec and
  clients diverge.
- **Temporal's Python workflow sandbox stays strict**, never disabled.

**Revisit if** the API's main maintainers turn out to be web developers rather than lab
researchers; or v1's domain logic has settled into shared libraries and stopped churning; or
the orchestrator is being rewritten anyway (e.g. the native-Rust processor move). At that
point a TypeScript API + orchestrator over Python activity queues becomes the better trade.

## 4. Architecture

```
   Web (TS/Next.js, confidential)   Mobile (Flutter, public+PKCE)
        │        Authentik OIDC            │
        └───────────────┬──────────────────┘
                        ▼   validates JWT in-app → sets RLS tenant_id
        ┌─────────────────────────────────────┐
        │  API (Python/FastAPI)  — Incus/Compose │  control plane + state
        │  tenant scoping · RBAC · RLS · ingest  │
        └───────┬───────────────────────┬──────┘
        presigned│ up/download    enqueue│
           (Garage)                (Temporal, shared krg cluster, mTLS)
                 ▼                        ▼
   Garage (durable, tenant-        Orchestrator (Python) — workflow worker
   partitioned: raw + processed    tenant-aware select-next schedules
   + model artifacts)                     │ activity
                                          ▼
                    Processor (Python thin shell → Rust libs)  ── FLOATS
                    fishsense-core + pixel-finch (bindings) + registry models
                    NRP amd64/GPU today · phones ARM64/TPU later
                                          │
                                          ▼
                    Postgres (append-only, versioned results)
```

### 4.1 Multi-tenancy
- Single Postgres; every domain row carries `tenant_id`.
- **Two isolation layers**: (a) mandatory tenant scope in the data-access layer; (b)
  **Postgres RLS** keyed on a per-request `tenant_id` session var — backstop against a
  missed scope (critical: partners share the DB).
- Users ↔ tenants **many-to-many** (`memberships` with roles); cross-tenant sharing via
  an explicit association table, not duplication.
- A dedicated **test/staging tenant** fixes the current "production-only, no test env"
  gap — same code path, isolated data.
- **Existing authz to preserve is coarse**: v1's web portal is gated fail-closed on one
  Authentik group (`FishSense-Prod-Admins`); the API itself enforces nothing. v2's lab-tenant
  admin role must cover what that group can do today (calibration-source edits, LS triage).
- Unresolved mechanics — active tenant for multi-membership users, shared captures under a
  single-`tenant_id` RLS policy, pooled-connection `SET LOCAL`, the app DB role — are §9.10.

### 4.2 Identity & authorization
- **Authentik OIDC** is the single IdP. Reuse existing Terraform patterns in
  `krg-infra/terraform/authentik`: **web = confidential** (copy `fishsense_oauth`),
  **mobile = public client + PKCE** (copy the `incus` provider), **workers/machine =
  app-password service account** (copy `fishsense_data_worker.tf` / `svc_fishsense`).
  Load-bearing fields: `signing_key`, `grant_types = [authorization_code, refresh_token]`,
  strict redirect URIs.
- **Lab users**: AD-backed Authentik accounts.
- **External partners/customers**: **tenant-scoped invite links → Authentik-LOCAL accounts**
  — **BUILT** in `krg-infra` **PR #504** (`collaborator_enrollment.tf`,
  `fishsense_collaborators.tf`; merged, **not yet applied to live Authentik**).
  - Flow `krg-collaborator-enrollment`: `invitation → prompt → user_write (external,
    inactive) → email verify/activate → user_login`. **Invite-only**
    (`continue_flow_without_invitation = false`), email-verified, `user_type = "external"`
    → **no AD account**.
  - **Tenant/org rides on the invite's `fixed_data`** → persisted as hidden user
    `attributes.*` → both **gates** the apps (expression policy OR-ed with the AD-group
    binding, `policy_engine_mode = any`) **and is emitted as an `org` OIDC claim**.
  - **Attribute/claim-driven, not per-org Authentik groups** — "per-org isolation stays
    app-side," which matches our model exactly (we key tenants on the stable `sub`).
  - **Requirements this places on v2:** (a) our OIDC clients **must request the `org`
    scope** to receive the claim; (b) v2's apps must be added to
    `local.fishsense_collab_targets` or collaborators can't reach them — and we must decide
    deliberately whether partners reach the **v2 API** (v1 currently binds all three apps
    incl. the orchestrator API); (c) **invites are minted out-of-band** via the Authentik UI
    (no `authentik_invitation` provider resource), so **self-service invite generation from
    our web app is not available today** — it would need Authentik API calls (§9.3).
- **Authorization is owned by this service**, not Authentik: validate the JWT (the
  forward-auth proxy already emits `X-authentik-jwt`), map the stable `sub` →
  `users`/`memberships`/roles, set the RLS `tenant_id`. Authentik groups/claims are
  hints, not the source of truth. **Never key identity on email** (krg convention:
  emails are admin-mutable).

### 4.3 Device abstraction & data model
Core model shared by all devices; a typed extension per kind so new devices add a
table, not a rewrite. Maps onto existing tables where possible.

| v2 entity | From current | Notes |
|---|---|---|
| **Tenant** | *(new)* | customer / partner / team / test. |
| **User** + **Membership(role)** | `user` | `user` today is a Label-Studio identity only; add auth principal + membership. |
| **DeviceKind** | *(new enum)* | `lite \| lite_flatport \| mobile \| multilens \| mono \| scout`. `lite_flatport` shares Lite's hardware, but its **camera model** (axial, refractive) and **calibration workflow** (in air, no slate) differ — that, not the hardware, is what makes it a kind. (Kind vs configuration of `lite` — §9.21.) |
| **Device** | `camera` | add `kind`; Lite = TG-6 metadata. |
| **Device component** *(new)* | *(none)* | Housing/**port** (flat / dome / layered; pane thickness, glass index, standoff, dome radius/decentre), removable **air lens**, laser mount with bench-measured origin `|O|`. Components change independently of the camera serial (an air lens is re-fitted per dive), so a dive records **which configuration** it used. Required by the in-air calibration method (§2.7). |
| **Session / Experiment** *(new)* | *(none — lives in research repos)* | Groups dives by protocol (field / pool / designed-angle), site, era, diver; carries per-dive research flags (design-excluded, accuracy evidence + the rule version that set it, split-from). Lets the cohort rule stay research code while its inputs live in the data model. |
| **Capture** (core) | `image` (+`dive`) | tenant, uploader, device, timestamps, geo, environment, **pointer to raw in Garage**, content checksum (v1: md5 of the whole ORF; duplicates across dives → `is_canonical`). Reproducibility anchor. |
| **CaptureExtension** (per kind) | `cameraintrinsics` (Lite) | Mobile: LiDAR depth + ARKit meta; Mono: model inputs. |
| **Dive / Batch** | `dive`, `diveslate`, `diveframecluster(+mapping)` | Lite offload session. Carries v1's `priority` (LOW/HIGH/NONE) + `notes`. |
| **Camera calibration** *(new)* | `cameraintrinsics` | Per device configuration, **append-only**: **camera model type** (`pinhole` \| `axial_refractive`, extensible), medium (**air / water**), coordinate frame (**raw sensor** vs JPEG), target + target-geometry version, RMS, **port/refraction model + version**. Research shows the two halves must be modeled separately — each camera model calibrates its own laser (§2.7). |
| **Laser calibration** *(new as a first-class entity)* | `laserextrinsics`, `divelaserline`, refusal columns on `dive`, `calibration_dive_id` | Per dive, **append-only**, names its camera calibration; records **producer** — `slate`, `checkerboard`, and the research producers `dots+range`, `dots+two_ranges`, `dots+apparent_size`, `bench` — plus lever arm, observation count, conditioning, residual, each gate verdict, and post-hoc verdicts (range trend); a refusal is a calibration outcome, not dive columns. Borrowing — §9.17. |
| **Dive conditions** *(new)* | *(none)* | Per dive: water salinity, temperature (or a fresh/brackish/sea preset) → the water index used, and its source. Input to refraction correction. |
| **Prediction** *(new)* | `laser/slate/headtailprediction` | Model output **before** human review: `model_version`, `checkpoint`, `core_version`, gate verdict. Append-only. |
| **Derived geometry** | `laserdepth` | Names its inputs (label, calibration) like `Measurement`. |
| **Measurement** | `measurement` | **append-only + versioned**, names its inputs (§4.6). |
| **Label / Annotation** | `*label`, `fish`, `species` | preserve LS-sync provenance; add **source** (human / gate auto-accept / model pre-annotation) — v1 cannot recover it. Carry `superseded` / `needs_reprocess` / sentinel as distinct states. |
| **Reference data** | `species`, `fishmodelreference`, `calibrationtarget` | **Global, no `tenant_id`**, seeded — and **versioned** (value, method, measured_at, supersedes, `is_provisional`): v1's live values have silently drifted from its seeds (§2.7). Targets carry **per-axis pitch or a 3-D model file + checksum**, not one scalar. Measurements reach target identity by **foreign key**, never by label string. |

Everything except the reference tables above carries `tenant_id`.

**Schema conventions** *(decided 2026-09-24; enforced by tests)*:
- **Isolation is audited, not remembered.** `schema_audit.tenancy_violations` classifies
  every table: tenant-scoped (the default: non-null `tenant_id` → `tenants`, forced RLS, a
  read+write policy on `app.tenant_id`), caller-scoped (`users`, `memberships`, `tenants`),
  or global reference (read-only to the app role). The app role owns nothing. `migrate`
  fails the deploy on any violation.
- **Same-tenant references are enforced by the database.** A tenant-scoped table
  references another through a **composite foreign key** `(tenant_id, parent_id) →
  parent (tenant_id, id)`, so a row can never point into another tenant, even when written
  as the owner.
- **Ids:** `uuid` primary keys. Migrated rows keep **`v1_id bigint UNIQUE`** (PLAN §6.4).
- **Enumerations:** `text` + `CHECK`, not Postgres enums, which are painful to evolve. v1's
  enums are nullable at the DB level; v2's are `NOT NULL` with explicit defaults.
- **JSON:** `jsonb`. **Timestamps:** `timestamptz`.
- **Deletes:** `ON DELETE CASCADE` only from `tenants` (removing a tenant removes its
  data). References within a tenant are `RESTRICT` (v1 had no ON DELETE rules at all), so
  a delete can never silently take history with it.
- **Measured reference values are versioned** by `valid_from`, never edited; `current_*`
  views give the latest.
- **Models mirror migrations.** Migrations are hand-written (policies and grants), the
  typed `models.py` mirrors them, and a drift test compares the two.

### 4.4 Ingestion flows
- **Lite (TG-6)** — batch/offline: a lab member offloads the SD card, uploads a
  dive/batch attributed to **their tenant + user**; raw images (+ slate, calibration,
  camera metadata) go **direct-to-Garage via presigned URLs**. This is a **new capability,
  not an evolution**: v1 ingest is operator-run from an NAS folder (§2.2), no presigned PUT
  exists, and the API never presigns. Keep v1 ingest's semantics — content checksum,
  cross-dive duplicate detection, reject frames without a timestamp, and a commit step
  (v1 uses priority LOW → HIGH as its commit flag). Completion enqueues a Temporal
  workflow → processor activities → append-only `Measurement`s. Who verifies checksums when
  the API never sees bytes — §9.19.
- **Lite, operator path** — an operator-run bulk ingest (v1's `IngestDiveWorkflow` shape)
  is part of parity (§6.2): v2 ships it first, and user upload follows after cutover.
- **Mobile** — authenticated app sync from local SQLite: image + LiDAR depth +
  on-device measurement. Raw + depth → Garage; `Capture` + mobile `CaptureExtension` +
  device measurement recorded. Raw retained so we **recompute/validate** the device's
  measurement.

### 4.5 Processing pipeline
- **Orchestrator** (Temporal **workflow** worker, Python) — durable coordination; lives
  with the API. Owns the schedule chain. Runs in the **single `fishsense` namespace** with
  **in-workflow tenant scoping** (§9.4): `tenant_id` in every payload, **tenant-scoped
  workflow IDs**. **Make `select-next` selectors tenant-aware / fair-share** (+ per-tenant
  concurrency limits) so one tenant's backlog can't starve others. v1's selectors are
  `ORDER BY id LIMIT 1`, and one dive whose step "completes" without clearing its condition
  blocks every dive behind it (dive 32 blocked 49) — so v2 selectors also need an explicit
  **"tried, made no progress" state** and must skip it (§9.16). Fix the current
  `ensure_schedule` "never updates in place, must delete+redeploy" footgun (still present
  across v1's 21 schedules).
- **Human-in-the-loop stays**: labeling is model-assisted but still required (head/tail
  review of every frame, all species, dive slate, declined laser frames — §2.4). The pipeline
  waits on Label Studio; how that works per tenant is §9.14.
- **Processor** (Temporal **activity** worker) — the heavy CV/ML, calling `fishsense-core`
  (+ `pixel-finch` on phones) + registry models. **Python thin shell over the Rust libs**
  today; can flip to **native Rust** later as a phone-ops optimization — still a Temporal
  activity worker either way (Rust SDK is first-class now).
- **Scheduled / batch reprocessing is first-class** (Temporal Schedules): "run algorithm
  vX / model vY over all of tenant Z's captures." This is both the reproducibility
  mechanism and the PhD's active workflow — reads/writes the **stable data-contract**
  (§7) so scheduled tasks survive the restructure.
- **Processor floats & is GPU/TPU-capable**: NRP `amd64` today — v1 already runs CPU,
  light and **GPU** queues across four Deployments with a CPU fallback (§2.2); v2 inherits
  that split — phones ARM64/Edge-TPU later.
- **The processor is a cross-tenant principal** (one service identity, every tenant's
  payloads, running on infrastructure we don't own). What bounds it is §9.11.

### 4.6 Storage & reproducibility
- **The e4e Garage (S3) is the single durable, tenant-partitioned source of truth.** NAS
  retired — copy durable raw `.ORF` NAS → Garage after cutover, in §9.15's order (
  today's Garage `raw/` is scratch — v2 makes raw **durable + retained**). Presigned
  **uploads** (new) + reads; Garage **CORS** for browser flows; a lifecycle rule to abort
  abandoned multipart uploads.
- **"Tenant-partitioned" needs a mechanism.** Garage grants keys per **bucket**, not per
  prefix, so a tenant prefix is a layout, not an isolation boundary. v1 already splits by
  purpose with per-worker grants (`fishsense-lite` scratch, `labels-fishsense-lite` JPEGs,
  `model-weights`), so bucket-per-tenant is an ordinary extension — §9.11.
- **Three independent Garages** (e4e / krg / phone-cluster); **e4e owns the data**. v2
  pins to e4e as canonical. A later move to the phone-cluster Garage is an **explicit data
  migration between independent stores**, not replication. The **floating processor** reads
  the canonical (e4e) endpoint wherever it runs — cross-cluster from the phones (works; keys
  auth from any IP) but with egress, which is part of what would justify migrating the data.
- **Known reality:** the e4e Garage is currently **single-node, no backup**. Durability of
  the source of truth is being followed up **outside this plan** (owner-tracked, recurring);
  v2 must not assume Garage redundancy exists yet.
- **Measurements are append-only, versioned, and name their inputs.** v1 no longer has an
  append-only table to generalize (`laserextrinsics` became an in-place overwrite on
  2026-08-01). What v1 *does* have and v2 should adopt: derived rows record the id of each
  input (`measurement.laser_extrinsics_id`), and selectors recompute whatever no longer
  matches the current input, so a recompute drains on its own. v2 adds:
  - **history** — every row is appended, never overwritten. This also fixes v1's blind spot
    where an in-place refit keeps its id and the mismatch never fires;
  - **full provenance** — `algorithm + version + run_id + core_version (wheel) +
    model_version`, plus the ids of everything that fed it: **camera calibration, laser
    calibration**, labels, and (once refraction lands) the port/refraction model version and
    the water index used. **Raw-decode parameters** and **model checkpoint digests** count as
    inputs too — research and prod already differ on both (§2.7);
  - **as-of reads** — "the corpus as it stood on date D" must be a query, not a frozen CSV
    (today the imwut CSV snapshots are the only record of pre-refit results);
  - **"current" defined per `(capture, fish, source)`, or by explicit promotion**, not
    "latest by `created_at`" globally — otherwise a server recompute silently displaces a
    device measurement, and re-running an old version for comparison becomes "current"
    (§9.13).

### 4.7 Model / ML lifecycle *(new)*
- The processor is **model-heavy** and getting more so: v1 now runs a laser detector
  (ResNet-34) and SAM 3.1 head/tail, with `fishsense-core` ONNX segmentation as the CPU
  fallback (§2.3).
- **Models are versioned source-of-truth inputs** — same status as raw captures; to
  reproduce a length you need raw + calibration + `core_version` + **`model_version`**.
- **Two execution runtimes with different rules** *(hard constraint: mobile is offline)*:
  - **Server processor** (NRP / phone-cluster, online) — pulls the pinned model version
    from the registry at deploy/run time; ONNX/CUDA now, Edge TPU (via `pixel-finch`) later.
  - **fishsense-mobile** (field app, **offline**) — models **MUST ship in the app bundle**
    and run **on-device**; the registry is a **build-time** input, **never a runtime
    dependency**. Mobile records the shipped `model_version` in every measurement and syncs
    later. **Updating a mobile model = shipping an app release** (server models update
    independently).
- **Registry: MLflow (already in krg), artifacts on Garage** — the canonical versioned
  store, consumed at **build/deploy time**, not a mobile runtime dependency. *v1 went another
  way:* SAM 3.1 is versioned in a plain Garage `model-weights` bucket
  (`{name}/{version}/{filename}`, version pinned in config), and the laser detector is baked
  into the image from Hugging Face. Adopt v1's bucket as the registry, or migrate it —
  §9.12.
- **Each model version = one set of weights, two builds** under a single `model_version` —
  **server full-precision** (ONNX + CUDA/TPU providers) and a **mobile quantized** build
  (same is preferred; a quantized derivative is the likely reality). Provenance stays uniform
  whether a measurement was computed on the server or on a phone in the field.
- **Consumers pull selectively — no unused models shipped.** The server pulls its ONNX
  artifact; **mobile bundles only the mobile build for the models it actually runs** (its
  `DeviceKind`'s models), nothing else. The two-build set is the *registry's* holding, never
  any one consumer's payload — keeps the app minimal.
- **`fishsense-core` is the shared inference library for both server and mobile, and owns
  the on-device runtime/format.** The "load-model" seam lives there — two sources: **bundled
  file (mobile, offline)** vs. **registry pull (server)**. fishsense-mobile consumes
  `fishsense-core`; the mobile format choice is a `fishsense-core` internal, not a v2 concern.
  *Confirmed by repo read (2026-07-22; server side is now on v4.0.0):* mobile pins
  `fishsense-core v2.0.0` with `features = ["coreml"]` →
  on-device runtime is **ONNX Runtime + CoreML EP**, models resolved inside `fishsense-core`
  (nothing bundled in the mobile repo). Capture is **iOS-only today** (Android is a stub).
- **Hard dependency — model size.** Current models are **too large for on-device**; **offline
  mobile is blocked until they shrink** (quantization / distillation / smaller architectures).
  This is a `fishsense-core` workstream on the **critical path** for mobile, not a nice-to-have.
- **Shared-layer discipline extends to assets**: prepared models live in the shared layer
  (the registry / `fishsense-core`), not stranded in v1 worker code, so **v2 inherits them**.
  Today the laser detector is stranded (baked into the v1 data-worker image) and the SAM 3.1
  loader and predictor versions live in v1's `fishsense-shared`, not core.
- Models may be **per-`DeviceKind`** (Lite segmentation vs Mono monocular-depth). GPU on
  NRP now; Edge TPU on phones later. *(Open: mobile runtime/format, same-vs-variant,
  on-device size budget — §9.2.)*

### 4.8 Deployment / infrastructure
- **Control plane + state** (API, orchestrator, Postgres) → **the existing fishsense krg Incus
  slot, Docker-Compose**, NixOS-converged, like v1's `deploy/incus/compose.yml`. **No kube
  here.** v2 **replaces v1 on that slot at cutover** (§6.6), not before. Until then the slot
  builds only from `fishsense-lite`, and nothing in this repo converges it. That slot is tight
  (6 vCPU, 12 GiB, a 20 GB root disk), which v2's compose has to respect.
- **Processor** → **Kubernetes**, kustomize: **NRP `amd64`** today (v1 has four Deployments —
  cpu / light / gpu / gpu-cpu-fallback — scaled 0↔N by the orchestrator), →
  junkyard/Pixel-Fold **ARM64** later. **Multi-arch images**; ARM64/Knative
  are **processor-only, future** — not near-term control-plane concerns.
- **Garage** (external, `s3.e4e.ucsd.edu`) and **Temporal** (shared krg-prod cluster,
  **mTLS**, **single `fishsense` namespace** — tenant scoping is in-workflow, not per-namespace;
  §9.4).
- **Reuse krg patterns**: OpenBao-rendered secrets, mTLS Temporal client certs
  (CN `fishsense-worker`), app-password service accounts, and the release-please →
  promote → `nixos-rebuild` / `kubectl apply -k` CI pipeline.

## 5. Phase 0 — prerequisites in `fishsense-core`
- **Port the image-preprocessing pipeline to Rust** (raw decode, auto-gamma, contrast
  stretch, CLAHE, undistort, JPEG — still Python rawpy/skimage/cv2 in v4.0.0, and v4.0.0
  added an underwater-enhancement vocabulary, off by default, that the port must also cover),
  **gated on measurement parity** against the current path (gamma/CLAHE feed segmentation +
  length; the parity validation, not the coding, is the cost).
- **Finish untangling `core → fishsense_api_sdk`** — v4.0.0 moved it behind an optional
  `rectified` extra, but `RectifiedImage` and `_laser_detector.py` still import
  `CameraIntrinsics` from the API SDK, and v1's data worker relies on it. Core should take a
  plain intrinsics type of its own.
- **Move model loading into core** — the laser detector checkpoint and the SAM 3.1 loader
  and version constants live in v1 today (§4.7).
- **(When the research settles)** move the research geometry into core: flat/dome-port
  refraction + water index, the dots-plus-apparent-size laser closure (wuwnet), and one
  canonical depth triangulation (imwut and wuwnet each re-implement it in numpy). Until then,
  v2 records method versions but does not run the method (§9.21).
- **(Cheap, deferred)** add Python bindings to `pixel-finch` so a Python worker can drive
  the Edge TPU on phones without a native-Rust rewrite.

## 6. Migration strategy — big-bang cutover on the existing slot *(decided 2026-09-23)*

*Supersedes the original strangler / parallel-build plan. v2 is **not** run beside v1: no
second Incus slot, no dual-write period, no per-dive ownership handoff. v2 is built and
rehearsed here, then replaces v1 on the fishsense Incus slot in one planned window.*

### 6.1 Decisions
| Area | Decision |
|---|---|
| **Cutover style** | **Big bang**, on the **existing fishsense Incus slot** (same hostnames). |
| **Code home** | **This repo becomes the monorepo.** v1's pipeline code is **ported in**, not rewritten (all Python, §3.1). At cutover an admin repoints the slot's `fishsense-selfupdate` flake and runner scope from `fishsense-lite` to `fishsense-services` (§9.9). |
| **Web portal** | **Ported to the v2 API** before cutover (generated `openapi-typescript` client, tenant-scoped paths). No v1-compatible endpoints. |
| **v1 freeze** | **~2 weeks** of v1 feature freeze before cutover. Until then v1 keeps changing, and each change is ported as it lands. |
| **Downtime** | **A weekend** for the cutover window. |
| **Data** | A **one-shot** migration, v1 `fishsense` DB → a new v2 database **in the same Postgres instance**. v1's database is never modified, which is what makes rollback possible. |

### 6.2 What "parity" means — the gate for scheduling cutover
v2 must do everything v1 does in production (§2) before a date is set:
- **Ingest:** operator-run NAS ingest, with checksums and cross-dive duplicate detection (the
  v1 `IngestDiveWorkflow` shape). User upload (§4.4) can follow after cutover.
- **Pipeline:** the full stage set (DIAGRAMS §8), the schedules with `overlap=SKIP`, the
  laser detector and the auto-accept gate, SAM 3.1 head/tail, clustering, slate and
  checkerboard calibration with all four gates and refusals, laser depth, measure — on v2's
  schema, tenant-scoped.
- **Label Studio:** create/populate/sync for all four label kinds; existing projects and sync
  cursors carried over, not recreated.
- **Workers:** api-worker duties (selectors, NRP scaling, raw staging) and the NRP processor
  on cpu/light/gpu queues.
- **Web portal:** triage, calibration-source editing, and the project hub — on the v2 API.
- **Ops:** nightly backups (§9.5), the image prune, Temporal cert rotation (`reload`), and
  NRP cert sync.
- **Consumers:** Superset dashboards and the research repos' queries run against v2
  (§9.20) — v1-shaped research views, with v1 ids kept, so imwut/cscw/wuwnet don't break.
- **Numbers:** per-dive measurement parity with v1 on a restored dump (§6.4), within an
  agreed tolerance.

### 6.3 Porting v1 while it keeps changing
- Port **module by module** into the monorepo, adapting data access to v2's schema and
  tenant-scoped transactions. Keep the ported code recognisable, so a later v1 change can be
  re-applied by diff.
- Track the v1 commit each ported module was taken from. Until the freeze, every v1
  commit touching ported code is re-ported, so parity keeps up with v1.
- **Freeze** (~2 weeks before cutover): v1 takes only fixes. That leaves a stable target for
  the final port, the parity checks, and at least two full rehearsals.
- `fishsense-core` stays the shared research layer. v2 pins the same wheel v1 runs at freeze.
  **Phase 0's SDK untangling (§5) becomes a hard prerequisite**, because v2 retires the v1 SDK
  that `RectifiedImage` still imports.

### 6.4 Data migration
- **One-shot and rehearsed.** A migration job reads v1's `fishsense` DB and writes the v2
  database as the **lab tenant**. It is run against restored nightly dumps until it is
  idempotent and its validation report is clean. On the day, it runs **locally on the slot**
  (same Postgres instance, so no dump and no network hop).
- **Mapping** (v1 → v2): `camera` → Device (`lite`); `dive` → Dive (priority, notes, refusal,
  borrowed calibration); `image` → Capture (path + checksum + `is_canonical`); label tables →
  Label (with source; auto-accept inferred from `laserprediction`); `laserextrinsics` → laser
  calibration (the current row; producer inferred from the dive's calibration target);
  predictions → Prediction; `measurement` → Measurement tagged `source = v1-migration`, with
  unknown core/model versions recorded as **unknown**; reference data from **live** rows, with
  the seed→live difference as version history.
- **Keep v1 ids** as stable columns (the research SQL, tests, figures and cscw hard-code them).
- **Objects stay where they are.** Captures point at the existing Garage keys and NAS paths.
  Nothing is moved at cutover; re-homing under the tenant layout happens later, if ever.
- **Lost by construction:** v1 overwrote calibrations and measurements in place, so v2's
  history starts at migration. imwut's frozen CSVs are the only earlier record; importing
  them as historical measurement versions is optional (§9.20).
- **Validation report (go/no-go):** row counts per table, every image checksum carried over,
  every Label Studio project mapped, and per-dive measurement parity after v2 re-measures
  the migrated dives.
- ***Built and rehearsed (2026-09-25):*** `fishsense-services-api migrate` then
  `fishsense-services-api migrate-v1` (source `FISHSENSE_V1_DATABASE_URL`). One transaction,
  idempotent by `v1_id`, never inventing values v1 didn't record. On the 2026-09-25
  production dump: every row of all 24 v1 tables accounted, **~43 s**, tenancy audit
  passes, and **measurement parity 2,968 = 2,968** (v2's `current_measurements` vs v1's own
  freshness rule). It exits non-zero (NO-GO) on any unaccounted row, audit violation or
  parity gap, and refuses to start on a schema not at head.
- **The migrating role must bypass RLS** (superuser or `BYPASSRLS`): `FORCE ROW LEVEL
  SECURITY` binds the table owner too, so a plain owner would be blocked by the very
  policies it writes under. `migrate-v1` checks this before touching data.
- **v1 data issue found by the rehearsal:** dive 509 ("2023-08-18 Nathans Pool 04") borrows
  calibration from dive 508, which has no extrinsics -- its 162 measurements are stale in v1
  and v2 alike. Fix in v1 (or accept) before cutover.
- **Rehearsal hygiene:** production dumps are restored only into throwaway local
  containers; the committed test fixture is v1's schema only (`pg_dump --schema-only`).

### 6.5 Shared services during rehearsals and cutover
Before cutover, v2 never runs against production shared services with production names:
- **Temporal** (shared `fishsense` namespace): rehearsal workers use **distinct task-queue,
  workflow-id and schedule-id prefixes**, so they can never take v1's tasks. At cutover, v1's
  schedules are **deleted** (`ensure_schedule` never updates in place) and v2's created.
- **Garage:** rehearsals read v1's buckets with **read-only** keys and write to scratch
  buckets only.
- **Label Studio:** rehearsals never write to the production workspace.
- **NRP:** rehearsal processor Deployments use distinct names; at cutover the v2 image
  replaces v1's Deployments, and its API URL changes.

### 6.6 Cutover runbook (the weekend)
1. **Before:** parity reached (§6.2); v1 frozen (§6.3); ≥2 clean rehearsals (§6.4); the admin
   change to repoint selfupdate + the runner scope is prepared; the window is announced.
2. **Stop v1:** pause all v1 schedules; drain in-flight workflows; scale the NRP data worker
   to 0; put the portal into maintenance; turn off the nightly `autoUpgrade` from
   `fishsense-lite`.
3. **Back up:** a final `pg_dump` of `fishsense` and `superset`, copied **off the slot**.
4. **Migrate:** `fishsense-services-api migrate` (schema), then the data migration job, then
   the validation report → **go / no-go**.
5. **Switch:** the admin repoints the slot to `fishsense-services#fishsense`; converge; delete
   v1's schedules and create v2's; roll the v2 processor out to NRP; point the data-worker
   config at the v2 API.
6. **Verify:** a smoke-test script (health, a login, a known dive's measurements, one
   end-to-end pipeline firing, a Label Studio sync, a portal triage action, one research
   query).
7. **Reopen**, and watch the first full schedule cycle.
8. **Rollback window — 48 h.** Rollback = repoint the slot back to `fishsense-lite`, recreate
   v1's schedules, restore the NRP Deployments. v1's database was never modified, but
   **anything written in v2 after reopening is lost** on rollback. After 48 h: fix forward
   only, and v1's database becomes a read-only archive.
9. **After:** retire the v1 SDK; archive `fishsense-lite` (§9.9); NAS retirement follows its
   own ordering (§9.15).

## 7. What to do now

**In `fishsense-lite`, until the freeze** (useful to the PhD, and it makes the migration
richer):
- **Extend v1's input-naming into full provenance** (partly done: `measurement` already
  records `laser_extrinsics_id`): `core_version` + model/predictor versions on
  `measurement`; the **producer** (slate / checkerboard) and residual on `laserextrinsics`;
  gate auto-accepts marked on `laserlabel`. Every field v1 records now is one the migration
  doesn't have to mark "unknown".
- **Put all weights under one versioned scheme** (§9.12).
- ~~**Add GPU requests** to `deploy/k8s/data-worker`~~ — **done**.
- **Phase 0** (§5) in `fishsense-core`, starting with the SDK untangling: it is now on the
  cutover's critical path.

**In this repo, toward parity:**
- The tenancy foundation — **done** (roles, RLS, in-app OIDC, memberships, first route,
  packaging).
- The v2 domain schema (§4.3), then the port of v1's pipeline, module by module (§6.3).
- The processing contract package (§9.1), which the ported workers speak — **built**; stage 1
  (clustering) runs end to end on it.
- The data migration job and its validation report (§6.4), rehearsed early and often.
- The web portal port (§6.1).
- The production deploy: a `flake.nix` with the fishsense `mkTenant`, a production compose
  (inner Traefik, vault-agent secrets), and the promote → converge workflow.

## 8. Extension seams for future devices
- New device = new `DeviceKind` + a `CaptureExtension` table + processor algorithm(s)/
  model(s) in `fishsense-core` / the model registry. **No changes** to tenancy, auth, core `Capture`,
  ingestion, or the measurement/versioning model.
- **The first real test of this claim is `lite_flatport` (wuwnet)**, and it only holds if
  the **camera model is a seam too**. A flat-port camera is axial: rays don't pass through one
  centre, so pinhole back-projection (`WorldPointHandler`'s K⁻¹) is wrong for it, and depth
  and length need non-central rays. So the camera model must be **named on the camera
  calibration and dispatched in `fishsense-core`** — v2 stores which model and version was
  used, never assumes pinhole. Its calibration is also a different *workflow* (dry session +
  per-dive water conditions + slate-free laser closure), which the Calibration entities
  (§4.3) must accept without special cases.
- Multilens & Mono = new extensions + models; Scout reuses Mono with an ROV/camera-trap
  capture source.

## 9. Open questions — worklist

Grouped by when they need answering. Each has: **the decision**, *what it blocks*,
*options / lean*, and *to close*.

### A. Decide soon — unblocks v1-now churn (§7) and v2 foundations

**9.1 — Stable processing data-contract** *(what the ported workers speak)* — *built
2026-09-25; role revised 2026-09-23 for the big-bang cutover*
- *Decision:* a **v2-owned, versioned contract package that lives here.** Schema-first
  (Pydantic / JSON-Schema, language-neutral), *informed by* v1's `fishsense-api-sdk` +
  `libs/fishsense-shared/preprocess_contracts.py` but **not** a rename of them.
- *Role:* the contract between v2's orchestrator and processor (and the processor's writes
  back to the API). v1's worker interfaces are **ported onto it** during the port (§6.3).
  v1 itself never adopts it: v1 is retired at cutover, so there is no convergence period
  to protect.
- *Built 2026-09-25* (`services/fishsense-services-contracts`):
  - `CONTRACT_VERSION`, with each version's JSON Schema published under `schemas/`. CI fails
    when a model changes without a new version, and every published version is kept.
  - The processor's queues, named so they never collide with v1's.
  - The shared Temporal connection: the namespace is required.
  - Stage 1's input, the first DTO carried over from v1.
  - Later stages add their DTOs as they port.

**9.2 — Model packaging** *(§4.7)* — *resolved (registry choice reopened as §9.12)*
- MLflow-on-Garage = **canonical versioned registry**, consumed at **build/deploy time**
  (build-time input for mobile, **never a runtime dependency** — mobile is offline). Each
  version = **one set of weights, two builds** (server full ONNX + mobile quantized) under one
  `model_version`; **consumers pull selectively** (mobile bundles only the models it runs);
  mobile model update = **app release**.
- **`fishsense-core` is the shared inference lib for server + mobile and owns the on-device
  runtime/format** (mobile format is its internal concern; the load-model seam lives there).
- **Dependency, tracked:** current models are **too large for on-device** — shrinking them is
  a `fishsense-core` workstream on the **critical path for offline mobile** (§4.7).

**9.3 — Authentik invite flow** *(§4.2)* — *built (krg-infra PR #504, merged)*
- The enrollment/invitation/local-`user_write` flow now exists; tenant/org is carried as a
  user attribute and emitted as an **`org` OIDC claim**, with per-org isolation left
  **app-side** — matching our `sub`-keyed tenancy model.
- *Remaining follow-ups:*
  - **Apply to live Authentik** (PR is `tofu validate`/`tflint` clean but **not yet applied**).
  - **Register v2's OIDC clients requesting the `org` scope**, and add v2's apps to
    `local.fishsense_collab_targets`.
  - **Decide whether external partners may reach the v2 API** (v1 binds all three apps incl.
    the orchestrator API — one-line change to drop it).
  - **Self-service invites?** Today invites are minted **out-of-band in the Authentik UI**
    (no provider resource). If tenant admins should invite colleagues from our web app, that
    needs Authentik API integration — a new v2 UX workstream.

**9.4 — Temporal multi-tenancy isolation** — *resolved: in-workflow scoping*
- **Single `fishsense` namespace on the shared krg-prod cluster; tenant scoping lives in the
  workflow/activity logic, not in namespaces.** Rationale: namespace-per-tenant would sprawl
  namespaces across a **shared** cluster (not ours to explode), and OSS Temporal doesn't
  enforce namespace isolation anyway.
- *Mechanism:* `tenant_id` is a **required field in every workflow/activity payload** (part
  of the §9.1 contract); **workflow IDs are tenant-scoped** (e.g. `measure-fish-{tenant}-{cap}`);
  activities read/write only through the tenant-scoped contract / RLS-backed API.
- *Accepted tradeoff:* no Temporal-level *resource* isolation between tenants — mitigate a
  runaway tenant with **fair-share `select-next` selectors + per-tenant concurrency limits**
  (§4.5), not namespaces.

**9.10 — Tenant resolution & RLS mechanics** *(blocks the schema)* — *partly decided
2026-09-23*
- ***Decided:*** the active tenant is named in the **URL path** (`/tenants/{slug}/…`), and the
  API checks membership on every request. **Sharing is deferred:** every row has exactly one
  owning `tenant_id`, RLS is a plain equality, and a share table arrives later as an additive
  policy. The first vertical slice is this **tenancy foundation** (roles, RLS, JWT validation,
  memberships), built TDD against real Postgres.
- *Active tenant:* users ↔ tenants is many-to-many, so a `sub` → membership lookup can
  return several tenants. The request must name its **active tenant** (a header or URL path),
  and the API must check it against the user's memberships.
- *Shared captures vs single-column RLS:* §3 allows a capture to belong to several tenants,
  but a `tenant_id = current_setting(...)` policy hides a shared row from every tenant but its
  owner. Either the policy consults the sharing table, or sharing is dropped for now. Also
  decide whose `tenant_id` the derived rows (measurements, labels) of a shared capture carry.
- *Mechanics (non-negotiable once chosen):*
  - set the tenant with `SET LOCAL` / `set_config(..., true)` inside the transaction — a plain
    `SET` leaks across pooled connections;
  - the app role neither owns the tables nor has `BYPASSRLS`, and tables use
    `FORCE ROW LEVEL SECURITY`;
  - migrations run as a separate role.
- *Provisioning:* ***decided 2026-09-23*** — a `users` row is created **just-in-time on the
  first valid token** (keyed on `sub`; the app role may insert only the caller's own row).
  v2 keeps **no local credentials**: Authentik stays the only IdP, and the API validates
  bearer tokens itself rather than trusting forward-auth headers (mobile sends bearer tokens;
  tenancy needs per-request membership; nothing that bypasses Traefik can claim an identity).
  **Memberships are granted administratively.** Still open: turning a partner invite's `org`
  claim into a membership automatically — `org` is single-valued, while memberships are
  many-to-many.
- *Token:* validate the client's own bearer token (issuer, audience = the web and mobile
  client ids, expiry, JWKS signature). Trust the proxy's `X-authentik-jwt` only if nothing
  can reach the API without going through Traefik.

**9.11 — Cross-tenant principals: processor & object store** — *open*
- The processor holds one service identity, handles every tenant's payloads, and runs on
  infrastructure we don't own (NRP, later phones). What stops it writing tenant B's
  measurements from a tenant-A payload? *Lean:* the API accepts a processor write only when
  it matches a run the orchestrator issued for that tenant.
- Garage grants keys per bucket, so a tenant prefix isolates nothing. *Options:* accept
  shared-bucket + app-enforced isolation (and say so), or **bucket-per-tenant** (v1 already
  runs three buckets with per-worker grants — §2.6). Decide before the first partner upload.

**9.12 — Model registry: MLflow vs Garage `model-weights`** — *reopened (was part of 9.2)*
- §3 and §4.7 chose MLflow, but v1 has shipped without it: SAM 3.1 is versioned at
  `model-weights/{name}/{version}/{filename}`, and the laser detector is baked in from
  Hugging Face.
- *Options:*
  - (a) adopt the Garage bucket as the registry and add a small metadata table (version →
    server/mobile artifact, training run);
  - (b) migrate to MLflow for lineage and experiment tracking.
- *Either way:* one scheme for every model, including the laser detector, loaded through
  `fishsense-core`.

**9.13 — What "current" means for a measurement** — *decided 2026-09-24 (proposal adopted,
overridable); built in migration 0013*
- *Adopted for calibrations:* append-only rows; **current = the latest row per dive**; a
  refusal is itself a row (outcome `refused`), replacing v1's refusal columns on `dive`.
- *Adopted for measurements (`current_measurements`):* append-only; current = the
  latest row per `(capture, fish, source)` whose inputs (laser calibration, labels) still
  match the current ones -- v1's mismatch model, plus history.
- Candidates: latest per `(capture, fish, source)`, explicit promotion of a run, or the
  latest whose inputs match the current calibration and labels (v1's mismatch model extended
  with history).
- Must hold when a server recompute and a device measurement coexist, and when an old
  algorithm is re-run for comparison.

**9.14 — Label Studio under multi-tenancy** *(the pipeline has a human in it)* — *open*
- v1 labeling is model-assisted but still requires humans (§2.4), on the **hosted SaaS**
  (`app.heartex.com`), in **one workspace shared with unrelated projects**, one project per
  dive per stage.
- *Decide:*
  - may partner data go to a third-party SaaS at all (data agreements)?
  - one workspace per tenant, or projects tagged per tenant?
  - who labels a partner's data — the partner or the lab?
  - how LS user ids map to v2 users (v1's `user` is an LS identity);
  - whether v2 keeps LS or grows its own review UI (v1's web triage is a start).

### B. Decide during the build

**9.5 — Postgres durability / HA** — *deferred until the phone-cluster move (moves with 9.7)*
- **No HA work until we move to the phone cluster.** HA only becomes necessary when the
  control plane lands on **volatile** phone-cluster nodes; on the current single Incus slot
  it isn't worth the complexity.
- *Interim posture:* single-instance Postgres + **scheduled logical backups** (reuse v1's
  backup-worker pattern — nightly `pg_dump` of the app DB **and Superset's metadata DB**,
  retention 14). Backups **are** the durability mechanism until HA arrives.
- *Consequence to handle now:* v1 backs up `pg_dump` → **NAS**, which is being **retired**
  (§4.6) → **the backup target must move** — but *not* onto the same single-node Garage that
  holds the data it backs up (§9.15).
- *Revisit at the phone-cluster move:* HA topology + target RPO/RTO. *(e4e Garage durability
  is separate and owner-tracked — §4.6.)*

**9.6 — Mobile sync protocol** — *shape decided; one blocking decision open*
- **Direction: one-way per data class — no conflict resolution needed.**
  - *mobile → server*: captures + form responses = **append-only immutable facts**. A redone
    two-point selection is a **new append-only measurement version**, not a conflict.
  - *server → mobile*: **form templates** (tag ID etc.) = **server-authoritative, read-only
    on device**, versioned + cached offline; every response records `template_version`.
  - No record is ever written by both sides.
- **Transport:** presigned **S3 multipart** direct to Garage (resumable by construction, keeps
  the API out of the byte path, matches the Lite path). **Two-phase**: `reserve` → `upload` →
  `commit`; a capture is only visible once committed.
- **Idempotency:** client-generated UUID at capture time + content checksum; server upserts on
  `(tenant_id, client_capture_uuid)`.
- **Triggers:** manual "sync now" + automatic **on WiFi only** (a session is GBs), as a
  background transfer.
- **Local retention: keep on-device; offer a user-initiated "clear".** (A delete-all path
  already exists; per-row delete currently **orphans the JPEG** — fix that leak.) No tombstones
  needed while sync is upload-only — the server is the archive.
- **Constraints:** capture must work with **expired/absent tokens** (weeks offline) → generous
  `refresh_token_validity` on the mobile client, and captures bound to the capturing user so a
  different login can't sync them. Old app builds + **old cached templates** persist for months
  → the §9.1 contract must be **backward compatible**.

*Current mobile reality (repo read) — `photos` table, sqflite, RGB as a JPEG file + depth /
confidence / mask / intrinsics as SQLite BLOBs:*
- ✅ **Intrinsics retained** (9×f64 K, added in v7 for post-hoc recompute); **depth f32 ~256×192
  in meters**; **confidence map retained**.
- ⚠️ **Depth is a raw `memcpy` using `bytesPerRow`** — rows may be padded and **stride is not
  recorded**; ingest must derive `stride = len / height`, never assume `width*4`.
- ⚠️ **Mask ~2.7 MB dominates the payload and is derivable** from the image + model version →
  **do not sync it** (or compress heavily).
- ❌ **Sync is entirely greenfield** — no outbox, no sync-state columns, no API client.
- ❌ **No client UUID / device id, no auth or token storage, no app / model / `fishsense-core`
  version per row** → **breaks the `model_version` provenance committed to in §4.7**.
- ❌ Missing camera **pose/extrinsics**, ARKit timestamp, depth↔color alignment, distortion.
- **Android capture is a stub — iOS only today.**

***Blocking decision — lossless RGB:*** mobile persists **only JPEG q0.8**; the raw RGBA8 is
computed then **dropped**. So §4.6's "recompute and validate from raw" **cannot be fully honored
for mobile** — server-side segmentation on a q0.8 JPEG won't reproduce the on-device mask. Either
(a) change mobile to persist raw pixels (storage cost), or (b) consciously accept mobile recompute
as **approximate** and document it. **Needs a call.**

*Required mobile-side work (schema v8):* client UUID + stable device id; app / model /
`fishsense-core` version per row; sync-state columns + outbox; depth stride; OIDC auth + secure
token storage; fix the per-row file-delete leak.

**9.15 — NAS retirement sequencing** — *open; hard ordering constraint*
- The NAS is load-bearing three ways: the durable raw archive, **v1's ingest source**, and the
  **backup target** (§2.6). The e4e Garage is single-node with no backup (§4.6).
- Retire it in this order: (1) Garage redundancy or an off-site copy exists; (2) raw is
  backfilled and verified by checksum; (3) ingest has a non-NAS source (v2 upload, §4.4);
  (4) backups move to a target **other than** the Garage they protect. Retiring earlier leaves
  one disk holding raw, processed data and the DB backups.

**9.16 — Pipeline state model** — *open*
- DIAGRAMS §7 mixes upload state with processing state, and it lacks states that v1 has
  proved necessary:
  - dive priority LOW / HIGH / NONE + notes;
  - calibration refusals that expire when a newer label arrives;
  - `superseded` vs `needs_reprocess` vs sentinel labels;
  - "awaiting human labels";
  - "tried, made no progress" (v1's `computed: 0` wedge).
- *Lean:* upload status on `Capture`; processing tracked **per run** (the run a re-run
  appends), so a re-run never makes a measured capture look unmeasured; the server can't
  observe "uploading" (clients PUT straight to Garage), so it goes from reserved to committed.

**9.17 — Cross-tenant references** — *borrowing decided 2026-09-24 (proposal adopted,
overridable)*
- ***Decided:*** a dive may **borrow another dive's calibration within the same tenant only**,
  as an explicit fallback (v1 parity). The database enforces the same-tenant rule (composite
  FK) and forbids self-borrowing; the effective-calibration view follows the borrow.
  Cross-tenant borrowing is not allowed.
- Note the research premise that **laser extrinsics move every dive** (wuwnet; v1's own
  `dive_laser_line` docstring agrees), which argues against borrowing at all except as an
  explicit, flagged fallback.
- v1 lets a dive **borrow another dive's calibration** (`calibration_dive_id`), and the
  **same frame can sit under several dives** (`is_canonical`). Under tenancy, both can cross a
  tenant boundary. Allow within a tenant only? Or allow borrowing a calibration across tenants
  as read-only reference data (the rig is physical and shared)?
- `species`, `fishmodelreference`, `calibrationtarget` are global reference data (§4.3) —
  confirm that no tenant needs its own.

**9.18 — Analytics (Superset) under RLS** — *open*
- v1's Superset connects directly to Postgres as its own role, through views. Under RLS
  that connection sees nothing (no tenant variable set) or everything (`BYPASSRLS`).
- *Options:* keep Superset lab-internal on a BYPASSRLS read role (and say so), or give each
  tenant its own analytics through the API.
- Label the views: prod's views compute p90 **per fish**; the paper's p90 is **per (dive,
  model) cell after the cohort rule**. Dashboard numbers must not be mistaken for the paper's.

**9.19 — Upload integrity** — *open*
- The API never sees bytes, and a multipart ETag isn't a content hash, so "commit (checksums
  verified)" needs a verifier: S3 additional-checksum headers (check that our Garage version
  supports them) or processor-side verification. Keep v1's content-checksum contract (md5 of
  the whole ORF), or move to sha256 and backfill.

**9.20 — Research data access** *(the paper repos are production consumers)* — *open*
- Today: **superuser `psql` over SSH** into the prod container, or restored nightly dumps;
  hand-run CSV exports; hand-written prod fixes (§2.7). Under v2 RLS, superuser bypasses
  everything and the SSH path must go.
- *Lean:* a named **research role** with read access scoped to the lab tenant (not
  `BYPASSRLS`); a **versioned read-only view contract** replacing `sql/extract_*.sql`;
  **as-of** reads for frozen corpora; exports as a first-class, reproducible job (query +
  version + timestamp recorded) instead of psql footers stripped by hand; data fixes through
  the API, not SQL.
- Keep v1 ids addressable through the migration (§6.4).

**9.21 — Calibration and camera model for the research methods (`lite_flatport`)** — *open*
- wuwnet is a **new camera kind** (§1): a TG-6 with the air lens removed is an axial,
  refractive camera. *Decide:* a separate `DeviceKind` (lean, because processing dispatches
  on it) or a configuration of `lite` distinguished only by its camera calibration's model
  type. Either way, v2 must not hard-code pinhole anywhere (§8).
- wuwnet's in-air method needs: camera calibration separate from laser calibration; a
  **port/housing** device component; per-dive **water conditions**; new laser-calibration
  producers (`dots+apparent_size` etc.); versioned target geometry (§4.3).
- *Decide:* which of these v2 models **now** (cheap to add as nullable, painful to backfill
  later) vs when the method is ported to `fishsense-core`. Refraction and port models belong
  in core, not v2 — v2 only records which version was used. *Lean:* model the entities and
  producers now; leave the math in research until core has it.
- Also: the target anisotropy finding implies every v1 `cameraintrinsics` row may carry a
  ~0.7 % fx/fy bias — a v2 camera calibration must be re-derivable, not copied as truth.

**9.22 — Per-frame and per-animal analysis data** *(P2 needs)* — *open*
- Segmentation **masks** (storage, format, model version), **pose** per frame (from mask
  aspect ratio, independent of the error), **frames-per-animal**, **diver/volunteer
  identity** (a privacy question for partner tenants). None exist in v1. Mobile already
  produces masks (§9.6 says don't sync them — revisit if P2's pose analysis needs them).

### C. Decide late — post-parity

**9.7 — Phone-cluster data-migration trigger** — when/why to move data from the e4e Garage
to the phone-cluster Garage (cross-cluster egress vs. locality — §4.6).

**9.8 — Phone-cluster platform boundary** — how v2 (owning tenancy now) coexists with /
hands off to the phone-cluster platform's own tenancy when it arrives.

**9.9 — Monorepo consolidation** — *decided 2026-09-23:* **this repo is the monorepo.** v1's
pipeline is ported in before cutover (§6.3), and the web portal is ported to the v2 API. At
cutover a krg-infra admin repoints the slot's `fishsense-selfupdate` flake and the runner
scope (`mkTenant.repo`) from `fishsense-lite` to `fishsense-services`. After the rollback
window, `fishsense-lite` is archived. Still open: whether the web app moves into this repo
or stays a separate one (and so whether the runner scope covers one repo or two), and
whether to rename this repo.

### Resolved
- **API↔frontend typing** → `openapi-typescript` + `openapi-fetch` + zod, cleaned `operation_id`s (§3).
- **Garage topology** → three independent stores; start on e4e, may migrate later (§4.6).
- **Language / stack, deployment split, Garage-as-durable** → §3.
- **Delivery: big-bang cutover on the existing slot; this repo is the monorepo; web portal ported; ~2-week freeze; weekend window** → §3, §6.
- **Python vs TypeScript for the app tier; SQLAlchemy 2.0 over SQLModel; always TDD** → §3, §3.1.

## 10. Reference links
- Current service (Lite): https://github.com/UCSD-E4E/fishsense-lite/
- Mobile app: https://github.com/UCSD-E4E/fishsense-mobile/
- Measurement algorithms (Rust + PyO3): https://github.com/UCSD-E4E/fishsense-core
- Research repos (local siblings): `../imwut_2026_fishsense_lite` (P1 accuracy),
  `../wuwnet-fishsense2026` (in-air calibration), `../cscw-fishsense2027` (P2 deployability)
- Pixel Edge-TPU driver (Rust): https://github.com/junkyard-computing/pixel-finch
- Authentik / infra (Incus/Compose/NixOS, no kube): https://github.com/KastnerRG/krg-infra
- Data-processing cluster: https://nrp.ai/
- Temporal Rust SDK: https://docs.temporal.io/develop/rust
- OCEANS 2025 (Mobile): http://kastner.ucsd.edu/wp-content/uploads/2025/08/admin/oceans2025-fishsenseMobile.pdf
- https://e4e.ucsd.edu/fishsense/ · /fishsense-lite · /fishsense-mobile · /fishsense-scout

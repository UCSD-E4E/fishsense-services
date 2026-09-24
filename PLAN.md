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
v1 is **not frozen** (§6) and moves fast (~500 commits between the first draft of this plan
and this snapshot) — re-verify a detail before building on it. v1's own `CLAUDE.md` is its
most current doc; `docs/measurement_pipeline.md` is stale. Research repos (§2.7):
`wuwnet-fishsense2026` @ `440c6a4`, `imwut_2026_fishsense_lite` @ `64c08bf`. Mobile and
krg-infra were **not** re-read for this snapshot (still as of 2026-07-22).*

The current stack is single-tenant and **production-only (no staging)**. It is the
strangler base we build beside (§6), not a clean slate.

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
| **Role of this repo** | New **v2 backend** superseding `fishsense-api`; end state is a **monorepo** (this repo grows into it; consolidation deferred). |
| **Delivery strategy** | **Strangler / parallel build — v1 is NOT frozen.** `fishsense-lite` keeps churning for the owner's PhD (freezing blocks publications). v2 is built beside it; both share `fishsense-core`. See §6–7. |
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
| **Deployment** | **Control plane + state** (API, orchestrator, Postgres) → **krg Incus slot, Docker-Compose** (like v1; *no kube*). **Processor** → **Kubernetes**: NRP `amd64` now → junkyard/Pixel-Fold **ARM64** later. **Garage** and **Temporal** are external/shared. **ARM64/Knative are processor-only, future concerns.** |

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
  stays useful for backfill and for lab data that never leaves the NAS until §9.15 is done.
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
  retired — backfill durable raw `.ORF` NAS → Garage (repeatable, since v1 keeps ingesting;
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
- **Control plane + state** (API, orchestrator, Postgres) → **krg Incus slot, Docker-
  Compose**, NixOS-converged, like v1's `deploy/incus/compose.yml`. **No kube here.**
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

## 6. Migration strategy — strangler / parallel build (v1 is NOT frozen)
- **Hard constraint:** `fishsense-lite` keeps iterating throughout (PhD data runs +
  publications). Big-bang "freeze + cut over" and a live RLS retrofit are both off.
- **Orthogonal axes:** the PhD iterates on *measurement/processing* (algorithms, models,
  scheduled runs); v2 restructures *tenancy/control-plane*. They run **in parallel on
  different layers**.
- **`fishsense-core` is the shared, continuously-iterated research layer** (versioned
  wheel; both v1 and v2 depend on it) — v2 **inherits** PhD progress, never chases it.
- **Build v2's control plane fresh and additively** — designed-in tenancy/RLS, never
  retrofitted onto live data.
- **Lab-as-tenant at parity:** backfill existing data into a **primary lab tenant**; when
  v2 reaches parity for the research loop, the PhD workflow continues *inside* v2 as that
  tenant — no blocking cutover.
  - **Backfill is repeatable and idempotent, not one-shot** — v1 keeps ingesting and
    relabeling until parity.
  - **Reference existing Garage objects in place; don't move them.** v1 reads the current keys,
    and `Capture.raw_object_key` can point at them. Only new objects go under the tenant layout.
  - v1's **label state** (`superseded`, `needs_reprocess`, sentinel) and **dive state**
    (priority, refusals, borrowed calibration) must survive the backfill.
  - **Keep v1 ids addressable** (v1 dive/image ids as stable columns, even if v2 uses UUIDs) —
    they are hard-coded in the research SQL, tests, figures and the cscw repo (§2.7).
  - Backfill reference data from **live** rows, not seeds, and record the seed→live
    difference as version history.
- **Discipline:** algorithm/model changes → shared `fishsense-core` / model registry;
  control-plane / schema / storage-layout changes → **v2, not v1**. Exception: provenance
  additions to v1's own tables (§7) are allowed — they serve the PhD's publications, and v1
  is already heading that way.

## 7. What to do in v1 *now* (churn) vs. v2

**Now, in `fishsense-lite` (helps the PhD + de-risks v2 — all on the shared/processing axis):**
- **Extend v1's input-naming into full provenance** (partly done — `measurement` already
  records `laser_extrinsics_id`): add `core_version` + model/predictor versions to
  `measurement`; record the **producer** (slate / checkerboard) and residual on
  `laserextrinsics`; mark gate auto-accepts on `laserlabel`. Append-only history would
  replace the destructive upsert and the in-place extrinsics refit — reproducible,
  publication-traceable lengths.
- **Put all weights under one versioned scheme** (§9.12). SAM 3.1 already lives in Garage
  `model-weights`; the laser detector is still baked in from Hugging Face.
- ~~**Add GPU requests** to `deploy/k8s/data-worker`~~ — **done** (gpu Deployment + CPU
  fallback).
- Begin **Phase 0** (§5) in `fishsense-core`.
- *(Optional)* opt-in-align v1 toward the v2 contract package (§9.1) where cheap — never
  required; v1's existing `fishsense-api-sdk` / `preprocess_contracts.py` inform it.

**v2 (this repo, built beside v1):**
- **Author the v2-owned processing data-contract package** (§9.1) — versioned,
  contract-tested here; v1 opt-in only.
- Tenancy + RLS + in-app OIDC validation + RBAC; device abstraction; presigned-upload
  ingestion; tenant-aware schedule selectors; test/staging tenant; the Authentik
  invite-flow prerequisite.

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

**9.1 — Stable processing data-contract** *(the strangler linchpin)* — *approach decided*
- *Decision:* a **v2-owned, versioned contract package that lives here.** Schema-first
  (Pydantic / JSON-Schema, language-neutral), *informed by* v1's `fishsense-api-sdk` +
  `libs/fishsense-shared/preprocess_contracts.py` but **not** a rename of them.
- **v1 may be made aware of it (opt-in — vendor / `pip install` / validate against it),
  never a hard dependency.** A v2→v1 hard coupling is exactly the backwards dependency to
  avoid; v1 must stay free to churn.
- *Role:* it is the **convergence target**, not a lock on v1. It does **not** freeze v1's
  task interfaces — those stay free to churn and get ported/adapted to the contract **at
  parity** (lab-as-tenant migration); v1 can opt-in-align earlier where cheap. (Trades
  "early protection of v1 tasks" for "v1 freedom" — the correct priority.)
- *To close:* author the package here, version it, contract-test it in **v2's** CI,
  optionally publish it as a wheel v1 can consume.

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

**9.10 — Tenant resolution & RLS mechanics** *(blocks the schema)* — *open*
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
- *Provisioning:* the invite puts `org` into the token, but nothing yet turns that into a v2
  `Membership` row (just-in-time on first login? admin-created?). `org` is also single-valued,
  while memberships are many-to-many.
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

**9.13 — What "current" means for a measurement** — *open*
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

**9.17 — Cross-tenant references** — *open*
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
- Keep v1 ids addressable through the backfill (§6).

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

**9.9 — Monorepo consolidation** — when/how v1's pieces fold in; whether to rename off
`fishsense-lite`.

### Resolved
- **API↔frontend typing** → `openapi-typescript` + `openapi-fetch` + zod, cleaned `operation_id`s (§3).
- **Garage topology** → three independent stores; start on e4e, may migrate later (§4.6).
- **Language / stack, deployment split, Garage-as-durable, strangler delivery** → §3.
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

# FishSense Services — v2 Plan

## 1. Purpose

FishSense is scaling along two axes at once:

1. **Multiple tenants** — no longer one lab. We serve our own team, a test/staging
   environment, and external **partners and customers**. Data and access must be
   isolated per tenant. FishSense itself becomes a **"customer" of the phone-cluster
   platform by end of year**.
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
| FishSense Mobile (Multilens) | Future | Multiple lenses, length without LiDAR, above water. |
| FishSense Mono | Future | Built on Lite; **ML monocular** depth. |
| FishSense Scout | Future | ROV / camera-trap on FishSense Mono. |

## 2. Current system (prior art, from repo reads)

The current stack is single-tenant and **production-only (no staging)**. It is the
strangler base we build beside (§6), not a clean slate.

- **`fishsense-api`** (`services/fishsense-api`) — FastAPI + SQLModel + async
  SQLAlchemy, Postgres, Alembic. Tables: `user, camera, cameraintrinsics, dive,
  diveframecluster(+mapping), diveslate, fish, species, measurement, image,
  {laser,headtail,diveslate,species}label, laserextrinsics, labelstudiosynccursor`.
  **Findings that drive v2:**
  - **Zero auth in the API** — only `Depends(get_async_session)`; trusts a forward-auth
    proxy, returns all rows unfiltered.
  - **No `tenant_id` on any table.** No device abstraction (`camera` = `serial_number`
    + `name`, no kind/type).
  - **`measurement` is a destructive upsert** on natural key `(image_id, fish_id)`; no
    version/run/algorithm/model provenance. **`laserextrinsics` is the one append-then-
    latest-by-`created_at` entity** — the template to generalize.
  - **The API never handles image bytes** — it stores `path`/`checksum` strings only;
    all object I/O is worker-side. Config: Dynaconf, `E4EFS_` prefix.
- **Workers (Temporal, mTLS to the shared krg-prod cluster, namespace `fishsense`):**
  - `fishsense-api-workflow-worker` (queue `fishsense_api_queue`) — owns **13 hourly
    Temporal Schedules** (staggered offsets, `overlap=SKIP`, `select-next/*` selectors
    filtering `priority=HIGH`), Label-Studio sync, stages raw/slate into Garage, and
    **k8s-scales the data worker 0↔1** on NRP.
  - `fishsense-data-processing-workflow-worker` (queue `fishsense_data_processing_queue`,
    on **NRP/Nautilus**, `amd64`, **no GPU**, scaled 0↔1) — imports **`fishsense-core`
    (pinned GitHub release wheel v2.0.0, cp313/cp314)**; does preprocessing, laser
    calibration, world-point, measure. Owns **zero schedules** (scale-to-zero safe).
  - `fishsense-backup-worker` — daily 03:00 UTC `pg_dump` → NAS.
- **`fishsense-core`** (we own it; wheel v2.0.0) — Rust + PyO3. Laser calibration,
  world-point triangulation, **ONNX Mask R-CNN segmentation**, head/tail, RANSAC depth,
  length. **Preprocessing (raw decode → auto-gamma → CLAHE → undistort → JPEG) is still
  Python** (rawpy/skimage/cv2), and `RectifiedImage` **depends on `fishsense_api_sdk`**
  (a coupling to fix — §5).
- **`pixel-finch`** (we own it) — pure-**Rust** Pixel-Fold **Edge TPU** driver from
  Debian userspace (WIP). No Python bindings yet; cheap to add.
- **Storage** — **Garage** (S3, hosted at `s3.e4e.ucsd.edu`, single bucket `fishsense`,
  path-style, presigned **reads** for Label Studio) + **NAS** (Synology, durable raw
  `.ORF`). **Decision: NAS is out, Garage is the forward path** (§4.6).
- **Web** — `apps/fishsense-lite-web`: Next.js 15 + **next-auth v5** (Authentik OIDC,
  JWT session). Groups are plumbed onto the session but **authorization is not enforced**
  ("intentionally not yet"). Calls the API via interior basic-auth, read-only.
- **Mobile** — `fishsense-mobile`: Flutter; captures image + LiDAR + on-device
  measurement (Rust); local SQLite; "cloud sync" stubbed, **not wired to a backend**.
- **Identity/infra (`KastnerRG/krg-infra`)** — **Authentik** (Terraform IaC, AD-backed
  via LDAP). **No invitation/enrollment flow exists** (ADR 0013 — policy written, **NOT
  BUILT**). Also runs **MLflow, Temporal, OpenBao, Traefik**. Substrate is **x86
  Proxmox + Incus + Docker-Compose + NixOS — no Kubernetes, no ARM64.** The only k8s is
  the data-worker kustomize in `fishsense-lite`, targeting **NRP** (external).

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
| **Storage** | **The e4e Garage is the single durable, tenant-partitioned source of truth. NAS retired.** Three *independent* Garages exist (e4e / krg / phone-cluster); **e4e owns the data** → v2 starts on e4e, **may migrate to the phone-cluster Garage later** (an explicit data move, not sync). |
| **Reproducibility** | Raw capture = durable truth. **Measurements are append-only and versioned** by `algorithm + version + run_id + core_version + model_version` (generalizing `laserextrinsics`). Device-provided measurements stored alongside raw. |
| **Models** | Processor is becoming **model-heavy** (PhD churn). Models are **versioned source-of-truth inputs**, stored/served via **MLflow (already in krg) backed by Garage**; every measurement records the model version. §4.7. |
| **Language — app tier** | **Python** — API + orchestrator + data-worker (thin shell). Evolve FastAPI; first-class Temporal SDK. |
| **Language — libraries** | **Rust** (`fishsense-core`, `pixel-finch`) via **PyO3 bindings**. All real logic lives here. |
| **Language — web** | **TypeScript** (Next.js). |
| **API↔frontend typing** | **Generated from the FastAPI OpenAPI schema, done right**: `openapi-typescript` (types) + `openapi-fetch` (tiny typed client) + **zod** for runtime validation at the boundary. Requires cleaning up FastAPI `operation_id`s. (Not `openapi-generator` — the class-soup output that soured the v1 attempt.) tRPC-style *inferred* types are unavailable because the API is Python. |
| **Ruled out** | **Go** (nobody in-org) and **Rust for the app tier** (Rust talent is CV/systems). |
| **Processing** | Keep the **two-worker split** (Temporal **workflow** orchestrator + **activity** processor). **Processor floats** — NRP today, phones later — and is model/GPU/TPU-capable. |
| **Deployment** | **Control plane + state** (API, orchestrator, Postgres) → **krg Incus slot, Docker-Compose** (like v1; *no kube*). **Processor** → **Kubernetes**: NRP `amd64` now → junkyard/Pixel-Fold **ARM64** later. **Garage** and **Temporal** are external/shared. **ARM64/Knative are processor-only, future concerns.** |

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
   + MLflow model artifacts)              │ activity
                                          ▼
                    Processor (Python thin shell → Rust libs)  ── FLOATS
                    fishsense-core + pixel-finch (bindings) + MLflow models
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
  gap — same code path, isolated data. (There is no existing authz to preserve — the web
  app plumbs `groups` but enforces nothing.)

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
| **DeviceKind** | *(new enum)* | `lite \| mobile \| multilens \| mono \| scout`. |
| **Device** | `camera` | add `kind`; Lite = TG-6 metadata. |
| **Capture** (core) | `image` (+`dive`) | tenant, uploader, device, timestamps, geo, environment, **pointer to raw in Garage**. Reproducibility anchor. |
| **CaptureExtension** (per kind) | `cameraintrinsics`, `laserextrinsics` (Lite) | Mobile: LiDAR depth + ARKit meta; Mono: model inputs. |
| **Dive / Batch** | `dive` | Lite offload session. |
| **Measurement** | `measurement` | **append-only + versioned** (§4.6). |
| **Label / Annotation** | `*label`, `fish`, `species` | preserve LS-sync provenance. |

Everything except reference/lookup tables carries `tenant_id`.

### 4.4 Ingestion flows
- **Lite (TG-6)** — batch/offline: a lab member offloads the SD card, uploads a
  dive/batch attributed to **their tenant + user**; raw images (+ slate, calibration,
  camera metadata) go **direct-to-Garage via presigned URLs** (this is new — the API
  handles no bytes today; current presign is read-only). Completion enqueues a Temporal
  workflow → processor activities → append-only `Measurement`s.
- **Mobile** — authenticated app sync from local SQLite: image + LiDAR depth +
  on-device measurement. Raw + depth → Garage; `Capture` + mobile `CaptureExtension` +
  device measurement recorded. Raw retained so we **recompute/validate** the device's
  measurement.

### 4.5 Processing pipeline
- **Orchestrator** (Temporal **workflow** worker, Python) — durable coordination; lives
  with the API. Owns the schedule chain. Runs in the **single `fishsense` namespace** with
  **in-workflow tenant scoping** (§9.4): `tenant_id` in every payload, **tenant-scoped
  workflow IDs**. **Make `select-next` selectors tenant-aware / fair-share** (+ per-tenant
  concurrency limits) so one tenant's backlog can't starve others. Fix the current
  `ensure_schedule` "never updates in place, must delete+redeploy" footgun.
- **Processor** (Temporal **activity** worker) — the heavy CV/ML, calling `fishsense-core`
  (+ `pixel-finch` on phones) + MLflow models. **Python thin shell over the Rust libs**
  today; can flip to **native Rust** later as a phone-ops optimization — still a Temporal
  activity worker either way (Rust SDK is first-class now).
- **Scheduled / batch reprocessing is first-class** (Temporal Schedules): "run algorithm
  vX / model vY over all of tenant Z's captures." This is both the reproducibility
  mechanism and the PhD's active workflow — reads/writes the **stable data-contract**
  (§7) so scheduled tasks survive the restructure.
- **Processor floats & is GPU/TPU-capable**: NRP `amd64` (+ **GPU** once models land —
  add GPU requests to the kustomize) today, phones ARM64/Edge-TPU later.

### 4.6 Storage & reproducibility
- **The e4e Garage (S3) is the single durable, tenant-partitioned source of truth.** NAS
  retired (one-time backfill of durable raw `.ORF` NAS → Garage under the tenant layout;
  today's Garage `raw/` is scratch — v2 makes raw **durable + retained**). Presigned
  **uploads** (new) + reads; Garage **CORS** for browser flows.
- **Three independent Garages** (e4e / krg / phone-cluster); **e4e owns the data**. v2
  pins to e4e as canonical. A later move to the phone-cluster Garage is an **explicit data
  migration between independent stores**, not replication. The **floating processor** reads
  the canonical (e4e) endpoint wherever it runs — cross-cluster from the phones (works; keys
  auth from any IP) but with egress, which is part of what would justify migrating the data.
- **Known reality:** the e4e Garage is currently **single-node, no backup**. Durability of
  the source of truth is being followed up **outside this plan** (owner-tracked, recurring);
  v2 must not assume Garage redundancy exists yet.
- **Measurements are append-only and versioned.** Generalize the `laserextrinsics`
  "latest by `created_at`" pattern: each `Measurement` records `algorithm + version +
  run_id + core_version (wheel) + model_version`. "Current" = latest per natural key.
  Enables re-run history and ties every length to exactly what produced it.

### 4.7 Model / ML lifecycle *(new)*
- The processor is becoming **model-heavy** (PhD is preparing models now, in v1 churn).
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
  store, consumed at **build/deploy time**, not a mobile runtime dependency.
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
  *Confirmed by repo read:* mobile pins `fishsense-core v2.0.0` with `features = ["coreml"]` →
  on-device runtime is **ONNX Runtime + CoreML EP**, models resolved inside `fishsense-core`
  (nothing bundled in the mobile repo). Capture is **iOS-only today** (Android is a stub).
- **Hard dependency — model size.** Current models are **too large for on-device**; **offline
  mobile is blocked until they shrink** (quantization / distillation / smaller architectures).
  This is a `fishsense-core` workstream on the **critical path** for mobile, not a nice-to-have.
- **Shared-layer discipline extends to assets**: prepared models live in the shared layer
  (MLflow / `fishsense-core`), not stranded in v1 worker code, so **v2 inherits them**.
- Models may be **per-`DeviceKind`** (Lite segmentation vs Mono monocular-depth). GPU on
  NRP now; Edge TPU on phones later. *(Open: mobile runtime/format, same-vs-variant,
  on-device size budget — §9.2.)*

### 4.8 Deployment / infrastructure
- **Control plane + state** (API, orchestrator, Postgres) → **krg Incus slot, Docker-
  Compose**, NixOS-converged, like v1's `deploy/incus/compose.yml`. **No kube here.**
- **Processor** → **Kubernetes**, kustomize: **NRP `amd64`** today (add **GPU** requests
  for models), → junkyard/Pixel-Fold **ARM64** later. **Multi-arch images**; ARM64/Knative
  are **processor-only, future** — not near-term control-plane concerns.
- **Garage** (external, `s3.e4e.ucsd.edu`) and **Temporal** (shared krg-prod cluster,
  **mTLS**, **single `fishsense` namespace** — tenant scoping is in-workflow, not per-namespace;
  §9.4).
- **Reuse krg patterns**: OpenBao-rendered secrets, mTLS Temporal client certs
  (CN `fishsense-worker`), app-password service accounts, and the release-please →
  promote → `nixos-rebuild` / `kubectl apply -k` CI pipeline.

## 5. Phase 0 — prerequisites in `fishsense-core`
- **Port the image-preprocessing pipeline to Rust** (raw decode, auto-gamma, CLAHE,
  undistort, JPEG — currently Python rawpy/skimage/cv2), **gated on measurement parity**
  against the current path (gamma/CLAHE feed segmentation + length; the parity validation,
  not the coding, is the cost).
- **Untangle `core → fishsense_api_sdk`** (`RectifiedImage` imports `CameraIntrinsics`
  from the API SDK; core must not depend on the API).
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
- **Lab-as-tenant at parity:** backfill existing data into a **primary lab tenant**,
  re-home Garage objects under the tenant layout; when v2 reaches parity for the research
  loop, the PhD workflow continues *inside* v2 as that tenant — no blocking cutover.
- **Discipline:** algorithm/model changes → shared `fishsense-core` / MLflow;
  control-plane / schema / storage-layout changes → **v2, not v1**.

## 7. What to do in v1 *now* (churn) vs. v2

**Now, in `fishsense-lite` (helps the PhD + de-risks v2 — all on the shared/processing axis):**
- **Append-only, versioned measurements** recording `core_version` + `model_version` →
  reproducible, publication-traceable lengths. (Replaces the destructive upsert.)
- **Land the prepared models via MLflow (artifacts on Garage)**, referenced by the shared
  layer — not stranded in v1 worker code.
- **Add GPU requests** to `deploy/k8s/data-worker` for the models.
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
  model(s) in `fishsense-core` / MLflow. **No changes** to tenancy, auth, core `Capture`,
  ingestion, or the measurement/versioning model.
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

**9.2 — Model packaging** *(§4.7)* — *resolved*
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

### B. Decide during the build

**9.5 — Postgres durability / HA** — *deferred until the phone-cluster move (moves with 9.7)*
- **No HA work until we move to the phone cluster.** HA only becomes necessary when the
  control plane lands on **volatile** phone-cluster nodes; on the current single Incus slot
  it isn't worth the complexity.
- *Interim posture:* single-instance Postgres + **scheduled logical backups** (reuse v1's
  backup-worker pattern — nightly `pg_dump`, retention 14). Backups **are** the durability
  mechanism until HA arrives.
- *Consequence to handle now:* v1 backs up `pg_dump` → **NAS**, which is being **retired**
  (§4.6) → **the backup target must move to Garage.**
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

## 10. Reference links
- Current service (Lite): https://github.com/UCSD-E4E/fishsense-lite/
- Mobile app: https://github.com/UCSD-E4E/fishsense-mobile/
- Measurement algorithms (Rust + PyO3): https://github.com/UCSD-E4E/fishsense-core
- Pixel Edge-TPU driver (Rust): https://github.com/junkyard-computing/pixel-finch
- Authentik / infra (Incus/Compose/NixOS, no kube): https://github.com/KastnerRG/krg-infra
- Data-processing cluster: https://nrp.ai/
- Temporal Rust SDK: https://docs.temporal.io/develop/rust
- OCEANS 2025 (Mobile): http://kastner.ucsd.edu/wp-content/uploads/2025/08/admin/oceans2025-fishsenseMobile.pdf
- https://e4e.ucsd.edu/fishsense/ · /fishsense-lite · /fishsense-mobile · /fishsense-scout

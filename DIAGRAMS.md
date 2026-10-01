# FishSense Services — v2 Diagrams

Companion to [PLAN.md](PLAN.md). Mermaid renders natively on GitHub.

---

## 1. Deployment / component view

Where each tier actually runs. Note the control plane is **Incus + Docker-Compose (no kube)**;
only the **processor** is Kubernetes, and it **floats** (NRP today → phone cluster later).
Orchestrator and processor never call each other: both poll Temporal. The orchestrator only
scales the processor Deployments.

```mermaid
flowchart TB
    subgraph clients["Clients"]
        web["Web app<br/>TypeScript / Next.js<br/>confidential OIDC"]
        mob["fishsense-mobile<br/>Flutter + Rust, iOS<br/>public OIDC + PKCE<br/>OFFLINE capable"]
    end

    subgraph krgshared["krg-prod — shared lab services"]
        authentik["Authentik<br/>OIDC + invite enrollment"]
        temporal["Temporal<br/>mTLS, namespace 'fishsense'"]
        mlflow["Model registry<br/>MLflow, or Garage model-weights<br/>(PLAN §9.12)"]
        bao["OpenBao<br/>secrets"]
    end

    subgraph slot["krg Incus slot — Docker Compose (NO kube)"]
        traefik["Traefik + Authentik outpost"]
        api["API<br/>Python / FastAPI<br/>tenant scoping, RBAC, RLS"]
        orch["Orchestrator<br/>Temporal workflow worker"]
        pg[("Postgres<br/>tenant_id + RLS")]
    end

    subgraph e4e["e4e — owns the data"]
        garage[("Garage S3<br/>durable, tenant-partitioned<br/>single-node today")]
    end

    subgraph nrp["NRP / Nautilus — k8s, amd64 (TODAY)"]
        proc["Processor<br/>Temporal activity worker<br/>cpu / light / gpu queues<br/>fishsense-core + models"]
    end

    subgraph phones["Phone cluster — k8s, ARM64 (LATER)"]
        proc2["Processor<br/>+ pixel-finch Edge TPU"]
        garage2[("Garage<br/>separate store")]
    end

    web --> traefik
    mob --> traefik
    web -.->|OIDC| authentik
    mob -.->|OIDC + PKCE| authentik
    traefik --> api
    api --> pg
    api -->|presigned URLs| garage
    web -->|direct upload| garage
    mob -->|direct upload| garage
    api -->|enqueue| temporal
    orch -->|workflows + schedules| temporal
    proc -->|polls activity queues| temporal
    orch -.->|scales 0↔N| proc
    proc --> garage
    proc -->|pull pinned model| mlflow
    proc --> api
    bao -.->|secrets| slot
    proc2 -.->|future| garage
    proc2 -.->|future, polls| temporal

    classDef future stroke-dasharray: 5 5
    class proc2,garage2 future
```

---

## 2. Domain model — tenancy, devices, captures

Every domain entity carries `tenant_id` (enforced by app scoping **and** Postgres RLS).
Device-specific data hangs off `Capture` via a per-`DeviceKind` extension. Laser calibration
and the dive slate are **per dive**, not per capture. They belong to the Calibration entity
(PLAN §4.3) and are referenced from each Measurement.

*Not yet drawn* (PLAN §4.3, §9.21):
- **device components** (housing/port, air lens, laser mount);
- **camera calibration** kept separate from laser calibration;
- per-dive **water conditions**;
- the **Session/Experiment** grouping used by the research repos.

```mermaid
classDiagram
    class Tenant {
        +uuid id
        +string slug
        +TenantKind kind
    }
    class User {
        +uuid id
        +string authentik_sub
        +string email
    }
    class Membership {
        +uuid tenant_id
        +uuid user_id
        +Role role
    }
    class Device {
        +uuid id
        +uuid tenant_id
        +DeviceKind kind
        +string serial
    }
    class Dive {
        +uuid id
        +uuid tenant_id
        +uuid uploader_id
        +datetime dive_datetime
    }
    class Capture {
        +uuid id
        +uuid tenant_id
        +uuid client_capture_uuid
        +datetime captured_at
        +geo location
        +string raw_object_key
        +string checksum
        +CaptureStatus status
    }
    class CaptureExtension {
        <<abstract>>
        +uuid capture_id
    }
    class LiteExtension {
        +ref camera_intrinsics
    }
    class MobileExtension {
        +string depth_object_key
        +int depth_width
        +int depth_height
        +int depth_stride
        +string confidence_key
        +bytes intrinsics_K
    }
    class MonoExtension {
        +string model_inputs_key
    }

    Tenant "1" --> "*" Membership
    User "1" --> "*" Membership
    Tenant "1" --> "*" Device
    Tenant "1" --> "*" Dive
    Tenant "1" --> "*" Capture
    Dive "1" --> "*" Capture
    Device "1" --> "*" Capture
    User "1" --> "*" Capture : uploaded_by
    Capture "1" --> "0..1" CaptureExtension
    CaptureExtension <|-- LiteExtension
    CaptureExtension <|-- MobileExtension
    CaptureExtension <|-- MonoExtension
```

---

## 3. Results, provenance & annotations

`Measurement` is **append-only** — every row records exactly what produced it, so a length is
always traceable and re-runnable. This is the core reproducibility contract.

*Not yet drawn* (see PLAN §4.3):
- **Prediction** and **Calibration** entities (producer, gates, refusal);
- the calibration and label ids each Measurement must name;
- `Label.source` (human / auto-accept / model).

"Current" semantics are open (PLAN §9.13).

```mermaid
classDiagram
    class Capture {
        +uuid id
        +uuid tenant_id
    }
    class Measurement {
        <<append-only>>
        +uuid id
        +uuid tenant_id
        +uuid capture_id
        +float length_m
        +float uncertainty
        +string algorithm
        +string algorithm_version
        +uuid run_id
        +string core_version
        +string model_version
        +MeasurementSource source
        +datetime created_at
    }
    class ModelVersion {
        +string model_version
        +string server_artifact_uri
        +string mobile_artifact_uri
    }
    class Fish {
        +uuid id
        +uuid species_id
    }
    class Species {
        +string scientific_name
        +string common_name
    }
    class Label {
        <<abstract>>
        +uuid capture_id
        +uuid user_id
        +bool superseded
    }
    class LaserLabel
    class HeadTailLabel
    class SpeciesLabel
    class DiveSlateLabel
    class FormTemplate {
        +uuid id
        +uuid tenant_id
        +int template_version
        +json schema
    }
    class FormResponse {
        +uuid capture_id
        +int template_version
        +string tag_id
        +json answers
    }

    Capture "1" --> "*" Measurement : append-only
    Measurement ..> ModelVersion : produced_by
    Measurement "*" --> "0..1" Fish
    Fish "*" --> "0..1" Species
    Capture "1" --> "*" Label
    Label <|-- LaserLabel
    Label <|-- HeadTailLabel
    Label <|-- SpeciesLabel
    Label <|-- DiveSlateLabel
    Capture "1" --> "0..1" FormResponse
    FormTemplate "1" --> "*" FormResponse
```

---

## 4. Sequence — FishSense Lite dive-batch ingestion

Two-phase (`reserve → upload → commit`); the API never touches the bytes.

```mermaid
sequenceDiagram
    actor U as Lab member
    participant W as Web app
    participant A as API
    participant G as Garage
    participant T as Temporal
    participant P as Processor
    participant DB as Postgres

    U->>W: offload SD card, start upload
    W->>A: POST /dives:reserve (tenant, device, metadata)
    A->>DB: create Dive + Captures (status=reserved)
    A-->>W: capture ids + presigned multipart URLs

    loop per image (resumable)
        W->>G: PUT raw part(s)
    end

    W->>A: POST /dives/{id}:commit (checksums)
    A->>DB: status=committed
    A->>T: start workflow(tenant_id, dive_id)

    T->>P: activity: preprocess (decode, gamma, CLAHE, undistort)
    P->>G: GET raw
    T->>P: activity: calibrate + segment + measure
    P->>P: fishsense-core (+ pinned model)
    P->>A: POST measurements (append-only + provenance)
    A->>DB: INSERT Measurement (core_version, model_version, run_id)
    Note over DB: prior measurements retained — history preserved
```

---

## 5. Sequence — Mobile offline capture, then sync

Capture works fully offline with **no network and possibly expired tokens**. Sync is
**one-way upload**; only form templates flow server → mobile.

```mermaid
sequenceDiagram
    actor R as Researcher
    participant M as fishsense-mobile
    participant L as Local SQLite + files
    participant A as API
    participant G as Garage
    participant T as Temporal

    rect rgb(240,240,240)
        Note over R,L: OFFLINE — in the field
        R->>M: capture fish
        M->>M: ARKit image + LiDAR depth + intrinsics
        M->>M: fishsense-core on-device (ONNX + CoreML)
        M->>L: store capture + length<br/>client_uuid, model_version, template_version
    end

    Note over M,A: LATER — on WiFi
    M->>A: refresh token / re-auth (OIDC)
    A-->>M: access token
    M->>A: GET /form-templates (versioned)
    A-->>M: templates (cached offline)

    M->>A: POST /captures:reserve (client_uuid, tenant)
    A-->>M: presigned multipart URLs
    loop resumable, WiFi only
        M->>G: PUT raw image + depth + confidence
    end
    M->>A: POST /captures:commit (metadata + on-device measurement)
    A->>A: upsert on (tenant_id, client_uuid) — idempotent
    A->>T: optional server-side recompute / validate
    Note over M: local retention: keep on device,<br/>user may clear manually
```

---

## 6. Sequence — Partner onboarding and tenant scoping

How an external partner becomes a scoped identity, and how a request gets isolated.

```mermaid
sequenceDiagram
    actor Admin
    actor P as Partner
    participant AK as Authentik
    participant W as Web app
    participant A as API
    participant DB as Postgres

    Admin->>AK: create invitation<br/>fixed_data: attributes.tenant/org
    AK-->>Admin: invite link (itoken)
    Admin->>P: send link

    P->>AK: open link → enrollment flow
    AK->>AK: user_write (external, inactive)
    AK->>P: verification email
    P->>AK: confirm → account activated
    Note over AK: local account, NOT in AD<br/>tenant/org stored as user attribute

    P->>W: sign in
    W->>AK: OIDC (scopes: openid profile email org)
    AK-->>W: id/access token (sub + org claim)
    W->>A: request + JWT

    A->>A: validate JWT, read stable sub
    A->>DB: lookup User + Membership by sub
    DB-->>A: tenant_id + role
    A->>DB: SET app.tenant_id for this request
    Note over DB: RLS policies scope every row
    DB-->>A: tenant-scoped rows only
    A-->>W: response
```

---

## 7. Capture lifecycle

*Draft. This mixes upload state with processing state and omits states v1 has proved
necessary: priority park, calibration refusal, awaiting labels, and "tried, made no
progress". Being reworked under PLAN §9.16.*

```mermaid
stateDiagram-v2
    [*] --> Reserved: reserve (ids + presigned URLs)
    Reserved --> Uploading: client PUTs to Garage
    Uploading --> Uploading: resume failed parts
    Uploading --> Committed: commit (checksums verified)
    Reserved --> Expired: never committed
    Uploading --> Expired: abandoned

    Committed --> Queued: workflow enqueued
    Queued --> Processing: activity picked up
    Processing --> Measured: Measurement appended
    Processing --> Failed: activity error
    Failed --> Queued: retry (Temporal)

    Measured --> Queued: re-run (new algorithm / model)
    note right of Measured
        Re-runs APPEND a new
        versioned Measurement.
        Current = latest by created_at.
        History is never destroyed.
    end note

    Expired --> [*]
    Measured --> [*]
```

---

## 8. Processor — dive-level stage pipeline (current `fishsense-lite`)

*Snapshot: `fishsense-lite` @ `a8b2c3bc` (2026-09-21). Source of truth: v1's `CLAUDE.md`
(stage table) and `fishsense-api-workflow-worker/worker.py` (schedules).*

The pipeline is **not one long automated run**. It is four label tracks (laser, species,
head/tail, slate), each with a **model-assisted** Label Studio loop, feeding **calibration →
depth → measure**. The orchestrator drives it with **21 hourly schedules**:
- 17 staggered by minute offset, `overlap=SKIP`;
- 4 Label Studio syncs, `ALLOW_ALL`.

Each firing selects **one dive** (`select-next/*`, `priority=HIGH`, `ORDER BY id`).
Calibration, depth and measure select on **mismatch** — a row whose recorded calibration is no
longer the dive's current one — so a recalibration re-drains everything downstream on its own.

```mermaid
flowchart TB
    ingest["Ingest (operator-run)<br/>NAS folder → md5 → Image rows<br/>dive LOW → finalize → HIGH"]
    raw[("Garage fishsense-lite · raw/ scratch<br/>staged from NAS per dive, deleted after use<br/>read by 0.1 · 2 · 5.1 · 9 · checkerboard")]
    jpg[("Garage labels-fishsense-lite<br/>durable JPEGs written by 0.1 · 2 · 5.1 · 9<br/>presigned by Label Studio")]
    wts[("Garage model-weights<br/>+ laser detector baked in image")]

    subgraph laser["Laser track"]
        s01[":00 · 0.1 preprocess laser"]
        pl[":10 · predict laser (GPU)<br/>LaserPrediction"]
        aa[":22 · auto-accept gate<br/>fits dive's own line"]
        s03[":12 · 0.3 populate LS"]
        ll["LS: laser review<br/>declined + audit-sampled frames"]
        sl["sync → LaserLabel<br/>RANSAC validator supersedes outliers"]
    end

    subgraph species["Species track"]
        s1[":05 · 1 cluster dive frames"]
        s2[":15 · 2 preprocess species"]
        s4[":20 · 4 populate LS<br/>pre-annotated from stored judgements"]
        ls["LS: species labeling<br/>(all human)"]
        s42["4.2 sync → SpeciesLabel / Fish"]
        s61["6.1 regroup (on demand)"]
    end

    subgraph headtail["Head/tail track"]
        s51[":30 · 5.1 preprocess headtail"]
        ph[":32 · predict headtail<br/>SAM 3.1 on laser-centred crop<br/>(CPU ONNX fallback)"]
        s53[":34 · 5.3 populate LS"]
        lh["LS: head/tail review<br/>(every frame)"]
        sh["sync → HeadTailLabel"]
    end

    subgraph slate["Slate track"]
        s9[":45 · 9 preprocess slate"]
        s11["11 populate LS"]
        lsl["LS: dive slate labeling"]
        s12["12 sync → DiveSlateLabel"]
    end

    subgraph calib["Calibration → measure"]
        s13[":50 · 13 laser calibration<br/>from slate labels"]
        cb[":52 · checkerboard calibration"]
        gates{"4 gates: geometry ·<br/>self-consistency ·<br/>baseline 9.7–14.5 cm ·<br/>describes-dive"}
        ext[("LaserExtrinsics<br/>one per dive, overwritten")]
        ref[("Dive calibration refusal<br/>expires on newer label")]
        dep[":35 · laser depth<br/>on calibration mismatch"]
        s14[":40 · 14 measure fish<br/>on calibration mismatch"]
        meas[("Measurement<br/>upsert (image, fish)<br/>+ laser_extrinsics_id")]
    end

    ingest --> raw
    raw ~~~ jpg
    wts --> pl & ph

    s01 --> pl --> aa --> s03 --> ll --> sl
    s1 --> s2 --> s4 --> ls --> s42
    s42 -.-> s61
    sl --> s51
    s51 --> ph --> s53 --> lh --> sh
    s9 --> s11 --> lsl --> s12

    sl --> s13
    s12 --> s13
    s13 --> gates
    cb --> gates
    gates -->|pass| ext
    gates -->|refuse| ref
    ext --> dep
    sl --> dep
    ext --> s14
    sh --> s14
    s42 --> s14
    dep -.-> s14
    s14 --> meas

    classDef human fill:#ffd6e7,stroke:#c0396b
    classDef model fill:#d6ecff,stroke:#2b6cb0
    class ll,ls,lh,lsl,s61 human
    class pl,aa,ph model
```

*Pink = human-in-the-loop (Label Studio, hosted `app.heartex.com`, one project per dive per
stage). Blue = model output recorded as a prediction before any human sees it.* Not drawn:
`:25` reconcile-labeling-configs and `:55` scale-down-idle-data-worker. The data worker owns
**zero** schedules (so scale-to-zero can't drop them). The orchestrator scales its four
Deployments (cpu / light / gpu / gpu-cpu-fallback) 0↔N.

**What v2 must carry over from this picture** (PLAN §9.14, §9.16):
- the human steps;
- two calibration producers with gates and refusals;
- mismatch-driven recompute;
- per-dive priority as the commit/park flag.

---

## 9. Processor — per-capture compute path (`fishsense-core`)

The actual call chain. Two things worth reading off this: **where Lite and Mobile diverge**
(only in how depth is obtained) and **where the models have landed**. On the server, models
now propose laser points and head/tail and a human confirms them (§8). Mobile runs fully
automatic on-device.

```mermaid
flowchart TB
    start[("raw capture bytes")]

    d1["RawImage — rawpy decode"]
    d2["auto-gamma from HSV mean brightness"]
    d3["contrast stretch + CLAHE<br/>(CLAHE off by default)"]
    d4["RectifiedImage — cv2.undistort with K and dist"]

    human["SERVER TODAY — model-assisted, human-confirmed<br/>laser: detector + auto-accept gate<br/>head/tail: SAM 3.1 pre-annotation, human review<br/>(Label Studio)"]
    ml["MOBILE TODAY<br/>ONNX segmentation, then<br/>FishHeadTailDetector — PCA + polygon"]

    laser["LITE — laser triangulation<br/>calibrate_laser gives origin + axis<br/>compute_world_point_from_laser"]
    lidar["MOBILE — LiDAR sceneDepth f32<br/>mask-bounded RANSAC plane fit<br/>compute_world_point_from_depth"]

    wp["WorldPointHandler — K inverse<br/>project image point into 3D camera space"]
    len["length_m — distance snout to fork<br/>in world space"]
    out[("append-only Measurement<br/>core_version + model_version")]

    start --> d1 --> d2 --> d3 --> d4
    d4 --> human
    d4 --> ml
    human --> laser
    ml --> lidar
    human -.->|as models mature, review shrinks| ml
    laser --> wp
    lidar --> wp
    wp --> len --> out

    classDef pre fill:#def,stroke:#69c
    classDef dev fill:#efd,stroke:#9c6
    class d1,d2,d3,d4 pre
    class laser,lidar dev
```

*Blue = the preprocessing block moving from Python into the Rust core (Phase 0, `fishsense-core`
issue #54; still Python as of v4.0.0). Green = the device-specific step — the **only** place Lite and Mobile differ; both
converge on `WorldPointHandler` → length. That convergence point is exactly the extension seam
Mono/Multilens/Scout plug into. One caveat: `WorldPointHandler` is a **pinhole** (K⁻¹)
back-projection. The flat-port Lite (wuwnet, PLAN §8) is an axial refractive camera, so for it
the seam moves up a level, to a camera model dispatched in core.*

---

## Notes

- **Tenancy** is enforced twice: mandatory app-layer scoping *and* Postgres RLS keyed on a
  per-request `tenant_id` session variable.
- **The processor floats.** It is the only tier on Kubernetes, and the only place ARM64 /
  Edge TPU matter. The control plane never moves to kube in the near term.
- **Everything versioned:** `algorithm_version`, `core_version`, `model_version`,
  `template_version` — so any measurement can be explained and reproduced.
- Diagrams 4–6 all assume the **§9.1 contract**: v2-owned, and what the ported workers
  speak (v1 is retired at the big-bang cutover, PLAN §6).

# FishSense Services — v2 Diagrams

Companion to [PLAN.md](PLAN.md). Mermaid renders natively on GitHub.

---

## 1. Deployment / component view

Where each tier actually runs. Note the control plane is **Incus + Docker-Compose (no kube)**;
only the **processor** is Kubernetes, and it **floats** (NRP today → phone cluster later).

```mermaid
flowchart TB
    subgraph clients["Clients"]
        web["Web app<br/>TypeScript / Next.js<br/>confidential OIDC"]
        mob["fishsense-mobile<br/>Flutter + Rust, iOS<br/>public OIDC + PKCE<br/>OFFLINE capable"]
    end

    subgraph krgshared["krg-prod — shared lab services"]
        authentik["Authentik<br/>OIDC + invite enrollment"]
        temporal["Temporal<br/>mTLS, namespace 'fishsense'"]
        mlflow["MLflow<br/>model registry"]
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
        proc["Processor<br/>Temporal activity worker<br/>fishsense-core + models"]
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
    orch --> temporal
    orch -->|activities| proc
    proc --> garage
    proc -->|pull pinned model| mlflow
    proc --> api
    bao -.->|secrets| slot
    proc2 -.->|future| garage
    orch -.->|future| proc2

    classDef future stroke-dasharray: 5 5
    class proc2,garage2 future
```

---

## 2. Domain model — tenancy, devices, captures

Every domain entity carries `tenant_id` (enforced by app scoping **and** Postgres RLS).
Device-specific data hangs off `Capture` via a per-`DeviceKind` extension.

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
        +ref laser_extrinsics
        +ref dive_slate
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

The numbered stages as they exist today. Note this is **not one long automated pipeline** — it
is *preprocess → human labeling in Label Studio → sync labels back → calibrate → measure*.
The orchestrator drives it with hourly, staggered, `overlap=SKIP` schedules, each firing
selecting **one dive** via a `select-next/*` selector.

```mermaid
flowchart TB
    sel["Orchestrator selectors<br/>select-next/*, priority=HIGH<br/>one dive per firing, staggered hourly"]

    raw[("Garage: raw/ prefix, .ORF<br/>staged by api-worker, deleted after use")]
    jpg[("Garage: processed JPEGs<br/>preprocess_jpeg / groups / headtail / slate")]

    s01["stage 0.1 — preprocess laser images"]
    s1["stage 1 — cluster dive frames"]
    s2["stage 2 — preprocess species images"]
    s51["stage 5.1 — preprocess headtail images"]
    s9["stage 9 — preprocess slate images"]
    s13["stage 13 — perform laser calibration"]
    s14["stage 14 — measure fish"]

    l1["laser point labels"]
    l3["head/tail labels"]
    l4["dive slate labels"]
    l2["species labels"]

    ext[("LaserExtrinsics<br/>append, latest by created_at")]
    meas[("Measurement")]

    sel --> s01
    sel --> s1
    sel --> s2
    sel --> s51
    sel --> s9
    sel --> s13
    sel --> s14

    raw --> s01
    raw --> s2
    raw --> s51
    raw --> s9

    s01 --> jpg
    s2 --> jpg
    s51 --> jpg
    s9 --> jpg

    jpg -->|presigned read| l1
    jpg -->|presigned read| l3
    jpg -->|presigned read| l4
    jpg -->|presigned read| l2

    s1 --> s2
    l1 -.->|hourly sync workflow| s13
    l4 -.->|needs 2+ completed| s13
    s13 --> ext
    ext --> s14
    l1 -.->|sync| s14
    l3 -.->|sync| s14
    s14 --> meas

    classDef human fill:#fde,stroke:#c69
    class l1,l2,l3,l4 human
```

*Pink = human-in-the-loop.* The data worker owns **zero** schedules (so scale-to-zero can't
drop them) and is scaled 0↔1 by the orchestrator.

---

## 9. Processor — per-capture compute path (`fishsense-core`)

The actual call chain. Two things worth reading off this: **where Lite and Mobile diverge**
(only in how depth is obtained) and **where the incoming models land** (replacing human
keypoint labeling on the server, converging on what mobile already does).

```mermaid
flowchart TB
    start[("raw capture bytes")]

    d1["RawImage — rawpy decode"]
    d2["auto-gamma from HSV mean brightness"]
    d3["CLAHE — equalize_adapthist"]
    d4["RectifiedImage — cv2.undistort with K and dist"]

    human["SERVER TODAY<br/>human labels via Label Studio<br/>laser point + head/tail"]
    ml["MOBILE TODAY / SERVER SOON<br/>ONNX segmentation, then<br/>FishHeadTailDetector — PCA + polygon"]

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
    ml -.->|models landing here| laser
    laser --> wp
    lidar --> wp
    wp --> len --> out

    classDef pre fill:#def,stroke:#69c
    classDef dev fill:#efd,stroke:#9c6
    class d1,d2,d3,d4 pre
    class laser,lidar dev
```

*Blue = the preprocessing block moving from Python into the Rust core (Phase 0, `fishsense-core`
issue #54). Green = the device-specific step — the **only** place Lite and Mobile differ; both
converge on `WorldPointHandler` → length. That convergence point is exactly the extension seam
Mono/Multilens/Scout plug into.*

---

## Notes

- **Tenancy** is enforced twice: mandatory app-layer scoping *and* Postgres RLS keyed on a
  per-request `tenant_id` session variable.
- **The processor floats.** It is the only tier on Kubernetes, and the only place ARM64 /
  Edge TPU matter. The control plane never moves to kube in the near term.
- **Everything versioned:** `algorithm_version`, `core_version`, `model_version`,
  `template_version` — so any measurement can be explained and reproduced.
- Diagrams 4–6 all assume the **§9.1 contract**, which is v2-owned and only optionally
  adopted by v1.

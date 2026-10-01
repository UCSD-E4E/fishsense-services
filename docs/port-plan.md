# Porting the rest of v1: the plan every slice works from

Written 2026-09-27 for the two-week cutover at full parity (PLAN.md §6.1). The
per-slice maps of v1 are in `docs/port-map/<slice>.json`. They list every v1
workflow, activity, API endpoint, test, shared helper, contract, schema gap and
known gotcha, with file:line references into fishsense-lite (read it at
`origin/main`: #927 and #932 changed the laser slice).

## How a slice is ported (§6.3; same pattern as ingest, clustering, laser sync)

- **v1's tests first**, adapted only where v2 changes behaviour; each module
  names the fishsense-lite commit it came from. Every v2 change is pinned by a
  test that says why. Mutation-check anything that looks too easy.
- **The database side** is a tenant-scoped store in the API package (functions
  over an `AsyncConnection`), plus a catalog class extending `ServicePrincipal`.
  The cohorts are ported from v1's `dive_cohort_controller.py` predicates,
  tested on real Postgres. See `clustering_store.py` for the reference shape.
- **The orchestrator side** is a package with a `stage.py` declaring its
  `STAGE` (workflows, schedules with v1's minute, `build_activities(deps)`).
  Don't edit `worker.py` or `schedules.py`; the registry discovers stages.
  Activities are methods of a class given its catalog.
- **The processor side** speaks only the contract package. It never touches the
  database, the NAS or Label Studio. Register workflows and activities in
  exactly one processor role.
- **Contracts:** add your DTOs as a module in `fishsense-services-contracts`.
  Don't edit `schemas/` or `CONTRACT_VERSION`; integration bumps the version
  and publishes the schema once for all slices.
- **Migrations:** name yours `<slice>_NN_<what>.py`, with `revision = "<slice>_NN"`
  and `down_revision` set to the head you branched from. Integration re-chains
  them. Keep them additive.
- **Dependencies:** don't edit a `pyproject.toml` or `uv.lock`. Anything you
  need should already be in a foundation; if not, say so in your report.

## Decisions every slice uses

- **Integer ids.** Rows people or tools refer to by number get a `number bigint`
  (unique per table). For migrated rows it equals `v1_id`, and new rows take the
  next number above v1's maximum. Label Studio titles use `#{dives.number}`, as
  v1's do with its dive id, so every migrated project is still found by title.
  Research views expose `number` as v1's `id`.
- **Label Studio projects** are recorded, not found only by title:
  `label_studio_projects (tenant_id, dive_id, kind, ls_project_id, title)`.
- **The source of a new label row:**
  - `human`: a row seeded by populate for a labeler;
  - `auto_accept`: written or confirmed by the laser gate;
  - `import`: a sentinel;
  - `pre_annotation`: reserved for a model pre-annotation a labeler never
    touched. Not written until something can tell.
- **An effective calibration is v1's:** a dive's own accepted, plausible
  calibration wins; otherwise its link's. Plausible means a baseline of
  0.097–0.145 m (v1's `_plausible_extrinsics`). This fixes `effective_laser_calibrations`.
- **Append-only stays append-only.** Where v1 updates in place (the laser gate's
  verdict, the laser line every hour, the measurement binding), v2 appends,
  appends only on change, or adds a verdict table. It never grants UPDATE to
  keep a v1 shortcut. Say which in your PR.
- **Selectors** take the oldest candidate across every tenant the orchestrator
  serves (the clustering pattern). They skip on overlap.
- **Object storage:** one bucket, with every key under `tenants/{tenant_id}/`
  (§9.11). Existing v1 keys (processed JPEGs that Label Studio tasks and label
  `image_url`s point at) stay readable where they are.
- **The processor** is stood up per wake and torn down when idle. It is never
  kept scaled to zero (PLAN.md §3).
- **fishsense-core 4.1.0** in the processor. Model weights come from Garage
  `model-weights` through core's `WeightStore` (§9.12).
- **The laser validator** follows fishsense-core #88 and fishsense-lite #927: it
  judges the full population, superseded included, in v1's `(image_id, id)` order,
  which in v2 is `(captures.number, laser_labels.number)`, and writes
  `superseded_reason` and `noise_estimator`.

## Phases

1. **Foundations.** Built first, because every slice needs them:
   - integer numbers and the Label Studio project registry;
   - the calibration view fix;
   - the processor role registry, and fishsense-core with Garage weights in the
     processor;
   - the Label Studio write side (`populate_utils`: create or heal, S3 storage,
     import with dedupe and the `IMPORT_ISSUED` reconcile, publish);
   - the object store, raw staging and its cleanup;
   - NRP stand-up/tear-down with the GPU-to-CPU fallback, whose state has to
     survive deletion;
   - the taxonomy module;
   - the NRP wake step for clustering.
2. **Slices, in parallel.** laser, species, head/tail, slate and calibration,
   depth and measure, ops (checksum verification, labeling-config reconcile,
   backups, cert sync, image prune), web portal (with its API endpoints).
3. **Consumers.** `dive_pipeline_status` (mirroring every cohort, with a parity
   test), the v1-shaped research views, and a research role. Then a full
   migration rehearsal and measurement parity.

## The foundations: what a slice calls

Built in parallel, then integrated on `feat/foundations`. Where the
integration changed a foundation, what's written here is what's true.

**Label Studio**
- The only module that touches the SDK is `orchestrator/labels/label_studio.py`
  (`LabelStudioClient`). Every call backs off through 429s.
- It covers: workspaces; projects (list, get, create, update); S3 import
  storage; task listing and import; and predictions (`predictions`,
  `create_prediction`, for the backfills).
- Heartbeat through `sticky_heartbeat` / `heartbeat_again`, never a bare
  `activity.heartbeat()`. A bare one clears the import's `IMPORT_ISSUED` marker.
- Port the populate side through `orchestrator/labels/populate.py`, v1's
  `populate_utils`.
  - Each stage builds, in its own `stage.py`:
    `LabelProjects(catalog=LabelProjectCatalog(deps.engine, sub=deps.sub),
    label_studio=LabelStudioClient.from_settings(s), workspace=s.workspace,
    storage=LabelStudioStorageSettings())`.
  - `ensure_dive_project(tenant, dive, kind, suffix=, labeling_config_xml=)` finds
    the project in the registry first, then falls back to v1's title search,
    then creates and records it.
  - `import_tasks_and_record_labels` dedupes by URL and reconciles
    `IMPORT_ISSUED`.
  - Also: `publish_label_studio_project` and `ensure_project_shows_predictions`.
- Each kind keeps its own labeling-config XML and title suffix; they belong to
  the slice.
- **A task's image is the located JPEG:**
  `TaskImage(number=capture.number, image=<ObjectRef>, captured_at=...)`, where
  the ref is from `locate_processed_jpeg` below. Never build a URL yourself.
- **A project not tied to a dive** (the checkerboard lattice) is titled per
  tenant: v1's title for `lab`, `"{v1 title} ({tenant slug})"` for any other.
  Every tenant shares one Label Studio workspace, so a bare title search would
  find another tenant's project.
- v2-created projects register their S3 storage with no prefix
  (`FISHSENSE_LABEL_STUDIO_S3_PREFIX=""`), so one storage presigns both v1's
  keys and the tenant keys.

**Object store**
- `orchestrator/object_store/`. Only the orchestrator issues keys, through
  `ObjectLayout` (`tenants/{tenant_id}/...`).
- Raw frames:
  - In the parent workflow, call `object_store.steps.stage_raw(target)`, then
    dispatch the child, then `cleanup_raw(target)`.
  - Name the child with `object_store.readers.raw_scratch_reader_id`, so cleanup
    won't evict scratch another reader is still using.
- Processed JPEGs:
  - A resolver hands the processor
    `store.processed_jpeg_target(tenant, folder, checksum, from_v1=capture has a v1_id)`
    as the place to write. That target is wherever the JPEG already is, so a
    migrated frame's redraw overwrites v1's JPEG in place, as v1 did. Otherwise
    it's the tenant key.
  - Readers use `locate_processed_jpeg` (v1's key first for a migrated frame) or
    `has_processed_jpeg`.
- JPEG folders are the constants in `fishsense_services_contracts.object_store`
  (`LASER_JPEG_FOLDER` etc.).
- The processor side is `processor/object_store.py` (`ProcessorObjectStore`):
  `download_raw(ref)`, `download_processed_jpeg(ref)`, and
  `upload_processed_jpeg(ref)`. The upload writes a processed JPEG only (a UUID
  tenant, a known folder, a checksum filename) and nothing else. It can't delete.
- Settings: `FISHSENSE_OBJECT_STORE_*` on both sides.
  `LEGACY_LABELS_PREFIX` is required (`fishsense-lite` in production).

**Model weights**
- `processor/weights.py`: `GarageWeightStore.from_settings(ModelWeightsSettings())`
  with `fetch_weights(name, version)`. Every fetch is verified against
  fishsense-core's manifest.
- Build it when a role that loads models starts, so bad settings fail the pod.
- SAM 3.1 has no manifest entry in fishsense-core 4.1.0. The head/tail slice
  needs a core release that pins its sha256 and size, or a local manifest with
  them measured once.
- Weights are not in the object store's settings.

**Taxonomy**
- Python: `fishsense_services_contracts.taxonomy` (`is_measurable`, the slate
  markers, `LABELED_FISH_MODELS`, ...).
- SQL: `fishsense_services_api.taxonomy_sql` (`measurable_species_sql`,
  `rigid_target_sql`, `calibration_target_name_sql`). A test pins that the
  two agree.

**The processor on NRP**
- `orchestrator/nrp/`. In a parent, before dispatching to the processor, call
  `nrp.workflow.wake_per_image_processor()`, `wake_light_processor()` or
  `wake_gpu_processor()`.
- The GPU wake returns `gpu`, `cpu_fallback` or `unavailable`. On `unavailable`,
  skip staging and the child.
- A wake stamps the Deployment, and the hourly sweeper leaves it alone for
  `wake_grace_minutes`. Staging can take up to an hour, and that's fine.
- Scaling is a no-op without `FISHSENSE_NRP_KUBECONFIG_PATH` (locally, in e2e).

**Processor roles**
- `processor/registry.py`: a `<package>/stage.py` declares
  `Stage(name, role, workflows, activities)` on one role: `per_image` (capped at 2
  concurrent activities, a memory ceiling), `light` (8) or `gpu` (2).
- A workflow must call its activities by **string name**, so the registration
  check can see them.

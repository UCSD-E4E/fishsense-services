# Shadow size-constancy laser calibration (SAM 3.1): design

**Status: design for review (2026-10-21). Nothing here is built.** This is
deliverable 1 of the shadow-deployment spec. Its job is to collect evidence on
whether size constancy can replace the current laser calibration, without
changing anything production reports. The implementation PR (deliverable 2) and
the backfill report (deliverable 3) follow only after the author has reviewed
this, set the thresholds in §6, and committed the switching criteria in §8.

Sections marked **Author decides** are proposals, not settings.

## 1. Corrections to the spec, found while reading the sources

These change what the stage must reproduce, so they come first.

1. **"Median 0.023°" is not an angle between laser axes.** In
   `cscw-fishsense2027@8312780a analysis/codesign/size_constancy_sam.py` it is
   the median of |`err_deg`|. That is the signed offset of the fitted vanishing
   point from the stored one, **along the dive's laser line**, converted to
   degrees with a fixed `F_PX = 2850` (`slate_unknown.py:26`), not the
   camera's fx.
   - The 3D angle between the fitted and stored axes (`angle_deg`) has a median
     of **0.029°**, with 9/10 within 0.05°. Session 94 is the exception, at
     0.086°, on a tilted slate.
   - The reproduction check (§9.2) pins both numbers from
     `analysis/codesign/results/size_constancy_sessions.csv`. The stored results
     record both, plus the along-line offset converted with the camera's own fx.
2. **The script has no refusals.** It enforces no minimum frames, size spread,
   range span, SE or score floor beyond SAM's internal 0.3 confidence threshold.
   `TARGET = 0.05` is only reported (`within_target`).
   - The thresholds in §6 therefore come from `slate_unknown.py`
     (`MIN_FRAMES = 8`, size ratio ≥ 1.5), PAPER.md ("at least about 1.5× in
     distance"; "under 1.12× spread are unconstrained"), and the spec.
   - I found **no 0.6 m range-span criterion** in the paper or the code. It is
     new with this spec.
3. **Production's "human dots" are not filtered to human labels.** The laser
   calibration reads any laser label that is `NOT superseded` with x,y set
   (`laser_calibration_store._LIVE_DOT`, :202-208). That includes
   `source = 'auto_accept'` labels, and `completed` is not checked. Each slate
   frame takes its lowest-numbered such dot (`_LOWEST_LIVE_DOT`).
   - Dot source (a) uses **exactly this rule**, so the comparison with
     production is like for like. Each frame records its dot's label source.
4. **"laserextrinsics" is v1's name.** In v2 the stored calibrations are
   `laser_calibrations` (0008), resolved per mission by
   `effective_laser_calibrations` (0018: own, else borrowed through
   `missions.calibration_source_mission_id`).
5. **Dive 279's red-speck false positives are not documented anywhere** in this
   repo or its history.
   - The test that the detector gate must catch them (§5.3) needs the frames.
     **Author: please name the captures** (or a time range) where the specks
     appear. They will be recorded in this document and turned into a fixture.
6. **The GPUs that can run SAM 3.1 as configured** are Ampere or newer: the NRP
   A-class cards and the lab's A6000 and RTX 4500 Ada. Commit f987f46 found that
   a 2080 Ti (Turing) fails bfloat16 and then loses its CUDA context.

## 2. What already exists, and how this differs

Production already runs a size-constancy calibration: the automatic track's
label-free fit (`automatic_results/size_constancy.py`,
`AUTOMATIC_CALIBRATION_VERSION = "2"`). The shadow stage is a **different
method under different rules**:

| | Automatic track (existing) | Shadow stage (this design) |
|---|---|---|
| Apparent size | SIFT pairwise registration around the dot (`register.py` port) | √(SAM 3.1 slate-mask area) (`size_constancy_sam.py` port) |
| Dots | Detector dots only | (a) production's live label dots, and (b) gated detector dots, both recorded |
| Slate frames | Detector p ≥ 0.5 | Slate labels ∪ detector p ≥ 0.5, source recorded per frame |
| Results | `automatic_laser_calibrations`, **feeds automatic lengths** | New shadow tables, **feeds nothing** |
| Purpose | Lengths where no human calibration exists | Evidence to compare against production's calibration |

Both use the same estimator, `fit_tv` (`slate_unknown.py:70-73`): a bounded
Brent search for the vanishing position that makes `s / (t − t_v)` constant.
The backfill report (§10) also compares the shadow fits with the automatic
track's, since both now exist on the same missions.

## 3. Where the stage sits

A new stage, `shadow_calibration`, on each side, added through the registries
with no edits to shared files beyond registering it.

**Orchestrator** (`fishsense_services_orchestrator/shadow_calibration/`)
- `ShadowCalibrationParentWorkflow`, hourly when enabled (offset **+38**, which
  is unused). It runs:
  1. `select_next_mission_for_shadow_calibration`, which picks missions with
     slate frames and no current shadow result at this algorithm version,
     oldest first, across tenants.
  2. Then `ShadowCalibrationForMissionWorkflow` as a child (id
     `shadow-calibration-{mission}`, `ALLOW_DUPLICATE`).
- `ShadowCalibrationForMissionWorkflow` (also run by hand for one mission, or
  by a backfill CLI with a dry run and a reviewed plan, like `run_missions`):
  1. `resolve_shadow_calibration_inputs` (database, read-only). It returns:
     - slate frames with their source;
     - dots for both sources, with the detector gate's verdicts;
     - the dive line (production's `current_mission_laser_lines`), and the
       detector line;
     - the camera matrix;
     - each frame's render URL (presigned GET, as the automatic track does);
     - the production reference: the mission's `effective_laser_calibrations`
       row, its id, and whether it is borrowed.
  2. `wake_gpu_processor()`. On anything but a CUDA GPU it skips and reports,
     as the automatic track does.
  3. Child `SegmentSlateFramesWorkflow` on `PROCESSOR_GPU_TASK_QUEUE`, in chunks
     of ≤ 400 frames (the 2 MB payload precedent). It returns per-frame SAM
     results.
  4. `fit_shadow_calibration` on the light role, once per dot source (cheap:
     numpy and scipy).
  5. `persist_shadow_calibration`, which writes the shadow tables (§4) in one
     transaction.
- **Flag:** `ShadowCalibrationSettings` (env prefix
  `FISHSENSE_SHADOW_CALIBRATION_`), `enabled: bool = False`. Off, the stage has
  **no schedule**. Its workflows and activities stay registered so one mission
  can be run by hand in review, as with `FISHSENSE_AUTOMATIC_RESULTS_ENABLED`.
  - Schedules are never updated in place, so turning the flag off later means
    deleting `shadow-calibration` in Temporal (the runbook will say so).
  - **Enabling it in production, and any backfill, waits for the author.**

**Processor**
- `segment_slate_frame` (GPU role) reads the render by URL and downscales it to
  a 1600-px long side, as the port's render cache did (`render.py`: rectify,
  then downscale, JPEG q95).
- It runs SAM 3.1 through the existing pinned loader (`get_segmenter`, the
  head/tail stage's checkpoint `sam3/3.1@<sha12>`), with
  `ScoredSam3Adapter(..., prompt=…)`.
  - Prompts are tried in the port's order, `("slate", "white board")`, at the
    port's confidence threshold of **0.3**. On the pool, "white board" won 253
    of 258 frames.
  - It keeps the highest-scoring mask that contains the dot.
- It returns, per frame: prompt used, score, mask area, size = √area, the mask's
  box, and the SAM model id. It needs **no raw scratch**.
- The fit itself is a straight port (§7). It has no database access; results go
  back to the orchestrator.
- **Contract:** a new module, `shadow_calibration.py`, which bumps
  `CONTRACT_VERSION` to **13** (`schemas/v13.json`). The fields are new and in
  new models only.

**What the stage never does**
- It never INSERTs, UPDATEs or DELETEs in:
  - `laser_calibrations`, `laser_calibration_refusal_clears`, or
    `automatic_laser_calibrations`;
  - `laser_labels`, `slate_labels`, `laser_predictions`, `mission_laser_lines`,
    or `slate_presence_predictions`;
  - any `missions` column, including `calibration_source_mission_id`, the
    slate and target ids, and `calibration_links_changed_at`.
- It never calls `record_laser_calibration` or any catalog method behind it.
- No existing view references the shadow tables, and none is changed.

## 4. The tables (one migration, the next free number at merge time)

Append-only (`GRANT SELECT, INSERT`), forced RLS with the canonical tenant
policy, research read on the lab's rows, as 0053 does. A `current_*` view picks
the live row.

**`shadow_laser_calibrations`**: one row per mission × dot source × run.

| Column | Meaning |
|---|---|
| `id`, `tenant_id`, `seq`, `created_at` | as every append-only table |
| `mission_id` | |
| `dot_source` | `'labels'` (production's live-dot rule) or `'detector'` (gated) |
| `outcome`, `refusal_reason` | `'accepted'` / `'refused'`; reason from §6 |
| `laser_position`, `laser_axis` | 3-arrays, accepted only; origin from the assumed `o_mag_m` (§7.3) |
| `o_mag_m` | assumed laser offset: 0.104 m, the fleet median (score.py `O_DESIGN_M`) |
| `vanishing_px`, `line_direction`, `line_offset_px`, `line_source` | the fit along its line; `line_source` is `'mission_line'`, `'detector_line'` or `'dots'` |
| `reference_laser_calibration_id`, `reference_borrowed` | production's `effective_laser_calibrations` row when the fit ran (NULL if none) |
| `angle_diff_deg` | 3D angle between the shadow and reference axes |
| `vp_offset_px`, `vp_offset_deg_f2850`, `vp_offset_deg_fx` | signed along-line offset of the vanishing point from the reference's, in px, and in degrees at F=2850 (the paper's metric) and at the camera's fx |
| `fit_residual` | SD of `log s − log(t − t_v) − c` over frames used |
| `se_px`, `se_deg` | bootstrap SE (500 draws, seeded per mission and source) |
| `frames_used`, `frames_rejected` | count, and `jsonb` {reason: count} |
| `size_spread` | max/min size over frames used |
| `range_min_m`, `range_max_m`, `range_span_m` | the frames' ranges under the fitted calibration (§7.3) |
| `sam_score_min`, `sam_score_median` | over frames used |
| `sam_model`, `sam_prompts`, `sam_threshold`, `render_long_side` | e.g. `sam3/3.1@ab12cd34ef56`, `{slate,"white board"}`, 0.3, 1600 |
| `gate` | `jsonb`: the detector gate's thresholds and per-reason rejection counts (detector source) |
| `algorithm_version`, `code_version` | the stage's version, and the release and commit it ran under |

**`shadow_calibration_frames`**: one row per frame considered, so every check
in §10 can be recomputed without re-running SAM.

| Column | Meaning |
|---|---|
| `shadow_laser_calibration_id`, `capture_id` | |
| `frame_source` | `'label'`, `'detector'`, or `'both'` (slate label vs presence classifier) |
| `slate_probability` | the classifier's p, where scored |
| `dot_x`, `dot_y`, `dot_label_id`, `dot_label_source` | for labels: the label and its `source`; for the detector: its prediction id |
| `line_distance_px` | dot's distance from the line the fit used |
| `sam_prompt`, `sam_score`, `mask_area_px`, `size` | at 1600-px scale; `size = √area` |
| `t_px`, `range_m` | position along the line; range under the fitted calibration |
| `used`, `rejected_reason` | e.g. `no_mask_at_dot`, `sam_score_low`, `off_line`, `detector_conf_low`, `ambiguous_line`, `out_of_region` |
| `capture_number`, `captured_at` | for the start/end split (§10.3) |

## 5. Dot sources and the consistency gate

### 5.1 (a) Label dots
These are production's rule exactly (§1.3): per slate frame, the lowest-numbered
live laser label (`NOT superseded`, x,y set), with its label source recorded.

**Laser line:** production's `current_mission_laser_lines`. This is what the
port used (`lines.psv` is production's dive line), with the offset as the median
normal coordinate of the frames' dots (`register.py:153-154`).

### 5.2 (b) Detector dots
**Source:** the current `laser_predictions` row per capture. This is production's
detector on the human path, `LASER_PREDICTOR_VERSION = 2`, which already rejects
dots outside `LASER_REGION_POLYGON` (`rejected_out_of_region`).

### 5.3 The consistency gate, proposed. **Author decides** the values.
In order, each rejection recorded with its reason:

1. **Out of region:** already applied by the detector stage, and recorded.
2. **Presence confidence < 0.5:** the port's `prediction_lines()` floor. This
   alone does not triage reef frames: on 442, 49% of off-line predictions score
   ≥ 0.99. Hence step 3.
3. **Line agreement:** the dot must be within **10 px** of the detector line.
   - The detector line is a RANSAC line through every gated-so-far detector dot
     of the dive (500 iterations, 10 px tolerance, `default_rng(0)`, then TLS
     through the inliers), as the port's `sam3_predline` builds it.
   - 10 px is `OFF_LINE_PX`: detector dots land 1–2 px from human ones, and
     session 77's reflection block sat at 35–41 px.
4. **Ambiguous line:** if the rejected dots form a second line of ≥ 8 dots and
   ≥ 25% of the kept dots, refuse the dive (`ambiguous_line`, as
   `SECOND_LINE_SHARE`). Laser and reflection can't be told apart without labels.
5. **Range consistency:** after the fit, drop a frame whose deleted residual
   (the fit without it) exceeds 0.10 in log size, one at a time, re-fitting
   each time (`trim_frames`). This catches a dot on the line at the wrong range,
   as on mission 80.
   - **This is an addition the port lacks.** It is recorded as a separate
     variant (§7.4), so the port itself stays reproducible.

**Dive 279's red specks:** a test fixture built from the author's named frames
(§1.5). It must show them rejected at step 2 or 3, or the dive refused at step 4.
If the specks sit on the line at plausible ranges, none of these steps can catch
them, and the design comes back for review.

## 6. Refusal thresholds, proposed. **Author decides**

A source's fit is refused, with the first reason that applies, when:

| Reason | Proposed rule | Source |
|---|---|---|
| `too_few_frames` | < 8 frames used | `slate_unknown.MIN_FRAMES` |
| `too_little_size_spread` | max/min size < 1.5 | PAPER.md §4.3; `slate_unknown` |
| `too_little_range_span` | range span < 0.6 m (under the fitted calibration) | the spec; no precedent found |
| `se_too_high` | bootstrap SE > 0.05° along-line (F=2850) | `TARGET`; the paper's v-to-2.5 px |
| `sam_scores_low` | median score < 0.5, or > 25% of frames lack a mask under the dot | 0.5 is production's SAM gate; the port's floor is 0.3 |
| `unconstrained` | optimum at the search bracket's edge | `size_constancy._pinned` |
| `ambiguous_line` | §5.3 step 4 (detector source) | session 77 |
| `no_reference_line` | no mission line and < 3 dots to fit one | |

The range span depends on the fit, because a range is computed from the fitted
calibration. So it is checked after the fit, as a refusal rather than a
precondition.

## 7. The fit: a port, not a reimplementation

### 7.1 What is ported, verbatim in behaviour
From `cscw-fishsense2027@8312780a`:
- `slate_unknown.fit_tv`, `frame`, `F_PX`, `MIN_FRAMES`;
- `size_constancy_sam.py`: the per-variant fit, the bootstrap (500 draws,
  `default_rng([dive, variant_index])`), the fitted axis `K⁻¹[t_v u + off n, 1]`,
  and both error metrics;
- `prediction_lines()` for the detector line;
- `score.calibrations()` for the origin from |O|.

Each ported function's docstring names its source file and line, per the repo
convention.

### 7.2 What is not in the script and is taken from elsewhere
- The SAM pass (`sam_sizes.py`): prompts, the 0.3 threshold, the dot-containing
  highest-score mask, √area, and the 1600-px rectified render.
- The laser line offset (`register.py:153-154`).

### 7.3 Origin and ranges
The script fits **only the axis direction**. The origin uses the assumed
|O| = 0.104 m along the line direction in normalised coordinates
(`score.calibrations`). Ranges for §6 come from intersecting each frame's dot ray
with the fitted beam. They are therefore **conditional on the assumed |O|**,
which is recorded.

The backfill report states this, and does not compare origins with production's
fitted |O| as if they were independent.

### 7.4 Variants recorded
1. **`port`:** exactly the script, no trimming and no refusals. This is the
   reproduction and the baseline.
2. **`gated`:** the port with §5.3 and §6 applied. This is what the switching
   criteria judge.

Both are stored, distinguished by `algorithm_version`, so the refusals'
effect is visible rather than baked in.

## 8. Switching criteria, proposed. **Author decides and commits before the backfill**

Proposed candidates, to be judged per dot source, on the `gated` variant, over
missions where both the shadow fit (accepted) and a production calibration exist:

1. **Agreement:** at least **90%** of missions have along-line |offset| ≤ 0.05°
   at F=2850. The 95% CI lower bound, resampling whole missions, is at least 80%.
2. **No silent disagreement:** no accepted mission with |offset| above the
   length-budget angle. Proposed: 0.15°, the `slate_unknown` second tier.
   Beyond 0.05° the paper's 2.5 px budget is spent.
3. **Refusals carry the disagreements:** among missions where the `port` variant
   disagrees by more than 0.15°, at least 80% are refused by `gated`.
4. **Coverage:** state the share of missions with any slate frames that get an
   accepted fit. **No threshold is proposed here.** The author sets one if
   replacing calibration requires it.

**The reference is production's stored calibration, not ground truth.** Where
tape-measured lengths exist (the SMILE paired fish), the report also gives length
error under both calibrations. The criteria above don't use it, because that
sample is n = 7.

## 9. Tests (deliverable 2)

### 9.1 Measurement output identical, flag on and off
On the real-Postgres depth and measure harness
(`tests/test_depth_measure_over_temporal.py`, `depth_measure_seed.py`):
1. Seed a calibrated mission with slate frames.
2. Run depth and measure. Dump, ordered and with ids, these tables:
   `laser_calibrations`, `effective_laser_calibrations`,
   `mission_laser_geometry`, `laser_depths`, `laser_depth_refusals`,
   `measurements`, `measurement_refusals`, `current_measurements`, and the
   `automatic_*` tables and views.
3. Run the shadow stage end to end (stubbed SAM), then depth and measure again.
4. Assert every dump is **byte-identical**.

Plus a schema test: no view or function depends on the shadow tables
(`pg_depend`), and the orchestrator's shadow activities hold no write path to
any table in §3's list (a source scan, as `test_database_ownership.py` does).

### 9.2 Pool reproduction
A golden fixture is built from the cscw inputs:
- `sizes/sam3_text.csv`, `sizes_frames.csv`, `labelfree_tv.csv`;
- `e2e_measurement/extrinsics.psv`, `laser_detection_analysis/intrinsics.csv`,
  `predictions.csv` and `sample.csv`.

The ported fit must reproduce, for sessions 62, 63, 65, 71, 77, 80, 83, 87, 94
and 114:
- every `err_deg` and `angle_deg` in `size_constancy_sessions.csv` to 1e-6;
- median |`err_deg`| **0.02322**, median `angle_deg` **0.029**, 10/10 within
  0.05 on `err_deg`;
- the `sam3_predline` variant's median angle **0.047**.

### 9.3 SAM pass reproduction (GPU, marked `integration`)
Re-run the processor's `segment_slate_frame` on the 258 pool frames with the
pinned SAM 3.1 checkpoint. Then require:
- sizes within 0.1% of `sam_sizes_detail.csv` (its commit fe7bdd83 found
  3.1 ≈ v1 to 1.0006);
- the same 5 no-mask frames on dive 114 (16038, 16040, 16041, 16042, 16045).

This runs locally (the checkpoint and the render cache are present), not in CI.

### 9.4 Usual guards
- `test_schema_audit`, `test_models_match_migrations`;
- the pinned Temporal names;
- the contract schema v13;
- the flag defaults off (`deploy/tests`);
- mutation checks on every gate and refusal.

## 10. The four checks (deliverable 3), each a script in the repo

All are computed per dot source, from the stored rows alone, with every number
in the report produced by `analysis/shadow_calibration/*.py` at a pinned commit.

1. **Agreement:**
   - distribution of the along-line offset and of the 3D angle against
     production's calibration;
   - 95% CIs by resampling whole missions (10,000 draws, seeded);
   - split by own vs borrowed reference.
2. **Invariants:**
   - dots on the line (distance distribution);
   - fit residual;
   - **length constancy**: the slate's known template size measured at each
     frame's range under the shadow calibration, as a % deviation across range.
     The slate is the rigid object of known, constant size on every such dive.
3. **Start-to-end consistency:** on missions with slate frames in both the
   first and last thirds of the capture sequence, fit each end separately (same
   rules) and report the axis difference, against its bootstrap SE.
4. **Refusals:** the rate per reason. Then the agreement of `port`-variant fits
   on refused missions, against accepted ones (does refusal catch the
   disagreements?).

## 11. Compute cost

**Not yet measured. The implementation measures it before the backfill is
proposed.**
- **SAM:** the repo records ~18 s per automatic frame, which is mostly raw
  decode (15–18 s). The shadow stage reads pre-made renders, so it has no decode.
  SAM alone on a 1600-px image on an A6000-class card is expected to take
  seconds per frame.
- **Frames:** slate frames per mission run 16–60 (paper sessions 17–33; session
  77 had 60).
- **Per mission:** roughly 1–5 minutes of GPU per mission, plus about 1 s of
  CPU for the fit (500 draws × 2 sources × 2 variants).
- **Backfill:** the slate detector has scored 69,162 frames over 317 missions.
  Only missions with slate frames run. At those numbers that's hours of GPU, not
  days, and **only once the GPU is otherwise idle** (the rule: no extra GPU until
  the first is fully used).

## 12. Open questions for the author

1. Dive 279: which captures show the red specks (§1.5)?
2. Thresholds in §5.3 and §6, and the criteria in §8: accept, change, or replace?
3. Detector dot source: the human-path detector (`laser_predictions`, proposed),
   or the automatic track's dots (`automatic_head_tail_predictions`, the same
   detector without the human path's history)?
4. Is the assumed |O| = 0.104 m acceptable for origins and ranges, or should the
   stage borrow |O| from the mission's production calibration where one exists?
   That would make the range span less assumption-bound, but no longer
   independent of production.
5. Scope of the backfill: every mission with slate frames, or the calibrated
   sessions first?

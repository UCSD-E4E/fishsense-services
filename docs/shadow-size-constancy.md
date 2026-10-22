# Shadow size-constancy laser calibration (SAM 3.1): design

**Status: approved by the author (2026-10-21). The thresholds (§5.3, §6) and
the switching criteria (§8) are committed in §13, before any backfill, and are
not to be adjusted after seeing results. Deliverable 2 builds against them.** This is
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
   - **The 0.6 m range span has precedent** (author's correction): the
     FishSense Lite (IMWUT) paper, §5.3, requires calibration photographs to
     span at least 0.6 m of range (`papers/fishsense-lite-imwut/Sections/results.tex:133`).
     Production enforces it as the lever-arm refusal:
     `calibration.consistency.check_observation_geometry`,
     `MIN_OBSERVATION_LEVER_M = 0.60`, the spread along the optical axis
     (max z − min z) of the camera-space laser points fitted. The shadow stage
     calls that function on its own fitted laser points. It reuses the
     definition and does not add a new one.
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
5. **Dive 279's red specks are recorded in the IMWUT analysis**
   (`imwut_2026_fishsense_lite/fish_model_analysis/IN_PLANE_DRIFT.md:346-358`,
   ce372d6, 2026-10-18; pinned by `tests/test_in_plane_ends.py`
   `test_279s_opening_detections_are_not_its_laser`).
   - In 78686 and 78688 the detector puts dots on the slate at y = 903 and
     810 px. Along the beam those are 0.49 and 0.42 m, while the slate's size
     puts it at 0.91 and 0.98 m.
   - One second later, 78690 puts the dot 445 px further along the line, on the
     diver's chest.
   - They are small red specks taken for the laser. **They lie on the laser
     line**, 3.3 px and 2.5 px from it, so a line-distance gate cannot catch
     them. Only range consistency can (§5.3 step 5; the result is in §6.1).
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
| `vp_offset_px`, `vp_offset_deg_fx`, `vp_offset_deg_f2850` | signed along-line offset of the vanishing point from the reference's, in px, and in degrees at the camera's own fx (what the paper reports) and at F=2850 (cscw's, for comparison) |
| `fit_residual` | SD of `log s − log(t − t_v) − c` over frames used |
| `se_px`, `se_deg` | bootstrap SE (500 draws, seeded per mission and source) |
| `frames_used`, `frames_rejected` | count, and `jsonb` {reason: count} |
| `size_spread` | max/min size over frames used |
| `range_min_m`, `range_max_m`, `lever_m` | the frames' laser-point ranges under the fitted calibration, and their spread along the optical axis (production's lever arm, §6) |
| `lever_m_production_offset` | the same spread with the mission's own production offset, where one exists: a sensitivity check only |
| `size_cv_pct`, `size_maxdev_pct` | slate length constancy: CV, and worst frame's deviation, of the slate's size in metres across frames (§8.4) |
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
**Source, as the author decided:** the current `laser_predictions` row per
capture. This is production's detector on the human path,
`LASER_PREDICTOR_VERSION = 2`, which already rejects dots outside
`LASER_REGION_POLYGON` (`rejected_out_of_region`).

**A coverage problem the author should see before the implementation.**
Production's human-path detector has only run where the laser stage needed it.
On the 10 pool missions it covers **128 of 1,596 canonical frames**: 62 has 7 of
34, 65 has 1 of 39, and 71 has 2 of 72. As decided, the detector source would
have too few dots to fit on most pool sessions, and on many field missions too.

The automatic track runs the **same detector kernel**
(`laser_predict._predict_from_raw`, the same region gate) on every frame, and
covers all 258 pool slate frames. The options:
- **(i)** have the shadow stage use the automatic track's dots;
- **(ii)** have the shadow stage run the human-path detector on the slate
  frames that lack a prediction, writing nothing to `laser_predictions`;
- **(iii)** keep `laser_predictions` and accept the gaps.

**Decided (author, 2026-10-21): (i), the automatic track's dots**
(`automatic_head_tail_predictions`: `laser_x`, `laser_y`, `laser_confidence`,
at the current automatic head/tail version). These are what the pool counts in
§6.1 used. The `laser_predictions` choice is withdrawn because of the coverage
gap above.

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

**Dive 279's red specks:** the test fixture is built from the IMWUT rows
(`fish_model_analysis/data/slate_sizes.csv`: 78686, 78688, 78690, 78696, and
the human closing frames 78797-78807). On those rows (§6.1):
- steps 2-4 **do not** catch the specks, which are on the line;
- step 5 **does**: it drops 78686 and 78688, and also 78696 (165 px off the
  line) and one human closing frame (78807);
- that leaves too few frames, so the dive is refused.

279 has only 6 sized frames, so it would be refused for too few frames anyway.
It therefore **does not test step 5 on its own.** The implementation adds a
synthetic fixture: a sound pool session with two dots moved along the line to
the specks' wrong ranges. Step 5 must drop exactly those two.

## 6. Refusal thresholds, proposed. **Author decides**

A source's fit is refused, with the first reason that applies, when:

| Reason | Proposed rule | Source |
|---|---|---|
| `too_few_frames` | < 8 frames used | `slate_unknown.MIN_FRAMES` |
| `too_little_size_spread` | max/min size < 1.5 | PAPER.md §4.3; `slate_unknown` |
| `lever_arm_below_0.6m` | production's own `check_observation_geometry` (`MIN_OBSERVATION_LEVER_M = 0.60`) on the fitted laser points, under the assumed 0.104 m offset | IMWUT §5.3; production's lever-arm refusal |
| `se_too_high` | bootstrap SE > 0.05° along-line (F=2850) | `TARGET`; the paper's v-to-2.5 px |
| `sam_scores_low` | median score < 0.5, or > 25% of frames lack a mask under the dot | 0.5 is production's SAM gate; the port's floor is 0.3 |
| `unconstrained` | optimum at the search bracket's edge | `size_constancy._pinned` |
| `ambiguous_line` | §5.3 step 4 (detector source) | session 77 |
| `no_reference_line` | no mission line and < 3 dots to fit one | |

The lever arm depends on the fit, because a laser point is computed from the
fitted calibration. So it is checked after the fit, as a refusal rather than a
precondition.

As a **sensitivity check only** (author's decision 4), each result also records
the lever arm computed with the mission's own production offset |O| where one
exists (`lever_m_production_offset`). It never decides a refusal.

### 6.1 Trigger counts on the 10 pool sessions (the author's request)

Script: `analysis/shadow_calibration/pool_triggers.py`, with results in
`analysis/shadow_calibration/results/`.
- Inputs: cscw-fishsense2027@8312780a, plus the automatic-track dots exported
  read-only from production (`data/pool_automatic_dots.csv`; the stand-in of
  §5.2).
- It uses cscw's own `fit_tv` and production's own lever-arm gate.
- **Sanity:** on the label dots it reproduces `size_constancy_sessions.csv`
  exactly, per session. Median |along-line offset| is 0.0232° and median axis
  angle 0.029°, with 10/10 within 0.05°.

**Refusals (§6): how many of 10 sessions each would refuse**

| Refusal | Label dots | Detector dots | Closest session to the threshold |
|---|---|---|---|
| `too_few_frames` (< 8) | 0 | **1** (77, after the gate) | fewest kept: 17 (63) |
| `too_little_size_spread` (< 1.5) | 0 | 0 | 1.73 (62) |
| `lever_arm_below_0.6m` | 0 | 0 | 1.05 m labels, 1.00 m detector (62) |
| `se_too_high` (> 0.05°) | 0 | 0 | 0.046° (62, labels) |
| `sam_scores_low` (median < 0.5, or > 25% without a mask) | 0 | 0 | median 0.54 (87); no-mask share 17% (114) |
| `unconstrained` | 0 | 0 | |
| `ambiguous_line` | n/a | **1** (77) | |

**The detector gate (§5.3): frames removed per step, over 258 slate frames**

| Step | Frames | Sessions |
|---|---|---|
| 1. out of region / no dot | 0 | |
| 2. confidence < 0.5 | 5 | 65 (3), 83 (1), 114 (1) |
| 3. > 10 px off the detector line | 23 | 77 (22), 114 (1) |
| 4. ambiguous line (dive refused) | 1 dive | 77: all 22 slate dots form their own line; the RANSAC detector line took the other (38/68 inliers) |
| 5. range-consistency trim (> 0.10) | 0 | none, on either source |

**Agreement, for context. These are not the criteria, which are judged on field
missions only:**
- Label dots: 10/10 within 0.05° along-line.
- Detector dots: 6/9 accepted within 0.05°, median 0.040°, max 0.068° (65).

**Lever arm with the production offset (sensitivity):** within 0.13 m of the
assumed-offset value on every session (94: 2.47 → 2.34 m).

**What the pool cannot set.** Nothing on the pool is near the frame, spread,
lever or SE thresholds, so the pool gives no evidence for tightening them, only
that they don't refuse sound sessions. Step 5 never fired on the pool. Its
value comes from 279 and mission 80 (`size_constancy.FRAME_RESIDUAL_MAX`
history).

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

Judged on **field missions only** (author's decision 2). The paper reports the
along-line offset at the camera's own fx (`vp_offset_deg_fx`); F=2850 is
stored beside it for comparison with cscw.

1. **Agreement:** at least **90%** of missions have along-line |offset| ≤ 0.05°.
   The 95% CI lower bound, resampling whole missions, is at least 80%.
2. **Two tiers on disagreement** (author's change):
   - **strict tier, reported:** accepted fits beyond **0.15°** are counted and
     listed. 0.15° is a stricter tier, named as such, not the length budget.
   - **budget, enforced:** no accepted fit may exceed the length-budget angle,
     **0.39°**. That's the tighter side of IMWUT §5.3's −0.53° to +0.39° of
     in-plane rotation at 2 m for a 15% length budget
     (`fishsense_imwut/calibration.py:1147-1156` `pointing_budget_deg(0.104, 2.0)`
     = (−0.526°, +0.389°)). The angle tested is the signed along-line offset,
     against the matching side of the band.
3. **Refusals carry the disagreements:** among missions where the `port` variant
   exceeds the budget angle, at least 80% are refused by `gated`.
4. **Slate length constancy** (new, the author's): under the shadow
   calibration, the slate's apparent size in metres (√area × range ÷ fx) must
   stay constant across range. On the pool's label dots (§6.1), its CV across
   frames is **0.7–1.7%**, and the worst frame deviates **1.4–6.4%** from the
   session median. Proposed:
   - **CV ≤ 3%** per mission (about twice the pool's worst);
   - **worst frame within 10%** of the median.

   Reported on every accepted mission. A mission over either is counted as a
   failure of this criterion.
5. **Coverage** (author's request for a value): at least **25%** of field
   missions with slate frames receive an accepted `gated` fit, and at least
   **60%** of those with a production calibration.
   - The only field reference is the automatic track's SIFT method, which has
     accepted 37 of 171 attempted missions (22%, production, 5:33 PM PDT
     2026-10-21).
   - The proposal asks the SAM method to beat that clearly. On calibrated
     missions it asks more, because those are where slate sessions were shot to
     be calibrated.
   - **This value has the weakest basis of any here**, since there is no pool
     evidence for it. The author should set it knowingly.

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

## 12. Decisions (author, 2026-10-21) and what is still open

Decided:
- **Dive 279:** found in the IMWUT analysis (§1.5, §5.3).
- **Thresholds:** the structure is accepted. Values are to be set on the pool
  sessions, from the counts in §6.1.
- **Detector dots:** `laser_predictions`. **Reopened** by the coverage gap in
  §5.2: options (i) to (iii).
- **Laser offset:** the fit keeps the assumed 0.104 m. The production offset's
  lever arm is recorded as a sensitivity check only.
- **Backfill order:** calibrated missions first, then every mission with slate
  frames, for coverage.
- **Metric:** the paper reports the along-line offset at the camera's own fx.

Settled (author, 2026-10-21):
1. Detector source: (i), the automatic track's dots (§5.2).
2. Threshold values: as proposed in §5.3 and §6. They are committed in §13.
3. Switching criteria: as proposed in §8, including length constancy and
   coverage. They are committed in §13.

## 13. Committed values (author, 2026-10-21, before any backfill)

**Not to be changed after seeing backfill results.** A change needs a new
dated entry here, made before the data it would be judged on is seen.

**Consistency gate (§5.3), detector dots** (the automatic track's):
- presence confidence ≥ **0.5**;
- within **10 px** of the detector line. That line is RANSAC (500 iterations,
  10 px tolerance, `default_rng(0)`) through every confident detector dot of
  the mission, then TLS through the inliers, oriented to negative y;
- `ambiguous_line` if the off-line slate dots form a line of ≥ **max(8, 0.25 ×
  kept)** dots;
- range-consistency trim: drop, one at a time, a frame whose deleted residual
  exceeds **0.10** in log size.

**Refusals (§6)**, in this order:

| Reason | Rule |
|---|---|
| `too_few_frames` | < **8** frames used |
| `too_little_size_spread` | max/min size < **1.5** |
| `lever_arm_below_0.6m` | production's `check_observation_geometry` (`MIN_OBSERVATION_LEVER_M = 0.60`) on the fitted laser points, assumed offset 0.104 m |
| `se_too_high` | bootstrap SE > **0.05°** (along-line, F = 2850 px) |
| `sam_scores_low` | median SAM score < **0.5**, or > **25%** of slate frames without a mask under the dot |
| `unconstrained` | optimum at the search bracket's edge |
| `ambiguous_line` | gate step 4 (detector dots) |

**Switching criteria (§8)**, judged on field missions only, per dot source, on
the `gated` variant, at the camera's own fx:
1. **Agreement:** ≥ **90%** of missions with |along-line offset| ≤ **0.05°**,
   with the 95% CI lower bound (whole-mission bootstrap) ≥ **80%**.
2. **Disagreement tiers:**
   - strict, reported: accepted fits beyond **0.15°**;
   - budget, enforced: **no** accepted fit beyond IMWUT §5.3's **−0.526° /
     +0.389°** at 2 m, on the matching side.
3. **Refusals carry the disagreements:** ≥ **80%** of missions whose `port`
   variant exceeds the budget are refused by `gated`.
4. **Slate length constancy:** per accepted mission, CV ≤ **3%** and worst
   frame within **10%** of the median.
5. **Coverage:** an accepted `gated` fit on ≥ **25%** of field missions with
   slate frames, and on ≥ **60%** of those with a production calibration.

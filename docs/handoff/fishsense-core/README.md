# fishsense-core: checkerboard detection drops real frames

**Resolved 2026-10-25:** fishsense-core 4.3.1 (#114) repairs a corner a laser dot dragged and tries a fixed sequence of seeds. On the same 745 frames it found a board in 640 (services' detector: 615; 4.3.0: 593), kept every grid both had found, recovered all 11 dot-dragged boards and 17 of the 18 seed-lost ones (496/132309 gives no passing grid on any seed), and still refuses the every-other-column lattice. #353 pins it.

**For:** whoever works on fishsense-core's `fishsense_core.checkerboard` (#98, released in 4.3.0).
**From:** fishsense-services, 2026-10-24. services PR #353, which adopts core's detector, is on hold until a core release fixes the two problems below.

## What we found

We ran services' own detector (`checkerboard/detection.py`, the one production runs through v0.55.0, on OpenCV 5) and core 4.3.0's `detect_checkerboard` over every checkerboard-labeled frame in production:
- 745 rendered frames, from the 17 missions that have a calibration target;
- all on the E4E board: 10 × 14 interior corners, 42.17 mm pitch;
- read-only. The renders are rectified JPEGs, the same frames the labelers saw.

| | Frames |
|---|---|
| a board found by services' detector | 615 |
| a board found by core's detector | 593 |
| found by both | 583, the same grid every time, corners identical |
| **found by services' detector only** | **32** |
| found by core's detector only | 10 |

The 32 frames only services' detector accepts fall into three groups.

**1. Correct refusals: 3 frames. Keep this behavior.**
- Mission 522, capture 134474: services' detector returns a 7 × 10 grid that takes **every other column**. Each drawn cell is two squares wide and one tall. `checker_parity` scores it 0.133 and core refuses it. This is the 2x scale error #98 describes, found in real production data.
- Mission 499, capture 132470, and mission 512, capture 133460: junk 3 × 3 grids, with a worst corner at 9–11 % of the spacing.

`checker_parity` refused no single-square lattice that services' detector found: every one scored at least 0.987.

**2. The worst-corner residual refuses good boards: 11 frames.**
These are full 10 × 14 boards. The median corner is about 0.5 % of the spacing off the homography and parity is at least 0.987, but one corner sits 5–20 % of the spacing off, against `MAX_GRID_RESIDUAL_FRACTION = 0.05`.

On the two frames inspected (509/133042 and 518/133799), every other corner is under 1 px. The one bad corner is 9–11 px off and sits under the laser dot: the dot pulls the subpixel refinement. These are exactly the frames calibration uses, the ones with the dot on the board, so this gate is biased against them.

| Mission | Capture | Worst corner / spacing |
|---|---|---|
| 496 | 132326 | 0.055 |
| 509 | 133042 | 0.201 |
| 509 | 133043 | 0.129 |
| 509 | 133045 | 0.054 |
| 509 | 133048 | 0.091 |
| 518 | 133795 | 0.066 |
| 518 | 133799 | 0.179 |
| 518 | 133802 | 0.054 |
| 518 | 133805 | 0.105 |
| 518 | 133806 | 0.061 |
| 518 | 133809 | 0.062 |

A corner latticed a whole square off, the case the worst-corner rule exists for, reads at roughly 1.0 of the spacing, far above these.

**3. Seed 0 is unlucky: 18 frames lost.**
On these frames services' detector finds a full 10 × 14 board that passes both of core's gates: worst corner at most 3.2 % of the spacing, parity at least 0.99. Core seeds OpenCV with 0, and on these frames that draw returns a small partial grid (3 × 3 up to 6 × 3) that fails the residual or the parity gate, so core refuses the frame. On 10 other frames the draw goes the other way. Seeding is right, because detection must be repeatable; one fixed draw is the problem.
- Mission 489: captures 131954, 131984, 131986, 131989.
- Mission 490: captures 132067, 132075, 132081, 132112, 132113.
- Mission 496: captures 132309, 132312, 132315.
- Missions 499, 502, 505, 512, 518 and 521: one capture each, 132439, 132606, 132758, 133470, 133807 and 134224.

## What we ask

1. **Tolerate a single displaced corner.** For example:
   - refuse when the **second-worst** corner exceeds 0.05 of the spacing, or when the worst exceeds about 0.5 of the spacing (a corner a square off reads about 1.0);
   - or drop up to one or two corners beyond 0.05 from the correspondences and refit, refusing if more are off.

   The second option is better for the pose, since the dragged corner then never reaches `solvePnP`. Either way, `checker_parity` and the "a minority latticed a square off" case must still be refused.
2. **Try more than one seed, deterministically.** For example, seeds 0..7 in order, returning the first detection that passes every gate, or the passing detection with the most corners. The same frame must still give the same answer.
3. Re-measure `MAX_GRID_RESIDUAL_FRACTION` on real frames, which its docstring already asks for. The numbers above are a start.

## How services will accept it

services pins the new core release in #353 and reruns this same comparison on the same 745 frames. To pass:
- frame 522/134474 and the two junk 3 × 3 grids are refused;
- the 11 + 18 frames above are accepted, or each one left out is explained;
- no frame services' detector accepts with a single-square lattice is refused without a reason;
- the 583 frames found by both still give the same grids.

The survey scripts are `run_detect.py` (one detector over a folder of frames), `compare_all.py` (per mission) and `why.py` (per frame: where core stops, and core's gates scored on services' corners). Ask fishsense-services for them, or for the frame IDs as a file.

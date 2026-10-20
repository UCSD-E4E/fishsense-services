# FishSense design: Deep water

One design language for the web portal (`apps/web`) and FishSense Mobile, agreed
with the owner on 2026-10-20. The canvas with every screen is
[FishSense design](https://claude.ai/artifact/NVqUWFd1LYzNKPTtiUgR9B). It is
private, so ask the owner to share it with you. The colour, type and shape values
live in [`tokens.json`](tokens.json). The mobile team's part is
[`docs/handoff/fishsense-mobile/DESIGN.md`](../handoff/fishsense-mobile/DESIGN.md).

Nothing here is built yet. The portal still uses its own Tailwind look, and these
docs describe where it is going.

## The structure: Fish and Data

The two apps share two top-level places, kept apart:

- **Fish** is what was measured: a fish (a species, or a fish model with a known
  length), its measurements (laser for Lite, LiDAR for Mobile; versions kept, the
  newest current) and the evidence for each one.
- **Data** is what was collected: **deployment** (one trip, one place, on
  consecutive days) → **mission** (one session with one camera or phone) →
  **capture** (every frame, measured or not).

Both Lite and Mobile fill both places. A phone capture that was never measured
lives in its mission under Data. That is why the portal's separate Mobile captures
page (`/captures`) is to be retired.

How a capture finds its mission:

| Mode | Lite | Mobile |
|---|---|---|
| Standard | Folder names and dates (`infer-deployments`, live since 2026-10-20) | Location and time: one phone's captures close in time and place form a mission. **Thresholds not decided** (proposed: 2 h, 1 km). |
| Enterprise (later) | An operator states the deployment and missions first (e4edm's datasets) | The phone records into the active mission |

In both modes, missions at one place on dates at most two days apart form a
deployment. Every grouping records how it was made. A stated grouping is never
replaced by an inferred one.

## Navigation

- **Web:** Today · Fish · Data · Map · Labelling, with Settings in the nav.
  Labelling is the triage and calibration tools, shown to staff. Missions and
  Deployments live under Data.
- **Phone:** Camera · Fish · Data · Settings. Photos becomes Fish, and Map becomes
  a list-or-map switch on Fish and Data, as on the web.

## Tenants

- The tenant is in the path: `/lab/fish`, `/lab/data/…`. Old `?tenant=` links
  redirect.
- A switcher beside the mark lists your tenants with your role. A member of only
  one tenant sees its name and no menu. Switching keeps you on the same kind of
  page, and the last tenant you used opens next time.
- System admins can open every tenant from the same menu. While they are in a
  tenant they don't belong to, a banner says so, and the visit is recorded.
- Superset and Temporal links are for system admins only.
- On the phone, the tenant is under Settings › Account and decides where captures
  sync.

## Home pages

- **Signed in, "Today":**
  - the tenant's numbers (lengths, missions, frames, latest activity);
  - recent fish and recent deployments;
  - "Needs you": for staff, labelling waiting, unknown ports and groupings to
    review; for members, their unsynced and unmeasured captures.
- **Signed out, the public page:**
  - what FishSense does;
  - live totals;
  - a world map of **wild fish by broad region only**: rounded counts, no sites,
    no coordinates, no photos. A region with fewer than a minimum number of fish
    shows "under N" with no count. **N is not decided** (proposed: 25). Pool
    trials of fish models count in the totals, not on the map.
  - This needs a public API endpoint that returns only those aggregates.
- No research or papers section for now (owner, 2026-10-20).

## The language

- **Teal `#00AAA5` is the identity**, as in the app today. It is a fill, never
  text on white (2.9:1): text uses `#00706C` on light and `#2CCBC5` on dark, and
  text on a teal fill is `#001A19`. Every label passes 4.5:1.
- **Dark is the field theme, light the desk theme.** The phone is always dark. The
  web follows the system setting, and both themes come from the same tokens.
- **The length is the hero.** It is set at 56 px on the web and 44 px on the phone,
  with tabular figures and a smaller, quieter unit. Its method (laser or LiDAR)
  always sits one line below it.
- **Type:** Inter for everything a person reads. JetBrains Mono only for what a
  person copies: object keys, checksums, paths, ids.
- **Shape:** cards 12 px, sheets 16, controls 10, chips round. Touch targets are
  at least 44 px.
- **Icons:** a heavy monoline with round caps and joins, taken loosely from the
  current logo. The mark on the canvas is a stand-in, because the logo may be
  replaced.
- **Colour use:** one filled teal action per screen. Facts are neutral chips, and
  only a state that needs a person is coloured (amber for a look, coral for a
  refusal, an outline for waiting).
- **Measurement overlays:** snout `#4DA3FF` (blue), fork `#FF9F43` (orange), mask
  teal at 35%, and the laser dot `#E51717`, after the logo's red. Blue and orange
  differ in lightness as well as hue, so colour-blind viewers can tell them apart.
  They replace the app's green, red and cyan.
- **Photos:** edge to edge, never cropped on the fish. A missing image is a
  labelled outline, never a blank space.

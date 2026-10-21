# FishSense design: Deep water

One design language for the web portal (`apps/web`) and FishSense Mobile, agreed
with the owner on 2026-10-20. The colour, type and shape values are in
[`tokens.json`](tokens.json). The mobile team's part is
[`docs/handoff/fishsense-mobile/DESIGN.md`](../handoff/fishsense-mobile/DESIGN.md).
The owner keeps a canvas with every screen drawn; ask them for access. Every
decision on it is written down here, so this page stands on its own.

Nothing here is built yet. The portal still uses its own Tailwind look, and these
docs describe where it is going. PLAN.md stays the design of record for the system.
This page covers how the apps look and are organised.

## The structure: Fish and Data

The two apps share two top-level places, kept apart:

- **Fish** is what was measured: a fish (a species, or a fish model with a known
  length), its measurements (laser for Lite, LiDAR for Mobile; versions kept, the
  newest current) and the evidence for each one.
- **Data** is what was collected: **deployment** → **mission** → **capture**.
  - A **deployment** is one trip: one place, on dates at most two days apart.
    Consecutive dives chain, so a week of dives every other day is one deployment.
  - A **mission** is one session with one camera or phone.
  - A **capture** is every frame, measured or not.

Both Lite and Mobile fill both places. A phone capture that was never measured
lives in its mission under Data. That is why the portal's separate Mobile captures
page (`/captures`) is to be retired.

How the groupings are made, in two steps:

| Step | Lite | Mobile |
|---|---|---|
| Capture → mission | Ingest: the folder a session was offloaded into is its mission | **Not built.** One phone's captures close in time and place form a mission. Thresholds not decided (proposed: 2 h, 1 km). |
| Mission → deployment | `infer-deployments` (live since 2026-10-20): one place, on dates at most two days apart. A mission's place and date come from its folder names. | The same rule, with the place and date taken from the captures' location and time. **Not built.** |
| Enterprise mode (later) | An operator states the deployment and its missions first (e4edm's datasets) | The phone records into the active mission |

Every grouping records how it was made. A stated grouping is never replaced by an
inferred one.

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
- Operator links are for system admins only:
  - **Temporal** always appears there.
  - **Superset** appears only while it still exists. It survives from v1 behind the
    compose stack's `superset` profile, and PLAN.md §4.9 replaces it with the
    portal's per-tenant dashboards. Show the link only when the deploy runs that
    profile.
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
  text: it is 2.9:1 on white. Text uses `tealText` (`#00706C` light, `#2CCBC5`
  dark), and text on a teal fill uses `onTeal` `#001A19` (6.3:1).
- **Dark is the field theme, light the desk theme.** The phone is always dark. The
  web follows the system setting. Both themes have the same token names in
  `tokens.json`, so a theme switch only changes values.
- **Contrast:** every text token, the state colours included, passes 4.5:1 on its
  theme's ground, surface and raised colours. The worst cases, checked 2026-10-20:
  dark `text3` 4.97, light `text3` 5.41, light `warn` 5.44, light `tealText` 5.27.
  Check again before adding a surface or a text colour.
- **The length is the hero.** It is set at 56 px on the web and 44 px on the phone,
  with tabular figures and a smaller, quieter unit. Its method (laser or LiDAR)
  always sits one line below it.
- **Lengths are in millimetres,** whole numbers with thousands grouped
  (`424 mm`, `1,235 mm`; the owner, 2026-10-21). Data is stored and served in
  metres, and distances (laser range, how far away) stay in metres.
- **Type:** Inter for everything a person reads. JetBrains Mono only for what a
  person copies: object keys, checksums, paths, ids.
- **Shape:** cards 12 px, sheets 16, controls 10, chips round. Touch targets are
  at least 44 px.
- **Icons:** a heavy monoline with round caps and joins, taken loosely from the
  current logo. The mark on the canvas is a stand-in, because the logo may be
  replaced.
- **Colour use:** one filled teal action per screen. Facts are neutral chips, and
  only a state that needs a person is coloured (`warn` for a look, `error` for a
  refusal, an outline for waiting).
- **Measurement overlays:**
  - snout `#2563EB` (blue), fork `#FFB547` (orange), mask teal at 35%, and the
    laser dot `#E51717`, after the logo's red;
  - snout and fork differ in hue **and** lightness: relative luminance 0.15 against
    0.55, a ratio of 2.9:1. So they stay apart for colour-blind viewers and in
    greyscale;
  - each point is drawn with a 3 px ring in the ground colour;
  - these replace the app's green, red and cyan.
- **Photos:** edge to edge, never cropped on the fish. A missing image is a
  labelled outline, never a blank space.

# Handoff to fishsense-mobile: one design language

**From:** fishsense-services. **For:** the FishSense Mobile (Flutter/iOS) developers.
**What this is:** the design language the owner agreed for the app and the web
portal together (2026-10-20). The full system is
[`docs/design/README.md`](../../design/README.md), the values are in
[`docs/design/tokens.json`](../../design/tokens.json), and the owner can share
the canvas with every screen drawn.

**What depends on the server.** Sections 2 to 5 are app-only and can start now.
Section 1's Data tab needs the server's missions. Since 2026-10-21 the server
groups each phone's captures into missions as they sync: a new mission after a gap
of more than 2 hours, or more than 1 km from the capture before. Missions are named
for the phone's local day, so send `time_zone` with each commit (SYNC.md). The
endpoint for the app to read its missions isn't handed over yet. Until it is, build
the Data tab grouped by day on the phone. It should switch to the server's grouping
without changing the tab's layout.

The app is already close: the black ground, teal `#00AAA5`, 12 px corners and a
bottom tab bar all stay. These are the changes.

## 1. Tabs: Camera · Fish · Data · Settings

- **Photos → Fish:** the measured fish, newest first. Each row shows the
  thumbnail, species, length, method and sync state.
- **Map** stops being a tab. It becomes a list-or-map switch at the top of Fish
  and of Data, as on the web.
- **Data is new.** It shows everything the phone collected, grouped by trip
  (deployment) and session (mission), with a "not measured" count on each
  mission. The grouping is the server's (see "What depends on the server" above),
  so the app shows what it is given. For now, group by day on the phone.

## 2. Colour

- Keep `#00AAA5` for one filled action per screen (Save, Measure). Text on it is
  `#001A19`, not black.
- Teal **text and icons** use `#2CCBC5`, so they pass 4.5:1 on the ground.
- Replace `Colors.grey[850/900]` with the tokens: surface `#141A1F`, raised
  `#1C242B`, line `#2A343C`. Replace `Colors.white` body text with `#E8EEF1`, and
  secondary text with `#9AA8B1`.
- States: amber `#F2B544` for something that needs a look (waiting to sync, for
  example), coral `#FF7A6E` for a refusal. Facts are neutral chips.

## 3. Measurement overlay (`fish_photo_overlay.dart`, `fish_result_screen.dart`)

- Snout `#2563EB`, fork `#FFB547`, mask teal at 35% with a `#00AAA5` edge.
- These replace `#4CAF50`, `#FF5252` and `#00C8FF`. Green and red are the pair
  colour-blind users confuse most. Blue and orange differ in hue and in lightness
  (luminance 0.15 against 0.55), so they stay apart in greyscale too.
- Draw each point with a 3 px ring in the ground colour, so it stands out on any
  photo.

## 4. Type and the length

- Use Inter, with tabular figures for every number.
- On the result screen, the length is 44 px semibold, with its unit at 20 px in
  `#9AA8B1`. The method and distance go one line below it, as chips
  ("LiDAR · 0.8 m away").
- The length is in whole millimetres (`424 mm`), as on the web (2026-10-21).
  The sync payload stays in metres; only what a person reads changes.
- Scale: title 28, heading 20, body 16, label 13.

## 5. Small things

- Touch targets are at least 44 px. The result screen's back button needs a
  44 px round target on a translucent ground over the photo.
- Icons: keep Material Symbols, in the rounded style at a heavy weight, to match
  the web's monoline icons.
- The app and the web say the same words: Fish, Data, deployment, mission,
  capture, "not measured", "waiting to sync".

Questions to the fishsense-services side, as for `SYNC.md`.

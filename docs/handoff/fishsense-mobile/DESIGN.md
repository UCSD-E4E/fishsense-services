# Handoff to fishsense-mobile: one design language

**From:** fishsense-services. **For:** the FishSense Mobile (Flutter/iOS) developers.
**What this is:** the design language the owner agreed for the app and the web
portal together (2026-10-20). The full system is
[`docs/design/README.md`](../../design/README.md), the values are in
[`docs/design/tokens.json`](../../design/tokens.json), and the screens are on the
[FishSense design](https://claude.ai/artifact/NVqUWFd1LYzNKPTtiUgR9B) canvas (ask
the owner for access). Nothing on the server changes for this.

The app is already close: the black ground, teal `#00AAA5`, 12 px corners and a
bottom tab bar all stay. These are the changes.

## 1. Tabs: Camera · Fish · Data · Settings

- **Photos → Fish:** the measured fish, newest first. Each row shows the
  thumbnail, species, length, method and sync state.
- **Map** stops being a tab. It becomes a list-or-map switch at the top of Fish
  and of Data, as on the web.
- **Data is new.** It shows everything the phone collected, grouped by trip
  (deployment) and session (mission), with a "not measured" count on each
  mission. The server will group phone captures by place and time, so the app
  only shows the grouping it is given. Until that exists, Data can group by day.

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

- Snout `#4DA3FF`, fork `#FF9F43`, mask teal at 35% with a `#00AAA5` edge.
- These replace `#4CAF50`, `#FF5252` and `#00C8FF`. Green and red are the pair
  colour-blind users confuse most, and blue and orange also differ in lightness.
- Draw each point with a 3 px ring in the ground colour, so it stands out on any
  photo.

## 4. Type and the length

- Use Inter, with tabular figures for every number.
- On the result screen, the length is 44 px semibold, with its unit at 20 px in
  `#9AA8B1`. The method and distance go one line below it, as chips
  ("LiDAR · 0.8 m away").
- Scale: title 28, heading 20, body 16, label 13.

## 5. Small things

- Touch targets are at least 44 px. The result screen's back button needs a
  44 px round target on a translucent ground over the photo.
- Icons: keep Material Symbols, in the rounded style at a heavy weight, to match
  the web's monoline icons.
- The app and the web say the same words: Fish, Data, deployment, mission,
  capture, "not measured", "waiting to sync".

Questions to the fishsense-services side, as for `SYNC.md`.

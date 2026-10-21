# Handoff to fishsense-mobile

**From:** fishsense-services. **For:** the FishSense Mobile (Flutter/iOS) developers.
**Last updated:** 2026-10-21.

This is the running handoff from the server to the app. It lists what we need from
the app, what changed on the server that affects the app, and what is coming. The
two contracts are in this folder:

- [`SYNC.md`](SYNC.md): sign-in, what to save with each capture, and the sync
  protocol. It is the contract the app has to meet.
- [`DESIGN.md`](DESIGN.md): the design language the app and the web portal share.

fishsense-services keeps this page current. Each server change that touches the app
adds a dated entry under "What changed on the server" and updates the table below.
Questions and corrections go to the fishsense-services side.

## What we need from the app

Statuses are what the server side can see: the app's merged PRs, and what reaches
the production API. "Not seen in production" means no capture has arrived to show
it working.

| Ask | Since | Where | Status |
|---|---|---|---|
| **Send `time_zone` with each commit**, the IANA zone the phone was in (`TimeZone.current.identifier`) | 2026-10-21 | [SYNC.md §5](SYNC.md#5-api-reference) | **Open.** Optional, but without it a mission's day is taken in UTC, so an evening in San Diego is named for the next day. A zone the server doesn't know is refused (422). |
| **Show lengths in whole millimetres** (`424 mm`) | 2026-10-21 | [DESIGN.md §4](DESIGN.md#4-type-and-the-length) | **Open.** Display only: the sync payload stays in metres (`length_m`). |
| **Adopt the shared design language**: tabs Camera · Fish · Data · Settings, colour tokens, the blue/orange overlay, type | 2026-10-20 | [DESIGN.md](DESIGN.md) | **Open.** Build the Data tab grouped by day for now (see "Coming from the server"). |
| Sign-in for sync (Authentik, code + PKCE); capturing works signed out | 2026-10-14 | [SYNC.md §1](SYNC.md#1-sign-in-required-to-sync) | Built in app 1.1.0 (mobile #69, #73). **Not seen in production:** no capture had synced as of 2026-10-21. |
| Database v8: capture UUID, capturer, install id, versions, row strides, pose, lossless frame, measurement versions | 2026-10-11 | [SYNC.md §2](SYNC.md#2-what-to-save-with-each-capture-database-v8) | Built in app 1.1.0 (mobile #68). Issues #56, #57 and #59 are still open in fishsense-mobile, so close them if #68 settled them. **Not seen in production.** |
| Sync: upload, commit, measurement versions | 2026-10-11 | [SYNC.md §4](SYNC.md#4-how-to-sync-one-capture) | Built in app 1.1.0 (mobile #72). **Not seen in production.** Please tell us when the first real sync is due, and we'll watch it arrive. |
| Pin the Fishial model to a revision and send it as `model_version` | 2026-10-11 | [SYNC.md §2](SYNC.md#2-what-to-save-with-each-capture-database-v8) | Unknown from here. |
| Fix per-row delete orphaning the JPEG (and the new PNG) | 2026-10-11 | [SYNC.md §2](SYNC.md#2-what-to-save-with-each-capture-database-v8) | **Open** (mobile #58). |

## What changed on the server

Newest first. Versions are fishsense-services releases.

- **2026-10-21 (v0.29.0): phone captures are grouped into missions.** Each capture joins a
  mission as it syncs: a new mission starts after a gap of more than 2 hours, or
  more than 1 km from the capture before. A late sync that falls between two missions
  joins the nearer one and never merges them, so mission numbers stay stable.
  Missions are named `2026.10.11.FishSense.La Jolla Cove`, for the phone's local day
  and the place the phone named (else `Mobile`). On the web a phone mission shows
  "Phone" as its camera and has no camera port. Nothing changes in the sync
  protocol; the commit gains the optional `time_zone` (above).
- **2026-10-21 (v0.28.0): lengths show in millimetres on the web.** Display only.
  The API and the sync payload stay in metres.
- **2026-10-20: one design language for the app and the web** ([DESIGN.md](DESIGN.md)).
- **2026-10-16 (v0.9.0): the server names places the app couldn't.** When
  `location.place_name` is null, the server derives a name from the coordinates on
  its own geocoder, keeps it apart from what the app sent, and shows it on the web
  as derived (mobile #72).
- **2026-10-14: syncing needs a sign-in, capturing doesn't** (the owner's decision).
- **2026-10-13 (v0.6.0): captures from before v8 sync with their unknowns** (mobile
  #68). Null versions are accepted for them, and their measurement versions have
  `origin: "unknown"`.
- **2026-10-11: the sync contract** ([SYNC.md](SYNC.md)) and per-tenant storage.

## Coming from the server

- **An endpoint for the app to read its missions,** for the Data tab. The grouping
  exists on the server (above), but the app can't read it yet. We'll add the contract
  here when it does.
- **Deployments for phone missions.** Phone missions will be linked to trips by
  location and date, as the owner decided for standard mode. An enterprise mode that
  lets a team set these explicitly comes later.
- **The web's capture list (`/captures`) is being retired.** Phone captures will be
  reached through their missions. SYNC.md's acceptance check 3 will point there once
  it's done.

## Open questions for the app

- When is the first real sync from a phone expected? Production has one registered
  phone and no synced captures (2026-10-21).
- Do mobile #56, #57 and #59 stay open for anything #68 didn't cover?

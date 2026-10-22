# Handoff to fishsense-mobile

**From:** fishsense-services. **For:** the FishSense Mobile (Flutter/iOS) developers.
**Last updated:** 2026-10-21 (the fish mask as a PNG, proposed).

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
| **Send the fish mask as a PNG** (role `mask_png`): 8-bit greyscale, lossless, the same width × height as the mask geometry, nonzero = fish | 2026-10-21 | "Coming from the server" below | **Proposed; don't send it yet.** The server doesn't accept `mask_png` until the change below lands. Keep sending `mask_u8` until then. |
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

- **2026-10-21: a mission lists all its captures, measured or not.** The v0.30.0
  entry below said a mission's page showed unmeasured captures too. It didn't: the
  Fish list, and a mission's fish, are measured fish only, so a capture synced
  without a length appeared nowhere on the web. Now Data → Missions → a mission lists
  every capture in the order taken, an unmeasured one marked "Not measured". It
  still appears in Fish only once it's measured.

- **2026-10-21 (v0.30.0): the web's capture list (`/captures`) is retired.** A phone's fish are
  in Fish, and in their missions: Data → Missions → Phone, where each mission lists
  its fish. Old `/captures` links
  redirect there, and a link to one capture goes to its fish's page, which now also
  shows what the phone recorded with it. The list endpoint
  `GET /tenants/{slug}/mobile/captures` is gone; the app never called it. Nothing
  else in the sync protocol changes. A phone mission now takes its place from the
  first of its captures that has one, the phone's own name or the one the server
  derives, so a mission named `…FishSense.Mobile` takes that place once it's known.
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

- **Accepting the fish mask as a PNG** (role `mask_png`). Today's `mask_u8` is raw bytes,
  one per pixel: about 2.8 MB at 1920 × 1440, the largest file in a capture, bigger than
  the JPEG (~0.8 MB) and the depth map (~0.2 MB). A mask is almost all 0 and 255, so a
  lossless PNG is a small fraction of that, and a browser can show it as it is, which
  the web's fish page needs to draw the mask over the photo.
  - The server can't compress the mask after upload: it keeps every file byte-for-byte
    under the SHA-256 the app computed. So the PNG is made on the phone.
  - **The contract:** an 8-bit greyscale PNG, exactly `mask.width` × `mask.height`
    pixels, nonzero on the fish, declared and committed like any other file. Its
    SHA-256 and size are those of the PNG bytes. The server will check its dimensions
    against the mask geometry, as it checks `mask_u8`'s size today.
  - `mask_u8` stays accepted, so app 1.1.0 keeps syncing. Send one or the other for a
    capture, not both.
  - We'll mark this done here, with the release, when the server accepts it. Until
    then a `mask_png` upload is refused as an unknown role.

- **An endpoint for the app to read its missions,** for the Data tab. The grouping
  exists on the server (above), but the app can't read it yet. We'll add the contract
  here when it does.
- **Deployments for phone missions.** Phone missions will be linked to trips by
  location and date, as the owner decided for standard mode. An enterprise mode that
  lets a team set these explicitly comes later.

## Open questions for the app

- When is the first real sync from a phone expected? Production has one registered
  phone and no synced captures (2026-10-21).
- Do mobile #56, #57 and #59 stay open for anything #68 didn't cover?

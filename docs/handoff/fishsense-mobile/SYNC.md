# Handoff to fishsense-mobile: sign-in and sync

**From:** fishsense-services. **For:** the FishSense Mobile (Flutter/iOS) developers.
**What's ready:** the API side is built and deployed behind the steps in `docs/handoff/krg-infra/MOBILE_SYNC.md`. It has an upload/commit/measurement protocol, per-tenant storage, and a web page where every member of a tenant sees the tenant's captures. This document is the contract the app has to meet.

**The requirement behind every rule below:** FishSense collects scientific data, so a synced capture must hold **everything the app saved**, and enough to **reproduce the measurement** on the server. Please treat "what the phone measured on" as data, not just the photo.

---

## 1. Sign-in (required to sync)

**Syncing requires a sign-in; capturing doesn't** (owner decision, 2026-10-14). The app can be used signed out, but "Sync now", automatic Wi-Fi sync and any sync status appear only to a signed-in user, and nothing syncs while signed out.

- **Identity provider:** Authentik, OpenID Connect, **authorization code + PKCE** (a public client, no secret). AppAuth (`flutter_appauth`) is the usual route.
  - issuer `https://auth.krg.ucsd.edu/application/o/fishsense-mobile/`
  - client id `fishsense-mobile`
  - scopes `openid profile email org offline_access`
  - redirect URI `com.aqua3d.fishsense:/oauthredirect`. Agree the exact string with krg-infra; theirs is a strict match.
- **Tokens:** keep the refresh token in the Keychain (`flutter_secure_storage`). The access token lasts 60 minutes; the refresh token lasts 90 days.
- **Offline:** capturing must keep working with an expired or absent access token for weeks. Only *syncing* needs a fresh token. If the refresh token has expired, ask the user to sign in again, then sync.
- **Who and which tenant:** after sign-in, call `GET /me/memberships` (§5). It returns the tenants the user belongs to, with slug, name and role.
  - A partner's account joins its org's tenant automatically the first time it calls the API.
  - If there's one tenant, use it. If there are several, let the user pick, and keep the choice.
- **Bind every capture** to a user and tenant:
  - **taken while signed in:** stamp the `sub` claim from the ID token and the tenant slug on the row at capture;
  - **taken while signed out** (or before v8): the row has no `captured_by_sub`. The signed-in account that first syncs it becomes its capturer: stamp that `sub` and tenant on the row before the upload request, and keep them.

  The server refuses a capture whose recorded `sub` isn't the signed-in caller's, so once a capture has a capturer, a different login on the same phone can't sync it.

## 2. What to save with each capture (database v8)

The current `photos` table (v7) is missing things the server needs. Add these in a v8 migration. Existing rows can be backfilled where possible, and left as unsynced where not.

| Add | Why |
|---|---|
| `client_capture_uuid` (UUID v4, generated **when the capture is taken**) | the sync's idempotency key: retries never duplicate |
| `captured_by_sub`, `tenant_slug` | bind the capture to its user and tenant (§1) |
| `install_id` (a UUID generated once per install, kept in the Keychain) | one phone = one device record on the server |
| `app_version` (e.g. `1.1.0+12`), `core_version` (e.g. `fishsense_core-v2.0.0`), `model_version` | provenance: which code and weights produced the measurement. The Fishial ONNX is fetched from Hugging Face `main`, unpinned, so **pin it** to a revision or digest, and record that digest as `model_version` |
| `depth_row_bytes` | ARKit pads depth rows. The app already copies `bytesPerRow`, but doesn't record it, so the server can't decode the buffer reliably without it |
| `confidence_row_bytes` | the same, for the confidence map |
| `camera_pose` (the ARKit `camera.transform`, 16 doubles, row-major) | not saved today; needed to place the capture in space |
| `arkit_timestamp` (the frame's `timestamp`) | not saved today |
| the **lossless frame**: the exact RGBA buffer given to `compute_length`, saved as a PNG file next to the JPEG | **this is what makes the measurement reproducible.** The JPEG is lossy (q 0.8), so segmenting it on the server won't reproduce the phone's mask. The RGBA is computed today and then dropped |
| measurement versions: a table with one row per `compute_length` or `recompute_length` result: `client_measurement_uuid`, `measured_at`, `length_m`, snout and fork as floats, `origin` (`auto` or `manual`), algorithm, its version, core and model versions | today a user's edit **overwrites** the phone's own result. Keep both: an edit is a new version |
| sync state: `synced_at` (or an outbox table) | so sync can resume where it stopped |

Also fix **per-row delete**: it orphans the JPEG today (PLAN.md §9.6), and it will orphan the new PNG too.

## 3. The files of a capture

Each file is uploaded as one object under a **role**:

| role | content | required |
|---|---|---|
| `rgb_jpeg` | the JPEG the app saves today | yes |
| `rgb_lossless` | PNG of the RGBA frame `compute_length` ran on | **yes, from v8 on.** The server accepts captures without it, so older builds still sync |
| `depth_f32` | `depth_bytes` exactly as stored: float32 little-endian metres, `depth_row_bytes × depth_height` bytes, padding included | yes |
| `confidence_u8` | `confidence_bytes` as stored, `confidence_row_bytes × confidence_height` | yes |
| `mask_u8` | `mask_bytes` as stored: uint8, `mask_width × mask_height`, nonzero = fish | when the phone found a fish |

The server checks each file's **size against the geometry** you send:

- depth must be exactly `row_bytes × height`;
- confidence must be exactly `row_bytes × height`;
- the mask must be exactly `width × height`.

If they disagree, the commit is refused.

## 4. How to sync one capture

Sync only on Wi-Fi, or when the user taps "Sync now". A session is large. For each unsynced capture:

1. **Ask for upload URLs.** Compute each file's SHA-256 (hex) and size, then call `POST /tenants/{slug}/mobile/captures/{client_capture_uuid}/uploads`.
   - The response is either `already_committed: true` (mark the capture synced and stop), or one presigned `PUT` per file.
2. **Upload each file.** `PUT` the bytes to the `url`, with **exactly** the `headers` given. They're signed, and they include `x-amz-checksum-sha256` and `Content-Length`.
   - The store refuses any other bytes (400 `InvalidDigest`).
   - The URLs last an hour. If they've expired, ask again.
3. **Commit.** `PUT /tenants/{slug}/mobile/captures/{client_capture_uuid}` with everything recorded (§5).
   - `201` means it was kept.
   - `200` means it was already kept, which is fine.
   - Either way, mark the capture synced.
4. **Later edits.** For each new measurement version, `POST /tenants/{slug}/mobile/captures/{client_capture_uuid}/measurements`.
   - `201` means added; `200` means it was already there.

**Retrying is always safe.** Every call is idempotent on the app's UUIDs.

| response | meaning | what to do |
|---|---|---|
| 401 | token expired | refresh, retry |
| 403 on commit | the capture's `captured_by_sub` isn't the signed-in user | don't retry; it isn't this user's to sync |
| 404 | not a member of that tenant (or no such capture, for `/measurements`) | stop; check the tenant |
| 409 `… has not been uploaded` / `… is not the file declared` | an upload failed or was cut short | redo steps 1–3 |
| 409 `this tenant has no storage of its own yet` | the tenant isn't set up for sync | keep the capture; try later |
| 409 `another user committed this capture` / `immutable` | a UUID collision, or the capture changed after commit | report it; it's a bug |
| 422 | the payload contradicts itself (sizes vs geometry, ranges) | a bug in the app; report the body |
| 5xx, network | outage | back off, retry later |

**Sync works from US networks only.** The storage endpoint (`s3.e4e.ucsd.edu`) accepts HTTPS from US addresses only, so a phone abroad sees its uploads refused at the network level (a connection failure, not an HTTP error). Treat it like being offline: keep the capture queued, and tell the user sync will resume once they're back in the US.

## 5. API reference

Base URL: `https://api.fishsense.e4e.ucsd.edu`, with `Authorization: Bearer <access token>`. The OpenAPI 3.1 document is `apps/web/openapi.json` in fishsense-services. Generate the Dart client from it (PLAN.md §3), so the app and the API can't drift apart.

**Upload request** (`POST …/uploads`)

```json
{ "files": [
  { "role": "rgb_jpeg",      "sha256": "9f2c…(64 hex)", "size": 812345 },
  { "role": "rgb_lossless",  "sha256": "…", "size": 5234567 },
  { "role": "depth_f32",     "sha256": "…", "size": 196608 },
  { "role": "confidence_u8", "sha256": "…", "size": 49152 },
  { "role": "mask_u8",       "sha256": "…", "size": 2764800 }
] }
```

**Upload response**

```json
{ "already_committed": false, "uploads": [
  { "role": "rgb_jpeg", "method": "PUT", "url": "https://s3.e4e.ucsd.edu/…",
    "headers": { "x-amz-checksum-sha256": "…", "Content-Length": "812345",
                 "Content-Type": "image/jpeg" } }
] }
```

**Commit** (`PUT /tenants/{slug}/mobile/captures/{client_capture_uuid}`)

```json
{
  "captured_at": "2026-10-11T17:30:00.123Z",
  "captured_by_sub": "<the ID token's sub at capture time>",
  "device": { "install_id": "<uuid>", "model": "iPhone16,1",
              "info": "Apple iPhone (iOS 17.5 - LiDAR Enabled)" },
  "app_version": "1.1.0+12",
  "core_version": "fishsense_core-v2.0.0",
  "model_version": "fishial@<pinned revision>",
  "location": { "latitude": 32.8801, "longitude": -117.234,
                "horizontal_accuracy_m": 4.5, "place_name": "La Jolla Cove" },
  "orientation": "portrait",
  "intrinsics": [1400.0, 0.0, 960.0, 0.0, 1400.0, 720.0, 0.0, 0.0, 1.0],
  "camera_pose": [ 16 doubles, row-major ],
  "depth": { "width": 256, "height": 192, "row_bytes": 1024 },
  "confidence": { "width": 256, "height": 192, "row_bytes": 256 },
  "mask": { "width": 1920, "height": 1440 },
  "files": [ the same list as the upload request ],
  "measurements": [ every version so far, oldest first ],
  "arkit_timestamp": 81234.5
}
```

Notes on the commit body:

- `location`, `orientation`, `camera_pose`, `mask` and `measurements` are optional.
- `app_version`, `core_version` and `model_version` may be null, for a capture from before the app recorded them (v7). Send null, never a made-up value.
- **Any other field you add is kept.** The server stores the whole body as the capture's manifest, so send anything the app records, even if this list doesn't name it.

**Measurement version** (in `measurements`, or `POST …/measurements`)

```json
{
  "client_measurement_uuid": "<uuid per version>",
  "measured_at": "2026-10-11T17:30:01Z",
  "length_m": 0.42,
  "snout": { "x": 412.0, "y": 803.5 },
  "fork":  { "x": 1398.0, "y": 811.0 },
  "origin": "auto",
  "algorithm": "fishsense-mobile.compute_length",
  "algorithm_version": "1.1.0",
  "core_version": "fishsense_core-v2.0.0",
  "model_version": "fishial@<pinned revision>"
}
```

Notes on measurement versions:

- Points are pixels in the RGB frame, which is the mask's frame (sensor landscape, 1920×1440). Send them as floats, not truncated.
- `length_m` must be positive. A frame the phone couldn't measure has no measurement version; it still syncs.
- `origin` is `auto` when the phone found the points (`compute_length`) and `manual` when the user placed them (`recompute_length`).
- `origin` is `unknown` for a version migrated from v7, which overwrote edits in place. Only an `unknown` version may send `algorithm` and `algorithm_version` as null; an `auto` or `manual` one must name them (422 otherwise).
- **Captures with no `captured_by_sub`** (taken signed out, or before v8) take the syncing account as their capturer (§1).
- The latest version is the current length.

**Reading back** (any member): `GET /tenants/{slug}/mobile/captures?limit=50&before=<next>` and `GET /tenants/{slug}/mobile/captures/{uuid}`. Both include presigned links to every file. They're optional for the app; the web shows them at `https://fishsense.e4e.ucsd.edu/captures`.

## Acceptance checks

1. Signed out, the app captures but offers no sync. Signed in, it captures offline with the network off for at least a day, then syncs.
2. Every capture row has `client_capture_uuid`, `captured_by_sub`, `tenant_slug`, `install_id`, the three versions, both row strides and the pose, and a lossless PNG on disk.
3. Sync on Wi-Fi: the capture appears at `/captures` on the web with the same length, and each file downloaded from there has the SHA-256 the app computed.
4. Kill the app mid-upload and sync again: there's no duplicate, and the capture syncs.
5. Edit a measurement after syncing: the web shows both versions, with the edit as current.
6. Sign in as someone else on the same phone: the first user's captures don't sync (403), and stay on the device.
7. A capture taken signed out syncs under the account that syncs it, and keeps that capturer afterwards.

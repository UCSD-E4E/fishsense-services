"""FishSense Mobile sync and display, over HTTP: real Postgres, real Garage.

PLAN.md §9.6's protocol, as built:

1. ``POST /tenants/{slug}/mobile/captures/{uuid}/uploads`` -- the app
   declares each file (role, SHA-256, size); the API answers with presigned
   PUTs into the tenant's own bucket that Garage will accept for exactly
   those bytes.
2. The app PUTs the files straight to Garage.
3. ``PUT /tenants/{slug}/mobile/captures/{uuid}`` -- commit: everything the
   app saved about the capture. The API checks every file against the store
   before keeping anything; a capture is visible only once committed.
   Idempotent: committing again changes nothing.
4. ``POST .../{uuid}/measurements`` -- a redone two-point selection, as a
   new version.

And display: ``GET /tenants/{slug}/mobile/captures`` and ``.../{uuid}``, for
any member of the tenant, with presigned links to every file.

The app signs in (a required login), records who is signed in with every
capture, and only that user may sync it.
"""

import hashlib
import json
import struct
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import AsyncIterator

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from fishsense_services_api.app import create_app
from fishsense_services_api.auth import StaticKeySource, TokenValidator
from fishsense_services_api.tenant_stores import TenantStore, TenantStores
from garage import REGION

ISSUER = "https://auth.example.test/application/o/fishsense-mobile/"
AUDIENCE = "fishsense-mobile"
KID = "k1"
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ALICE = "sub-alice"
BOB = "sub-bob"
OUTSIDER = "sub-outsider"

RGB = b"\xff\xd8 a jpeg of a fish " * 400
DEPTH = struct.pack("<f", 1.25) * (256 * 192)
CONFIDENCE = bytes([2]) * (256 * 192)
MASK = bytes([0, 1]) * (32 * 24 // 2)


def _bearer(sub: str) -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 60}
    token = jwt.encode(claims, SIGNING_KEY, algorithm="RS256", headers={"kid": KID})
    return {"Authorization": f"Bearer {token}"}


def _hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


FILES = {
    "rgb_jpeg": RGB,
    "depth_f32": DEPTH,
    "confidence_u8": CONFIDENCE,
    "mask_u8": MASK,
}


def _declared(files: dict[str, bytes]) -> list[dict]:
    return [{"role": r, "sha256": _hex(b), "size": len(b)} for r, b in files.items()]


def _commit_body(files=FILES, *, sub=ALICE, measurements=None, **overrides) -> dict:
    body = {
        "captured_at": "2026-10-11T17:30:00Z",
        "captured_by_sub": sub,
        "device": {
            "install_id": "install-1",
            "model": "iPhone16,1",
            "info": "Apple iPhone (iOS 17.5 - LiDAR Enabled)",
        },
        "app_version": "1.1.0+12",
        "core_version": "fishsense_core-v2.0.0",
        "model_version": "fishial@unpinned",
        "location": {
            "latitude": 32.8801,
            "longitude": -117.234,
            "horizontal_accuracy_m": 4.5,
            "place_name": "La Jolla Cove",
        },
        "orientation": "portrait",
        "intrinsics": [1400.0, 0.0, 960.0, 0.0, 1400.0, 720.0, 0.0, 0.0, 1.0],
        "depth": {"width": 256, "height": 192, "row_bytes": 1024},
        "confidence": {"width": 256, "height": 192, "row_bytes": 256},
        "mask": {"width": 32, "height": 24},
        "files": _declared(files),
        "measurements": measurements if measurements is not None else [_version()],
        "arkit_timestamp": 81234.5,
    }
    body.update(overrides)
    return body


def _version(*, origin="auto", length=0.42, key=None) -> dict:
    return {
        "client_measurement_uuid": str(key or uuid.uuid4()),
        "measured_at": "2026-10-11T17:30:01Z",
        "length_m": length,
        "snout": {"x": 100.0, "y": 200.0},
        "fork": {"x": 900.0, "y": 210.0},
        "origin": origin,
        "algorithm": "fishsense-mobile.compute_length",
        "algorithm_version": "1.1.0",
        "core_version": "fishsense_core-v2.0.0",
        "model_version": "fishial@unpinned",
    }


def _put(upload: dict, data: bytes) -> int:
    request = urllib.request.Request(
        upload["url"], data=data, method="PUT", headers=upload["headers"]
    )
    try:
        return urllib.request.urlopen(request).status
    except urllib.error.HTTPError as error:
        return error.code


@pytest.fixture(scope="module")
def stores(garage) -> TenantStores:
    suffix = uuid.uuid4().hex[:8]
    lab = garage.tenant_store(f"fishsense-tenant-lab-{suffix}")
    return TenantStores(
        endpoint_url=garage.endpoint_url,
        public_endpoint_url=garage.endpoint_url,
        region=REGION,
        stores={
            "lab": TenantStore(lab.bucket, lab.access_key_id, lab.secret_access_key)
        },
    )


@pytest.fixture
async def client(app_engine, stores) -> AsyncIterator[httpx.AsyncClient]:
    validator = TokenValidator(
        issuer=ISSUER,
        audiences=(AUDIENCE,),
        keys=StaticKeySource({KID: SIGNING_KEY.public_key()}),
    )
    app = create_app(engine=app_engine, validator=validator, stores=stores)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    ) as c:
        yield c


@pytest.fixture
async def tenants(seed_memberships):
    return await seed_memberships(
        {
            ALICE: {"lab": "member"},
            BOB: {"lab": "member"},
            OUTSIDER: {"partner": "member"},
        }
    )


async def _upload(client, capture: uuid.UUID, files=FILES, sub=ALICE) -> dict:
    response = await client.post(
        f"/tenants/lab/mobile/captures/{capture}/uploads",
        json={"files": _declared(files)},
        headers=_bearer(sub),
    )
    assert response.status_code == 200, response.text
    granted = response.json()
    by_role = {u["role"]: u for u in granted["uploads"]}
    for role, data in files.items():
        assert _put(by_role[role], data) == 200
    return granted


async def _sync(client, capture: uuid.UUID, **body) -> httpx.Response:
    await _upload(client, capture)
    return await client.put(
        f"/tenants/lab/mobile/captures/{capture}",
        json=_commit_body(**body),
        headers=_bearer(ALICE),
    )


async def test_a_capture_syncs_and_reads_back_byte_for_byte(tenants, client):
    capture = uuid.uuid4()

    committed = await _sync(client, capture)

    assert committed.status_code == 201, committed.text
    detail = (
        await client.get(
            f"/tenants/lab/mobile/captures/{capture}", headers=_bearer(BOB)
        )
    ).json()
    files = {f["role"]: f for f in detail["files"]}
    for role, data in FILES.items():
        assert urllib.request.urlopen(files[role]["url"]).read() == data
        assert files[role]["sha256"] == _hex(data)
    assert detail["intrinsics"][0] == 1400.0
    assert detail["depth"] == {"width": 256, "height": 192, "row_bytes": 1024}
    assert detail["current_measurement"]["length_m"] == 0.42
    assert detail["location"]["place_name"] == "La Jolla Cove"
    # Fields this server doesn't model are kept, not dropped.
    assert detail["manifest"]["arkit_timestamp"] == 81234.5


async def test_uploads_go_into_the_tenants_own_bucket(tenants, client, stores):
    capture = uuid.uuid4()
    granted = await _upload(client, capture)

    bucket = stores.for_tenant("lab").bucket
    assert all(
        f"/{bucket}/mobile/captures/{capture}/" in u["url"] for u in granted["uploads"]
    )


async def test_committing_again_is_a_no_op(tenants, client):
    capture = uuid.uuid4()
    version = _version()
    await _sync(client, capture, measurements=[version])

    again = await client.put(
        f"/tenants/lab/mobile/captures/{capture}",
        json=_commit_body(measurements=[version]),
        headers=_bearer(ALICE),
    )

    assert again.status_code == 200
    detail = (
        await client.get(
            f"/tenants/lab/mobile/captures/{capture}", headers=_bearer(ALICE)
        )
    ).json()
    assert len(detail["measurements"]) == 1


async def test_a_committed_capture_needs_no_more_uploads(tenants, client):
    capture = uuid.uuid4()
    await _sync(client, capture)

    response = await client.post(
        f"/tenants/lab/mobile/captures/{capture}/uploads",
        json={"files": _declared(FILES)},
        headers=_bearer(ALICE),
    )

    assert response.json() == {"already_committed": True, "uploads": []}


async def test_a_commit_before_its_files_arrive_is_refused(tenants, client):
    capture = uuid.uuid4()

    response = await client.put(
        f"/tenants/lab/mobile/captures/{capture}",
        json=_commit_body(),
        headers=_bearer(ALICE),
    )

    assert response.status_code == 409
    assert "rgb_jpeg" in response.json()["detail"]
    assert (
        await client.get(
            f"/tenants/lab/mobile/captures/{capture}", headers=_bearer(ALICE)
        )
    ).status_code == 404


async def test_a_commit_declaring_other_bytes_than_were_stored_is_refused(
    tenants, client
):
    capture = uuid.uuid4()
    await _upload(client, capture)
    other = {**FILES, "rgb_jpeg": RGB + b"edited"}

    response = await client.put(
        f"/tenants/lab/mobile/captures/{capture}",
        json=_commit_body(files=other),
        headers=_bearer(ALICE),
    )

    assert response.status_code == 409


async def test_only_the_signed_in_capturer_may_sync_a_capture(tenants, client):
    """The app records who was signed in when it took the capture; a
    different login on the same phone can't sync it."""
    capture = uuid.uuid4()
    await _upload(client, capture)

    response = await client.put(
        f"/tenants/lab/mobile/captures/{capture}",
        json=_commit_body(sub=ALICE),
        headers=_bearer(BOB),
    )

    assert response.status_code == 403


async def test_another_user_cannot_take_over_a_committed_uuid(tenants, client):
    capture = uuid.uuid4()
    await _sync(client, capture)

    response = await client.post(
        f"/tenants/lab/mobile/captures/{capture}/uploads",
        json={"files": _declared(FILES)},
        headers=_bearer(BOB),
    )

    assert response.status_code == 409


async def test_a_tenant_without_a_bucket_cannot_upload(tenants, client):
    """Partner data never lands in a shared bucket: no store, no upload."""
    response = await client.post(
        f"/tenants/partner/mobile/captures/{uuid.uuid4()}/uploads",
        json={"files": _declared(FILES)},
        headers=_bearer(OUTSIDER),
    )

    assert response.status_code == 409
    assert "storage" in response.json()["detail"]


async def test_a_non_member_sees_nothing(tenants, client):
    capture = uuid.uuid4()
    await _sync(client, capture)

    listed = await client.get("/tenants/lab/mobile/captures", headers=_bearer(OUTSIDER))
    one = await client.get(
        f"/tenants/lab/mobile/captures/{capture}", headers=_bearer(OUTSIDER)
    )

    assert (listed.status_code, one.status_code) == (404, 404)


async def test_the_list_shows_newest_first_with_a_thumbnail_link(tenants, client):
    first, second = uuid.uuid4(), uuid.uuid4()
    await _sync(client, first, captured_at="2026-10-11T17:00:00Z")
    files = {**FILES, "rgb_jpeg": RGB + b"2"}
    await _upload(client, second, files=files)
    await client.put(
        f"/tenants/lab/mobile/captures/{second}",
        json=_commit_body(
            files=files, captured_at="2026-10-11T18:00:00Z", measurements=[]
        ),
        headers=_bearer(ALICE),
    )

    page = (
        await client.get("/tenants/lab/mobile/captures", headers=_bearer(BOB))
    ).json()

    assert [c["client_capture_uuid"] for c in page["captures"]] == [
        str(second),
        str(first),
    ]
    assert [c["length_m"] for c in page["captures"]] == [None, 0.42]
    assert urllib.request.urlopen(page["captures"][1]["rgb_url"]).read() == RGB


async def test_the_list_pages(tenants, client):
    for hour in range(3):
        capture = uuid.uuid4()
        files = {**FILES, "rgb_jpeg": RGB + str(hour).encode()}
        await _upload(client, capture, files=files)
        await client.put(
            f"/tenants/lab/mobile/captures/{capture}",
            json=_commit_body(files=files, captured_at=f"2026-10-11T1{hour}:00:00Z"),
            headers=_bearer(ALICE),
        )

    first = (
        await client.get("/tenants/lab/mobile/captures?limit=2", headers=_bearer(ALICE))
    ).json()
    rest = (
        await client.get(
            "/tenants/lab/mobile/captures",
            params={"limit": 2, "before": first["next"]},
            headers=_bearer(ALICE),
        )
    ).json()

    assert len(first["captures"]) == 2 and first["next"]
    assert len(rest["captures"]) == 1 and rest["next"] is None


async def test_a_redone_selection_becomes_the_current_length(tenants, client):
    capture = uuid.uuid4()
    await _sync(client, capture)
    redone = _version(origin="manual", length=0.45)

    created = await client.post(
        f"/tenants/lab/mobile/captures/{capture}/measurements",
        json=redone,
        headers=_bearer(ALICE),
    )
    again = await client.post(
        f"/tenants/lab/mobile/captures/{capture}/measurements",
        json=redone,
        headers=_bearer(ALICE),
    )

    assert (created.status_code, again.status_code) == (201, 200)
    detail = (
        await client.get(
            f"/tenants/lab/mobile/captures/{capture}", headers=_bearer(ALICE)
        )
    ).json()
    assert [m["origin"] for m in detail["measurements"]] == ["auto", "manual"]
    assert detail["current_measurement"]["length_m"] == 0.45


async def test_only_the_capturer_may_add_a_version(tenants, client):
    capture = uuid.uuid4()
    await _sync(client, capture)

    response = await client.post(
        f"/tenants/lab/mobile/captures/{capture}/measurements",
        json=_version(origin="manual"),
        headers=_bearer(BOB),
    )

    assert response.status_code == 409


async def test_a_version_for_an_unknown_capture_is_404(tenants, client):
    response = await client.post(
        f"/tenants/lab/mobile/captures/{uuid.uuid4()}/measurements",
        json=_version(),
        headers=_bearer(ALICE),
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"intrinsics": [1.0] * 8}, id="intrinsics-not-3x3"),
        pytest.param({"camera_pose": [1.0] * 12}, id="pose-not-4x4"),
        pytest.param(
            {"files": {r: b for r, b in FILES.items() if r != "depth_f32"}},
            id="no-depth",
        ),
        pytest.param(
            {"depth": {"width": 256, "height": 192, "row_bytes": 2048}},
            id="depth-file-is-not-that-geometry",
        ),
        pytest.param(
            {"mask": {"width": 64, "height": 24}}, id="mask-file-is-not-that-geometry"
        ),
        pytest.param({"mask": None}, id="a-mask-file-without-its-geometry"),
        pytest.param({"location": {"latitude": 91.0, "longitude": 0.0}}, id="latitude"),
        pytest.param(
            {"measurements": [_version(length=0.0)]}, id="a-length-that-is-not-positive"
        ),
    ],
)
async def test_a_capture_that_does_not_add_up_is_refused(tenants, client, change):
    """Reproducibility needs the numbers and the files to agree; a commit
    that contradicts itself is refused rather than kept wrong."""
    capture = uuid.uuid4()
    await _upload(client, capture)

    response = await client.put(
        f"/tenants/lab/mobile/captures/{capture}",
        json=_commit_body(**change),
        headers=_bearer(ALICE),
    )

    assert response.status_code == 422


async def test_an_oversized_file_is_refused_before_any_upload(tenants, client):
    response = await client.post(
        f"/tenants/lab/mobile/captures/{uuid.uuid4()}/uploads",
        json={"files": [{"role": "rgb_jpeg", "sha256": _hex(b""), "size": 1 << 40}]},
        headers=_bearer(ALICE),
    )

    assert response.status_code == 422


async def test_an_unknown_role_is_refused(tenants, client):
    response = await client.post(
        f"/tenants/lab/mobile/captures/{uuid.uuid4()}/uploads",
        json={"files": [{"role": "holiday_photos", "sha256": _hex(b""), "size": 1}]},
        headers=_bearer(ALICE),
    )

    assert response.status_code == 422


async def test_the_lossless_frame_is_kept_when_the_app_sends_it(tenants, client):
    """The app's lossless frame (docs/handoff/fishsense-mobile) is what makes
    a server recompute exact; older builds without it still sync."""
    capture = uuid.uuid4()
    files = {**FILES, "rgb_lossless": b"\x89PNG lossless frame" * 300}
    await _upload(client, capture, files=files)

    response = await client.put(
        f"/tenants/lab/mobile/captures/{capture}",
        json=_commit_body(files=files),
        headers=_bearer(ALICE),
    )

    assert response.status_code == 201, response.text
    roles = {f["role"] for f in response.json()["files"]}
    assert "rgb_lossless" in roles


# -- code review of #63 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            {"measurements": [_version(length=float("inf"))]}, id="infinite-length"
        ),
        pytest.param(
            {"intrinsics": [float("nan")] + [1.0] * 8}, id="nan-in-intrinsics"
        ),
        pytest.param({"vendor_extra": float("nan")}, id="nan-in-an-unmodeled-field"),
    ],
)
async def test_a_number_that_is_not_finite_is_refused(tenants, client, change):
    """JSON has no NaN or Infinity; one would have failed the database write
    (a 500) or stored an infinite length. Sent as text, as a client that
    writes Python's `NaN` / `Infinity` tokens would."""
    capture = uuid.uuid4()
    await _upload(client, capture)

    response = await client.put(
        f"/tenants/lab/mobile/captures/{capture}",
        content=json.dumps(_commit_body(**change)),
        headers={**_bearer(ALICE), "Content-Type": "application/json"},
    )

    assert response.status_code == 422


async def test_the_same_photo_under_another_uuid_is_409_naming_the_first(
    tenants, client
):
    first = uuid.uuid4()
    await _sync(client, first)
    second = uuid.uuid4()
    await _upload(client, second)

    response = await client.put(
        f"/tenants/lab/mobile/captures/{second}",
        json=_commit_body(),
        headers=_bearer(ALICE),
    )

    assert response.status_code == 409
    assert str(first) in response.json()["detail"]


async def test_a_changed_recommit_is_409_not_silently_ignored(tenants, client):
    capture = uuid.uuid4()
    await _sync(client, capture)

    response = await client.put(
        f"/tenants/lab/mobile/captures/{capture}",
        json=_commit_body(
            intrinsics=[1500.0, 0.0, 960.0, 0.0, 1500.0, 720.0, 0.0, 0.0, 1.0]
        ),
        headers=_bearer(ALICE),
    )

    assert response.status_code == 409


async def test_a_resent_version_answers_with_what_is_stored(tenants, client):
    capture = uuid.uuid4()
    await _sync(client, capture)
    version = _version(origin="manual", length=0.45)
    path = f"/tenants/lab/mobile/captures/{capture}/measurements"
    await client.post(path, json=version, headers=_bearer(ALICE))

    again = await client.post(path, json=version, headers=_bearer(ALICE))
    changed = await client.post(
        path, json={**version, "length_m": 0.99}, headers=_bearer(ALICE)
    )

    assert again.status_code == 200
    assert again.json()["length_m"] == 0.45
    assert changed.status_code == 409


# -- captures from before the app recorded provenance (fishsense-mobile #68) ----------
#
# Captures migrated from the app's v7 database have no app / core / model
# version, and their measurement versions have no algorithm: v7 overwrote
# edits in place, so whether the phone or the user placed the points is
# `unknown`. The app sends null for what it doesn't know, never a made-up
# value, and the server keeps that honestly.


def _legacy_version() -> dict:
    return {
        **_version(),
        "origin": "unknown",
        "algorithm": None,
        "algorithm_version": None,
        "core_version": None,
        "model_version": None,
    }


async def test_a_capture_from_before_provenance_syncs_with_its_unknowns(
    tenants, client
):
    capture = uuid.uuid4()
    await _upload(client, capture)

    response = await client.put(
        f"/tenants/lab/mobile/captures/{capture}",
        json=_commit_body(
            app_version=None,
            core_version=None,
            model_version=None,
            measurements=[_legacy_version()],
        ),
        headers=_bearer(ALICE),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["app_version"] is None
    assert body["current_measurement"]["origin"] == "unknown"
    assert body["current_measurement"]["algorithm"] is None


@pytest.mark.parametrize("origin", ["auto", "manual"])
async def test_a_known_origin_still_names_its_algorithm(tenants, client, origin):
    """Only `unknown` may leave the algorithm out: a version the phone or
    the user produced since v8 knows what produced it."""
    capture = uuid.uuid4()
    await _upload(client, capture)
    version = {**_version(origin=origin), "algorithm": None}

    response = await client.put(
        f"/tenants/lab/mobile/captures/{capture}",
        json=_commit_body(measurements=[version]),
        headers=_bearer(ALICE),
    )

    assert response.status_code == 422

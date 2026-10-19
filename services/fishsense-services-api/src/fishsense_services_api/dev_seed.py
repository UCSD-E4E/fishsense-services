"""Local UI-testing data: a synthetic tenant to click through (``compose.ui.yml``).

PLAN.md §9.26 delivers the Lite/Mobile unification in phases, each tested in
the UI as it lands (2026-10-17). Production is the only real environment, so
the UI is tested locally, and this module gives the local stack something to
show:

* the dev users of the mock sign-in (``deploy/local/ui/oidc.json``) and their
  roles in the ``lab`` tenant: ``dev-admin`` (admin), ``dev-member`` (member),
  and ``web-service``, the landing page's service account (member);
* a measured Lite dive: one frame per :class:`Frame`, each with a laser dot,
  head/tail and species label, a laser depth and a current server
  measurement. The first :data:`DECOY_FRAMES` are labelled as what they
  are, the fish model Ginny with her known length (most of production's
  fish are models); the rest carry a species, so the UI shows both. The frames are real (Ginny, the known-length decoy, from
  ``2025.01.17.FishSense.San Diego``), but **every label and number here is
  made up**: plausible, consistent with a pinhole camera, and not data;
* FishSense Mobile captures committed the way the API commits them: files PUT
  through presigned URLs into the tenant's Garage bucket, then
  :func:`~fishsense_services_api.mobile_capture_store.commit_capture`.

It writes as the schema owner, so it refuses production
(:func:`assert_dev_database`). A host can't tell the two apart: production's
owner DSN names the host ``postgres``, exactly as the local stack's does
(deploy/incus/secrets.nix; review of #92). So the database must say it is a
dev database itself, through the server setting ``fishsense.dev_database``,
which only compose.ui.yml sets. It must also hold no migrated v1 rows, which
only production has. Both are checked before anything is written. It is
idempotent: a second run finds the seed and changes nothing.

Coordinates are in the frame's own JPEG pixels, so the UI can draw them
without knowing a sensor size.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import urllib.request
import uuid
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import hypot
from pathlib import Path

import httpx
import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.mobile_api import ROLES
from fishsense_services_api.mobile_capture_store import (
    CaptureFile,
    DeviceMeasurement,
    MobileCapture,
    commit_capture,
)
from fishsense_services_api.tenant_stores import TenantStore, TenantStores

__all__ = [
    "Frame",
    "LITE_SPECIES",
    "MOBILE_CAPTURES",
    "NotDevDatabase",
    "SeedReport",
    "assert_dev_database",
    "bootstrap_garage",
    "load_frames",
    "load_stores",
    "seed",
]

#: The server setting that marks a database as the UI harness's: compose.ui.yml
#: starts its Postgres with ``-c fishsense.dev_database=true``. Production's
#: never has it.
DEV_DATABASE_SETTING = "fishsense.dev_database"
TENANT = "lab"
USERS = {"dev-admin": "admin", "dev-member": "member", "web-service": "member"}
#: (scientific, common): the species the seed's Lite fish are labelled with.
LITE_SPECIES = ("Lachnolaimus maximus", "Hogfish")
LITE_LABEL = f"Fish, {LITE_SPECIES[1]} ({LITE_SPECIES[0]})"
MOBILE_CAPTURES = 3
#: Ginny's calipered length; the seed's lengths scatter around it.
DECOY_LENGTH_M = 0.3127
DECOY_MODEL = "Ginny"
DECOY_LABEL = f"Fish Model, {DECOY_MODEL}"
#: How many of the frames are labelled as the decoy (the rest, a species).
DECOY_FRAMES = 3
#: Every seeded dive's path starts here, which is how a rerun finds the seed.
MISSION_PREFIX = "dev-seed/"
MISSION_PATH = MISSION_PREFIX + "2025.01.17.FishSense.San Diego/ED-00/FSL-10D/Ginny"
T0 = datetime(2025, 1, 18, 1, 0, tzinfo=UTC)
#: The decoy dive's field trip, and a pool session beside it whose port
#: nobody has stated (0047), so the mission pages show both.
DEPLOYMENT = "2025.01.17.FishSense.San Diego"
UNKNOWN_PORT_MISSION = "Pool test, port unrecorded (dev seed)"
#: A pinhole camera for a 1600x1200 frame (the TG-6's ~2970 px focal length
#: at 4000 px wide, scaled).
FX = 1188.0
CAMERA_MATRIX = [[FX, 0.0, 800.0], [0.0, FX, 600.0], [0.0, 0.0, 1.0]]
#: Per-frame relative error of the seeded length, so the lengths differ.
SCATTER = (-0.021, 0.008, 0.017, -0.006, 0.029, -0.013)
#: Places for the mobile captures (lat, lon, name).
PLACES = (
    (32.8665, -117.2546, "Scripps Pier"),
    (32.8504, -117.2713, "La Jolla Cove"),
    (32.7157, -117.1611, "San Diego Bay"),
)
DEPTH_SIZE = (256, 192)
_NAMESPACE = uuid.UUID("5d6b8f0e-6a39-4c55-9b0a-3c1f1c7e2d10")


class NotDevDatabase(Exception):
    """The database isn't the UI harness's: the seed refuses to touch it."""


@dataclass(frozen=True)
class Frame:
    """One photo of the decoy, with where its fish is (JPEG pixels)."""

    name: str
    jpeg: bytes
    head: tuple[float, float]
    tail: tuple[float, float]
    dot: tuple[float, float]


@dataclass(frozen=True)
class SeedReport:
    #: False when everything was already there.
    created: bool
    lite_captures: int
    mobile_captures: int


async def assert_dev_database(conn: AsyncConnection) -> None:
    """Refuse a database that isn't marked as the harness's, or that holds
    migrated v1 data."""
    marked = await _scalar(
        conn, "SELECT current_setting(:name, true)", name=DEV_DATABASE_SETTING
    )
    if marked != "true":
        raise NotDevDatabase(
            f"refusing to seed: the database is not marked as a dev database"
            f" ({DEV_DATABASE_SETTING} is not 'true'; compose.ui.yml sets it)"
        )
    if await _scalar(
        conn,
        "SELECT (SELECT count(*) FROM missions WHERE v1_id IS NOT NULL)"
        " + (SELECT count(*) FROM captures WHERE v1_id IS NOT NULL)",
    ):
        raise NotDevDatabase("refusing to seed: the database holds migrated v1 data")


def load_frames(directory: Path) -> list[Frame]:
    """The frames in ``directory``, described by its ``frames.json``:
    ``[{"file", "head": [x, y], "tail": [x, y], "dot": [x, y]}]``. Raises
    ``OSError`` or ``ValueError`` on anything missing or malformed."""
    entries = json.loads((directory / "frames.json").read_text())
    frames = []
    for entry in entries:
        points = {}
        for key in ("head", "tail", "dot"):
            point = entry.get(key)
            if not (
                isinstance(point, list)
                and len(point) == 2
                and all(isinstance(v, (int, float)) for v in point)
            ):
                raise ValueError(
                    f"{entry.get('file')}: {key} must be [x, y], not {point!r}"
                )
            points[key] = (float(point[0]), float(point[1]))
        frames.append(
            Frame(
                name=Path(entry["file"]).stem,
                jpeg=(directory / entry["file"]).read_bytes(),
                **points,
            )
        )
    return frames


def load_stores(path: Path) -> dict[str, TenantStore]:
    """The rendered stores file, read strictly. The API's own reader
    (``TenantStores.from_file``) forgives a missing or malformed file, since it
    must never stop the API; here the file is a required input, so anything
    wrong with it raises, naming the file."""
    try:
        rendered = json.loads(Path(path).read_text())
        return {
            slug: TenantStore(
                bucket=entry["bucket"],
                access_key_id=entry["access_key_id"],
                secret_access_key=entry["secret_access_key"],
            )
            for slug, entry in rendered.items()
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError(
            f"can't read the tenant stores in {path}: {error!r}"
        ) from error


async def seed(
    owner_engine: AsyncEngine, *, frames: Sequence[Frame], stores: TenantStores
) -> SeedReport:
    """Seed the local database (as its owner) and the tenant's bucket."""
    if len(frames) < MOBILE_CAPTURES:
        # One photo, one capture: the store refuses a photo twice.
        raise ValueError(f"the seed needs at least {MOBILE_CAPTURES} frames")
    async with owner_engine.begin() as conn:
        await assert_dev_database(conn)
        tenant_id = await _tenant(conn)
        users = {
            sub: await _member(conn, tenant_id, sub, role)
            for sub, role in USERS.items()
        }
        lite = await _lite(conn, tenant_id, frames)

    mobile = 0
    for i in range(MOBILE_CAPTURES):
        frame = frames[(i + 2) % len(frames)]
        async with tenant_transaction(owner_engine, tenant_id) as conn:
            mobile += await _mobile(
                conn, tenant_id, stores, users["dev-member"], i, frame
            )
    return SeedReport(
        created=bool(lite or mobile), lite_captures=lite, mobile_captures=mobile
    )


def bootstrap_garage(
    admin_url: str, admin_token: str, stores: Mapping[str, TenantStore]
) -> None:
    """Make a fresh local Garage usable: a cluster layout, and each tenant's
    bucket under its own key (imported, so the key in the stores file is the
    one Garage has). Idempotent."""
    with httpx.Client(
        base_url=admin_url.rstrip("/") + "/v2/",
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30,
    ) as admin:

        def call(
            endpoint: str, body: dict | None = None, *, ok: frozenset = frozenset()
        ):
            response = (
                admin.post(endpoint, json=body)
                if body is not None
                else admin.get(endpoint)
            )
            if response.is_success:
                return response.json()
            try:
                code = response.json().get("code")
            except ValueError:  # not JSON: a proxy, the wrong port, not up yet
                code = None
            if code in ok:
                return None
            raise RuntimeError(
                f"garage {endpoint}: {response.status_code} {response.text[:500]}"
            )

        layout = call("GetClusterLayout")
        if not layout["roles"]:
            node = call("GetClusterStatus")["nodes"][0]["id"]
            role = {"id": node, "zone": "dc1", "capacity": 1 << 30, "tags": []}
            call("UpdateClusterLayout", {"roles": [role]})
            call("ApplyClusterLayout", {"version": layout["version"] + 1})
        for store in stores.values():
            call(
                "ImportKey",
                {
                    "accessKeyId": store.access_key_id,
                    "secretAccessKey": store.secret_access_key,
                    "name": store.bucket,
                },
                ok=frozenset({"KeyAlreadyExists"}),
            )
            call(
                "CreateBucket",
                {"globalAlias": store.bucket},
                ok=frozenset({"BucketAlreadyExists"}),
            )
            bucket = call(f"GetBucketInfo?globalAlias={store.bucket}")["id"]
            call(
                "AllowBucketKey",
                {
                    "bucketId": bucket,
                    "accessKeyId": store.access_key_id,
                    "permissions": {"read": True, "write": True, "owner": True},
                },
            )


async def _scalar(conn: AsyncConnection, sql: str, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


async def _tenant(conn: AsyncConnection) -> uuid.UUID:
    await conn.execute(
        text(
            "INSERT INTO tenants (slug, name) VALUES (:s, 'Lab') ON CONFLICT (slug) DO NOTHING"
        ),
        {"s": TENANT},
    )
    return await _scalar(conn, "SELECT id FROM tenants WHERE slug = :s", s=TENANT)


async def _member(
    conn: AsyncConnection, tenant_id: uuid.UUID, sub: str, role: str
) -> uuid.UUID:
    await conn.execute(
        text("INSERT INTO users (sub) VALUES (:sub) ON CONFLICT (sub) DO NOTHING"),
        {"sub": sub},
    )
    user_id = await _scalar(conn, "SELECT id FROM users WHERE sub = :sub", sub=sub)
    await conn.execute(
        text(
            "INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, :r) "
            "ON CONFLICT DO NOTHING"
        ),
        {"t": tenant_id, "u": user_id, "r": role},
    )
    return user_id


async def _lite(
    conn: AsyncConnection, tenant_id: uuid.UUID, frames: Sequence[Frame]
) -> int:
    """The decoy dive, measured. Returns the captures created (0: seeded)."""
    if await _scalar(
        conn,
        "SELECT count(*) FROM missions WHERE tenant_id = :t AND source_path LIKE :p",
        t=tenant_id,
        p=MISSION_PREFIX + "%",
    ):
        return 0

    async def one(sql: str, **params):
        return await _scalar(conn, sql, t=tenant_id, **params)

    device = await one(
        "INSERT INTO devices (tenant_id, kind, serial, name) "
        "VALUES (:t, 'lite', 'DEVSEED-FSL10', 'FSL-10') RETURNING id"
    )
    await one(
        "INSERT INTO camera_calibrations (tenant_id, device_id, camera_model, "
        "camera_matrix, distortion_coefficients) VALUES (:t, :d, 'pinhole', :k, "
        "'[0, 0, 0, 0, 0]') RETURNING id",
        d=device,
        k=json.dumps(CAMERA_MATRIX),
    )
    deployment = await one(
        "INSERT INTO deployments (tenant_id, name, source_path, started_on, ended_on) "
        "VALUES (:t, :n, :p, '2025-01-17', '2025-01-19') RETURNING id",
        n=DEPLOYMENT,
        p=MISSION_PREFIX + DEPLOYMENT,
    )
    mission = await one(
        "INSERT INTO missions (tenant_id, source_path, name, started_at, priority, "
        "device_id, deployment_id, purpose, port, port_source, port_note, country, "
        "region, site) VALUES (:t, :p, 'FSL-10D Ginny (dev seed)', :at, 'high', :d, "
        ":dep, 'known_length', 'corrective_optic', 'device_suffix', "
        "'device labelled FSL-10D on the mission''s path', 'USA', 'San Diego', "
        "'Pool') RETURNING id",
        p=MISSION_PATH,
        at=T0,
        d=device,
        dep=deployment,
    )
    await one(
        "INSERT INTO missions (tenant_id, source_path, name, started_at, priority, "
        "device_id) VALUES (:t, :p, :n, :at, 'low', :d) RETURNING id",
        p=MISSION_PREFIX + "lab/2024-06-02 pool test",
        n=UNKNOWN_PORT_MISSION,
        at=T0,
        d=device,
    )
    calibration = await one(
        "INSERT INTO laser_calibrations (tenant_id, mission_id, producer, outcome, "
        "laser_position, laser_axis) VALUES (:t, :d, 'slate', 'accepted', "
        "'[0.1, 0.0, 0.0]', '[0.0, 0.0, 1.0]') RETURNING id",
        d=mission,
    )
    species = await _scalar(
        conn, "SELECT ensure_species(:s, :c)", s=LITE_SPECIES[0], c=LITE_SPECIES[1]
    )
    # The decoy: a global model (and its known length, if the database has
    # none yet) and the tenant's one fish for it (0012: one per model).
    model = await _scalar(conn, "SELECT ensure_fish_model(:n)", n=DECOY_MODEL)
    await conn.execute(
        text(
            "INSERT INTO fish_model_references (name, known_length_m, notes) "
            "SELECT :n, :len, 'dev seed: Ginny, calipered' WHERE NOT EXISTS "
            "(SELECT 1 FROM fish_model_references WHERE name = :n)"
        ),
        {"n": DECOY_MODEL, "len": DECOY_LENGTH_M},
    )
    await conn.execute(
        text(
            "INSERT INTO fish (tenant_id, fish_model_id) VALUES (:t, :m) "
            "ON CONFLICT (tenant_id, fish_model_id) DO NOTHING"
        ),
        {"t": tenant_id, "m": model},
    )
    decoy = await one(
        "SELECT id FROM fish WHERE tenant_id = :t AND fish_model_id = :m", m=model
    )

    for i, frame in enumerate(frames):
        project = 900_000 + i
        capture = await one(
            "INSERT INTO captures (tenant_id, mission_id, device_id, source_path, "
            "captured_at, checksum, is_canonical) VALUES (:t, :d, :dev, :p, :at, :sum, "
            "true) RETURNING id",
            d=mission,
            dev=device,
            p=f"{MISSION_PATH}/{frame.name}.ORF",
            at=T0 + timedelta(seconds=20 * i),
            sum=hashlib.md5(frame.jpeg).hexdigest(),
        )
        laser = await one(
            "INSERT INTO laser_labels (tenant_id, capture_id, source, ls_project_id, "
            "completed, x, y) VALUES (:t, :c, 'human', :p, true, :x, :y) RETURNING id",
            c=capture,
            p=project,
            x=frame.dot[0],
            y=frame.dot[1],
        )
        head_tail = await one(
            "INSERT INTO head_tail_labels (tenant_id, capture_id, source, ls_project_id, "
            "completed, head_x, head_y, tail_x, tail_y) VALUES (:t, :c, 'human', :p, "
            "true, :hx, :hy, :tx, :ty) RETURNING id",
            c=capture,
            p=project,
            hx=frame.head[0],
            hy=frame.head[1],
            tx=frame.tail[0],
            ty=frame.tail[1],
        )
        await one(
            "INSERT INTO species_labels (tenant_id, capture_id, source, ls_project_id, "
            "completed, top_three_photos_of_group, content_of_image) VALUES (:t, :c, "
            "'human', :p, true, true, :content) RETURNING id",
            c=capture,
            p=project,
            content=DECOY_LABEL if i < DECOY_FRAMES else LITE_LABEL,
        )
        if i < DECOY_FRAMES:
            fish = decoy
        else:
            fish = await one(
                "INSERT INTO fish (tenant_id, species_id) VALUES (:t, :s) RETURNING id",
                s=species,
            )
        # A pinhole camera: length = depth * pixel span / focal length. The
        # depth is chosen so the length lands near the decoy's own.
        span = hypot(frame.tail[0] - frame.head[0], frame.tail[1] - frame.head[1])
        length = DECOY_LENGTH_M * (1 + SCATTER[i % len(SCATTER)])
        depth = length * FX / span
        await one(
            "INSERT INTO laser_depths (tenant_id, capture_id, laser_label_id, "
            "laser_calibration_id, depth_m, range_m, residual_m, core_version) "
            "VALUES (:t, :c, :l, :cal, :d, :d, 0, '4.1.0') RETURNING id",
            c=capture,
            l=laser,
            cal=calibration,
            d=depth,
        )
        await one(
            "INSERT INTO measurements (tenant_id, capture_id, fish_id, source, length_m, "
            "laser_calibration_id, laser_label_id, head_tail_label_id, algorithm, "
            "algorithm_version, core_version) VALUES (:t, :c, :f, 'server', :len, :cal, "
            ":l, :h, 'dev_seed', '1', '4.1.0') RETURNING id",
            c=capture,
            f=fish,
            len=length,
            cal=calibration,
            l=laser,
            h=head_tail,
        )
    return len(frames)


async def _mobile(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    stores: TenantStores,
    uploaded_by: uuid.UUID,
    i: int,
    frame: Frame,
) -> int:
    """One mobile capture, uploaded and committed. Returns 1 if created."""
    client_uuid = uuid.uuid5(_NAMESPACE, f"mobile-{i}")
    if await _scalar(
        conn,
        "SELECT count(*) FROM captures WHERE tenant_id = :t AND client_capture_uuid = :u",
        t=tenant_id,
        u=client_uuid,
    ):
        return 0

    width, height = DEPTH_SIZE
    length = DECOY_LENGTH_M * (1 + SCATTER[(i + 3) % len(SCATTER)])
    contents = {
        "rgb_jpeg": frame.jpeg,
        "rgb_lossless": _png_gray(64, 48),
        "depth_f32": np.full((height, width), 0.6 + 0.1 * i, dtype="<f4").tobytes(),
        "confidence_u8": np.full((height, width), 2, dtype=np.uint8).tobytes(),
    }
    objects = stores.for_tenant(TENANT)
    files = []
    for role, body in contents.items():
        digest = hashlib.sha256(body).digest()
        key = f"mobile/captures/{client_uuid}/{role}{ROLES[role].suffix}"
        upload = objects.presign_put(
            key,
            sha256=base64.b64encode(digest).decode(),
            size=len(body),
            content_type=ROLES[role].content_type,
        )
        request = urllib.request.Request(
            upload.url, data=body, method="PUT", headers=upload.headers
        )
        with urllib.request.urlopen(request):
            pass
        files.append(
            CaptureFile(
                role=role,
                bucket=objects.bucket,
                key=key,
                sha256=digest.hex(),
                size=len(body),
                content_type=ROLES[role].content_type,
            )
        )

    latitude, longitude, place = PLACES[i % len(PLACES)]
    captured_at = datetime(2026, 10, 11 + i, 18, 30, tzinfo=UTC)
    capture = MobileCapture(
        client_capture_uuid=client_uuid,
        uploaded_by=uploaded_by,
        captured_at=captured_at,
        device_install_id="dev-seed-iphone",
        device_model="iPhone16,1",
        device_info="dev seed",
        app_version="0.0.0-dev",
        core_version="fishsense_core-v4.1.0",
        model_version="fishial@dev",
        latitude=latitude,
        longitude=longitude,
        horizontal_accuracy_m=5.0,
        place_name=place,
        orientation="landscape",
        intrinsics=(FX, 0.0, 800.0, 0.0, FX, 600.0, 0.0, 0.0, 1.0),
        camera_pose=None,
        depth_width=width,
        depth_height=height,
        depth_row_bytes=width * 4,
        confidence_width=width,
        confidence_height=height,
        mask_width=None,
        mask_height=None,
        files=tuple(files),
        manifest={"dev_seed": True},
    )
    measurement = DeviceMeasurement(
        client_measurement_uuid=uuid.uuid5(_NAMESPACE, f"mobile-{i}-measurement"),
        measured_at=captured_at + timedelta(seconds=5),
        length_m=length,
        snout=frame.head,
        fork=frame.tail,
        origin="auto",
        algorithm="fishsense-mobile.compute_length",
        algorithm_version="dev",
        core_version="fishsense_core-v4.1.0",
        model_version="fishial@dev",
    )
    committed = await commit_capture(conn, tenant_id, capture, [measurement])
    return int(committed.created)


def _png_gray(width: int, height: int) -> bytes:
    """A small grey PNG: the lossless frame's stand-in (nothing decodes it)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )

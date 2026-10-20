"""The local UI harness's data: a synthetic tenant to click through.

`dev_seed` fills a local database with what the web needs to show: the dev
users and their roles, a measured Lite dive, and FishSense Mobile captures
whose files are in Garage. It exists so the UI can be tested locally, phase
by phase, without production (PLAN.md §4.9, §9.26).

It writes as the schema owner, so above all it must refuse production. A
host can't tell them apart: production's owner DSN names the host
``postgres``, as the local stack's does (deploy/incus/secrets.nix, review of
#92). So the database must say it is a dev database itself -- the server
setting ``fishsense.dev_database``, which only compose.ui.yml sets -- and must
hold no migrated (v1) data. Both are checked before anything is written,
in Garage or the database.
"""

from __future__ import annotations

import base64
import json
import hashlib
import urllib.request
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fishsense_services_api import dev_seed
from fishsense_services_contracts.renders import RENDER_VERSION, render_key
from fishsense_services_api.cli import main
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.mobile_capture_store import list_mobile_captures
from fishsense_services_api.tenant_stores import TenantStore, TenantStores
from garage import ADMIN_TOKEN, REGION, Garage


def _jpeg(n: int, width: int = 1600, height: int = 1200) -> bytes:
    """Not an image, but a JPEG's headers as far as its size: SOI, an APP0,
    and a baseline start-of-frame (SOF0) giving ``width`` x ``height``."""
    app0 = b"\xff\xe0\x00\x04" + bytes([n, n])
    sof0 = (
        b"\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


FRAMES = [
    dev_seed.Frame(
        name=f"P117{n:04d}",
        jpeg=_jpeg(n, width=1600 + n),
        head=(640.0 + n, 450.0),
        tail=(880.0 + n, 545.0),
        dot=(770.0 + n, 525.0),
    )
    for n in range(5)
]


@pytest.fixture
def stores(garage) -> TenantStores:
    lab = garage.tenant_store(f"fishsense-tenant-lab-{uuid.uuid4().hex[:8]}")
    return TenantStores(
        endpoint_url=garage.endpoint_url,
        public_endpoint_url=garage.endpoint_url,
        region=REGION,
        stores={
            "lab": TenantStore(lab.bucket, lab.access_key_id, lab.secret_access_key)
        },
    )


@pytest.fixture
async def marked(postgres, owner_engine):
    """The test database, marked as a dev database the way compose.ui.yml
    marks the harness's (for every session opened from now on)."""
    database = f'"{postgres.dbname}"'
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                f"ALTER DATABASE {database} SET {dev_seed.DEV_DATABASE_SETTING} = 'true'"
            )
        )
    yield
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(f"ALTER DATABASE {database} RESET {dev_seed.DEV_DATABASE_SETTING}")
        )


@pytest.fixture
async def dev_engine(marked, owner_url):
    """The owner, on a marked database (a fresh engine: a session reads the
    database's settings when it opens)."""
    engine = create_async_engine(owner_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def unmarked_engine(owner_engine, owner_url):
    engine = create_async_engine(owner_url)
    yield engine
    await engine.dispose()


async def _seed(engine, stores):
    return await dev_seed.seed(engine, frames=FRAMES, stores=stores)


def _bucket_exists(garage, bucket: str) -> bool:
    response = httpx.get(
        f"{garage.admin_url}/v2/GetBucketInfo",
        params={"globalAlias": bucket},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    return response.is_success


async def _scalar(owner_engine, sql: str, **params):
    async with owner_engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def _rows(engine, sql):
    async with engine.connect() as conn:
        return [dict(r._mapping) for r in await conn.execute(text(sql))]


class TestOnlyADevDatabase:
    async def test_an_unmarked_database_is_refused_before_anything_is_written(
        self, unmarked_engine, stores
    ):
        with pytest.raises(dev_seed.NotDevDatabase, match="not marked"):
            await _seed(unmarked_engine, stores)

        assert await _scalar(unmarked_engine, "SELECT count(*) FROM tenants") == 0

    async def test_a_marked_database_holding_migrated_data_is_refused(
        self, dev_engine, stores, seed_memberships
    ):
        tenants = await seed_memberships({"someone": {"lab": "member"}})
        async with dev_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO missions (tenant_id, source_path, name, started_at, v1_id) "
                    "VALUES (:t, '/real/mission', 'real', now(), 17)"
                ),
                {"t": tenants["lab"]},
            )
        with pytest.raises(dev_seed.NotDevDatabase, match="migrated"):
            await _seed(dev_engine, stores)


class TestFrames:
    def test_a_point_must_be_two_numbers(self, tmp_path):
        (tmp_path / "a.jpg").write_bytes(b"jpeg")
        entry = {"file": "a.jpg", "head": [1, 2], "tail": [3], "dot": [5, 6]}
        (tmp_path / "frames.json").write_text(json.dumps([entry]))

        with pytest.raises(ValueError, match="tail"):
            dev_seed.load_frames(tmp_path)


class TestSeed:
    async def test_each_dev_user_has_their_role(self, dev_engine, stores):
        await _seed(dev_engine, stores)

        async with dev_engine.connect() as conn:
            roles = dict(
                (
                    await conn.execute(
                        text(
                            "SELECT u.sub, m.role FROM memberships m "
                            "JOIN users u ON u.id = m.user_id "
                            "JOIN tenants t ON t.id = m.tenant_id "
                            "WHERE t.slug = 'lab'"
                        )
                    )
                ).all()
            )
        assert roles == {
            "dev-admin": "admin",
            "dev-member": "member",
            "web-service": "member",
        }

    async def test_every_lite_frame_is_an_observation_of_the_decoy_or_a_species(
        self, dev_engine, stores
    ):
        """The frames are Ginny, the decoy: the first ones are labelled as the
        model they are, with its known length; the rest carry a species, so
        the UI shows both kinds of fish."""
        await _seed(dev_engine, stores)

        async with dev_engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT o.length_m, o.fish_model, o.known_length_m, "
                        "o.scientific_name, h.head_x FROM observations o "
                        "JOIN measurements m ON m.id = o.measurement_id "
                        "JOIN head_tail_labels h ON h.id = m.head_tail_label_id "
                        "WHERE o.method = 'laser'"
                    )
                )
            ).all()
        assert len(rows) == len(FRAMES)
        models = [r for r in rows if r.fish_model is not None]
        species = [r for r in rows if r.scientific_name is not None]
        assert len(models) == dev_seed.DECOY_FRAMES
        assert {(r.fish_model, r.known_length_m) for r in models} == {
            (dev_seed.DECOY_MODEL, dev_seed.DECOY_LENGTH_M)
        }
        assert {r.scientific_name for r in species} == {dev_seed.LITE_SPECIES[0]}
        assert len(species) == len(FRAMES) - dev_seed.DECOY_FRAMES
        # The labels sit where the frame's fish is.
        assert sorted(r.head_x for r in rows) == sorted(f.head[0] for f in FRAMES)
        assert all(0.25 < r.length_m < 0.40 for r in rows)

    async def test_missions_show_a_trip_a_known_port_and_an_unknown_one(
        self, dev_engine, stores
    ):
        """0047, for the mission pages: the decoy dive is on a deployment, its
        D-labelled device makes its port known, and a pool session beside it
        has no port anyone has stated."""
        await _seed(dev_engine, stores)

        async with dev_engine.connect() as conn:
            rows = {
                r.name: r
                for r in await conn.execute(
                    text(
                        "SELECT m.name, m.purpose, m.port, m.port_source, m.site, "
                        "d.name AS deployment FROM missions m "
                        "LEFT JOIN deployments d ON d.id = m.deployment_id"
                    )
                )
            }
        decoy = rows["FSL-10D Ginny (dev seed)"]
        assert (decoy.purpose, decoy.port, decoy.port_source) == (
            "known_length",
            "corrective_optic",
            "device_suffix",
        )
        assert decoy.deployment == dev_seed.DEPLOYMENT
        pool = rows[dev_seed.UNKNOWN_PORT_MISSION]
        assert (pool.port, pool.port_source, pool.purpose) == ("unknown", None, None)

    async def test_mobile_captures_are_listed_and_their_files_are_in_garage(
        self, dev_engine, app_engine, stores
    ):
        await _seed(dev_engine, stores)

        tenant_id = await _scalar(
            dev_engine, "SELECT id FROM tenants WHERE slug = 'lab'"
        )
        async with tenant_transaction(app_engine, tenant_id) as conn:
            captures = await list_mobile_captures(conn, tenant_id, limit=50)
        assert len(captures) == dev_seed.MOBILE_CAPTURES
        assert all(c.length_m is not None for c in captures)

        objects = stores.for_tenant("lab")
        for capture in captures:
            with urllib.request.urlopen(objects.presign_get(capture.rgb_key)) as got:
                assert got.read() in {f.jpeg for f in FRAMES}

    async def test_every_lite_frame_has_a_render_in_the_tenants_bucket(
        self, dev_engine, app_engine, stores
    ):
        """Phase 4: a Lite fish's photo on /fish is its frame's render. The
        harness has no raws to render, so it stores each frame's JPEG as its
        render, as the render stage would."""
        await _seed(dev_engine, stores)

        rows = await _rows(
            dev_engine,
            "SELECT r.key, r.bucket, r.render_version, r.camera_calibration_id, "
            "r.width, r.height, c.checksum FROM capture_renders r "
            "JOIN captures c ON c.id = r.capture_id WHERE r.status = 'rendered'",
        )
        assert len(rows) == len(FRAMES)
        objects = stores.for_tenant("lab")
        by_checksum = {hashlib.md5(f.jpeg).hexdigest(): f.jpeg for f in FRAMES}
        for row in rows:
            # As the render stage writes it: its key, version and size.
            assert row["render_version"] == RENDER_VERSION
            assert row["key"] == render_key(
                RENDER_VERSION, row["camera_calibration_id"], row["checksum"]
            )
            assert row["bucket"] == objects.bucket
            assert (row["width"], row["height"]) == dev_seed.jpeg_size(
                by_checksum[row["checksum"]]
            )
            with urllib.request.urlopen(objects.presign_get(row["key"])) as got:
                assert got.read() == by_checksum[row["checksum"]]

    def test_a_jpegs_size_is_read_from_its_header(self):
        assert dev_seed.jpeg_size(_jpeg(0, width=4014, height=3016)) == (4014, 3016)
        with pytest.raises(ValueError):
            dev_seed.jpeg_size(b"not a jpeg")

    async def test_seeding_twice_changes_nothing(self, dev_engine, stores):
        await _seed(dev_engine, stores)
        counts = (
            "SELECT (SELECT count(*) FROM captures), (SELECT count(*) FROM measurements),"
            " (SELECT count(*) FROM capture_renders)"
        )
        async with dev_engine.connect() as conn:
            before = (await conn.execute(text(counts))).one()

        report = await _seed(dev_engine, stores)

        async with dev_engine.connect() as conn:
            after = (await conn.execute(text(counts))).one()
        assert after == before
        assert report.created is False


@pytest.fixture(scope="module")
def fresh_garage(tmp_path_factory):
    """A Garage as `docker compose` first starts it: no layout yet."""
    with Garage(tmp_path_factory.mktemp("fresh-garage"), layout=False) as server:
        yield server


@pytest.mark.integration  # fresh_garage starts a container of its own
class TestBootstrapGarage:
    """A fresh local Garage gets the tenant's bucket under the stores file's
    own key, so the API's presigned URLs work against it."""

    KEY = TenantStore(
        bucket="fishsense-tenant-bootstrap",
        access_key_id="GK0000000000000000000000b0",
        secret_access_key="0" * 62 + "b0",
    )

    def test_the_stores_key_can_write_and_read_its_bucket_and_a_rerun_is_harmless(
        self, fresh_garage
    ):
        garage = fresh_garage
        for _ in range(2):
            dev_seed.bootstrap_garage(garage.admin_url, ADMIN_TOKEN, {"lab": self.KEY})

        objects = TenantStores(
            endpoint_url=garage.endpoint_url,
            public_endpoint_url=garage.endpoint_url,
            region=REGION,
            stores={"lab": self.KEY},
        ).for_tenant("lab")
        body = b"bootstrapped"
        upload = objects.presign_put(
            "probe",
            sha256=base64.b64encode(hashlib.sha256(body).digest()).decode(),
            size=len(body),
            content_type="text/plain",
        )
        put = urllib.request.Request(
            upload.url, data=body, method="PUT", headers=upload.headers
        )
        with urllib.request.urlopen(put):
            pass
        with urllib.request.urlopen(objects.presign_get("probe")) as got:
            assert got.read() == body

    def test_an_error_that_is_not_json_is_reported_as_it_came(self, garage):
        # Garage's S3 port answers the admin path with XML, not JSON.
        with pytest.raises(
            RuntimeError, match="GetClusterLayout.*403|GetClusterLayout.*40"
        ):
            dev_seed.bootstrap_garage(garage.endpoint_url, ADMIN_TOKEN, {})


class TestCommand:
    """``fishsense-services-api dev-seed``: compose.ui.yml's one-shot seeder.
    Everything it is given is checked before Garage is touched."""

    @pytest.fixture
    def frames_dir(self, tmp_path):
        entries = []
        for frame in FRAMES[:3]:
            (tmp_path / f"{frame.name}.jpg").write_bytes(frame.jpeg)
            entries.append(
                {
                    "file": f"{frame.name}.jpg",
                    "head": list(frame.head),
                    "tail": list(frame.tail),
                    "dot": list(frame.dot),
                }
            )
        (tmp_path / "frames.json").write_text(json.dumps(entries))
        return tmp_path

    @pytest.fixture
    def bucket(self):
        return f"fishsense-tenant-command-{uuid.uuid4().hex[:8]}"

    @pytest.fixture
    def environment(self, monkeypatch, tmp_path, garage, owner_url, bucket):
        suffix = uuid.uuid4().hex[:6]
        stores_file = tmp_path / "tenant-stores.json"
        stores_file.write_text(
            json.dumps(
                {
                    "lab": {
                        "bucket": bucket,
                        "access_key_id": "GK" + "0" * 18 + suffix,
                        "secret_access_key": "0" * 58 + suffix,
                    }
                }
            )
        )
        for name, value in {
            "MIGRATION_DATABASE_URL": owner_url,
            "TENANT_STORES_FILE": str(stores_file),
            "OBJECT_STORE_ENDPOINT_URL": garage.endpoint_url,
            "OBJECT_STORE_PUBLIC_URL": garage.endpoint_url,
            "DEV_GARAGE_ADMIN_URL": garage.admin_url,
            "DEV_GARAGE_ADMIN_TOKEN": ADMIN_TOKEN,
        }.items():
            monkeypatch.setenv(f"FISHSENSE_{name}", value)

    async def test_it_refuses_an_unmarked_database_before_touching_garage(
        self, environment, frames_dir, owner_engine, garage, bucket, capsys
    ):
        assert await main(["dev-seed", "--frames", str(frames_dir)]) == 1

        assert "not marked" in capsys.readouterr().err
        assert not _bucket_exists(garage, bucket)

    async def test_missing_frames_are_refused_before_touching_garage(
        self, environment, marked, tmp_path, garage, bucket, capsys
    ):
        missing = tmp_path / "no-frames-here"

        assert await main(["dev-seed", "--frames", str(missing)]) == 2

        assert "frames" in capsys.readouterr().err
        assert not _bucket_exists(garage, bucket)

    async def test_a_missing_stores_file_is_named(
        self, environment, marked, frames_dir, tmp_path, monkeypatch, capsys
    ):
        missing = tmp_path / "not-rendered.json"
        monkeypatch.setenv("FISHSENSE_TENANT_STORES_FILE", str(missing))

        assert await main(["dev-seed", "--frames", str(frames_dir)]) == 2

        assert str(missing) in capsys.readouterr().err

    async def test_it_seeds_garage_and_the_database(
        self, environment, marked, frames_dir, owner_engine, garage, bucket, capsys
    ):
        assert await main(["dev-seed", "--frames", str(frames_dir)]) == 0

        assert await _scalar(owner_engine, "SELECT count(*) FROM mobile_captures") == (
            dev_seed.MOBILE_CAPTURES
        )
        assert _bucket_exists(garage, bucket)
        assert "3 Lite" in capsys.readouterr().out

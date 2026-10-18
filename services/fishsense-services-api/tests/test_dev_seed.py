"""The local UI harness's data: a synthetic tenant to click through.

`dev_seed` fills a local database with what the web needs to show: the dev
users and their roles, a measured Lite dive, and FishSense Mobile captures
whose files are in Garage. It exists so the UI can be tested locally, phase
by phase, without production (PLAN.md §4.9, §9.26).

It writes as the schema owner, so above all it must refuse anything that
isn't a local database: a host that isn't local, or a database that already
holds migrated (v1) data, the mark of production.
"""

from __future__ import annotations

import base64
import json
import hashlib
import urllib.request
import uuid

import pytest
from sqlalchemy import text

from fishsense_services_api import dev_seed
from fishsense_services_api.cli import main
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.mobile_capture_store import list_mobile_captures
from fishsense_services_api.tenant_stores import TenantStore, TenantStores
from garage import ADMIN_TOKEN, REGION, Garage

FRAMES = [
    dev_seed.Frame(
        name=f"P117{n:04d}",
        jpeg=b"\xff\xd8\xff\xe0 not really a jpeg " + bytes([n]),
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


async def _seed(owner_engine, stores):
    return await dev_seed.seed(owner_engine, frames=FRAMES, stores=stores)


async def _scalar(owner_engine, sql: str, **params):
    async with owner_engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


class TestOnlyLocal:
    @pytest.mark.parametrize(
        "url",
        [
            "postgresql+psycopg://o:p@localhost/fishsense",
            "postgresql+psycopg://o:p@127.0.0.1:5432/fishsense",
            "postgresql+psycopg://o:p@postgres/fishsense",
        ],
    )
    def test_a_local_host_is_allowed(self, url):
        dev_seed.assert_local_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql+psycopg://o:p@fishsense-postgres-1/fishsense_services",
            "postgresql+psycopg://o:p@db.krg.ucsd.edu/fishsense",
            "postgresql+psycopg://o:p@10.0.0.5/fishsense",
        ],
    )
    def test_any_other_host_is_refused(self, url):
        with pytest.raises(dev_seed.NotLocal):
            dev_seed.assert_local_url(url)

    async def test_a_database_holding_migrated_data_is_refused(
        self, owner_engine, stores, seed_memberships
    ):
        tenants = await seed_memberships({"someone": {"lab": "member"}})
        async with owner_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO dives (tenant_id, source_path, name, dived_at, v1_id) "
                    "VALUES (:t, '/real/dive', 'real', now(), 17)"
                ),
                {"t": tenants["lab"]},
            )
        with pytest.raises(dev_seed.NotLocal):
            await _seed(owner_engine, stores)


class TestSeed:
    async def test_each_dev_user_has_their_role(self, owner_engine, stores):
        await _seed(owner_engine, stores)

        async with owner_engine.connect() as conn:
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

    async def test_every_lite_frame_has_a_current_measurement_with_a_species(
        self, owner_engine, stores
    ):
        await _seed(owner_engine, stores)

        async with owner_engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT m.length_m, s.scientific_name, h.head_x "
                        "FROM current_measurements m "
                        "JOIN captures c ON c.id = m.capture_id "
                        "JOIN dives d ON d.id = c.dive_id "
                        "JOIN fish f ON f.id = m.fish_id "
                        "JOIN species s ON s.id = f.species_id "
                        "JOIN head_tail_labels h ON h.id = m.head_tail_label_id "
                        "WHERE m.source = 'server'"
                    )
                )
            ).all()
        assert len(rows) == len(FRAMES)
        assert {r.scientific_name for r in rows} == {dev_seed.LITE_SPECIES[0]}
        # The labels sit where the frame's fish is.
        assert sorted(r.head_x for r in rows) == sorted(f.head[0] for f in FRAMES)
        assert all(0.25 < r.length_m < 0.40 for r in rows)

    async def test_mobile_captures_are_listed_and_their_files_are_in_garage(
        self, owner_engine, app_engine, stores
    ):
        await _seed(owner_engine, stores)

        tenant_id = await _scalar(
            owner_engine, "SELECT id FROM tenants WHERE slug = 'lab'"
        )
        async with tenant_transaction(app_engine, tenant_id) as conn:
            captures = await list_mobile_captures(conn, tenant_id, limit=50)
        assert len(captures) == dev_seed.MOBILE_CAPTURES
        assert all(c.length_m is not None for c in captures)

        objects = stores.for_tenant("lab")
        for capture in captures:
            with urllib.request.urlopen(objects.presign_get(capture.rgb_key)) as got:
                assert got.read() in {f.jpeg for f in FRAMES}

    async def test_seeding_twice_changes_nothing(self, owner_engine, stores):
        await _seed(owner_engine, stores)
        counts = "SELECT (SELECT count(*) FROM captures), (SELECT count(*) FROM measurements)"
        async with owner_engine.connect() as conn:
            before = (await conn.execute(text(counts))).one()

        report = await _seed(owner_engine, stores)

        async with owner_engine.connect() as conn:
            after = (await conn.execute(text(counts))).one()
        assert after == before
        assert report.created is False


@pytest.fixture(scope="module")
def fresh_garage(tmp_path_factory):
    """A Garage as `docker compose` first starts it: no layout yet."""
    with Garage(tmp_path_factory.mktemp("fresh-garage"), layout=False) as server:
        yield server


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


class TestCommand:
    """``fishsense-services-api dev-seed``: compose.ui.yml's one-shot seeder."""

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
    def environment(self, monkeypatch, tmp_path, garage, owner_url):
        store = TenantStore(
            bucket="fishsense-tenant-command",
            access_key_id="GK0000000000000000000000c0",
            secret_access_key="0" * 62 + "c0",
        )
        stores_file = tmp_path / "tenant-stores.json"
        stores_file.write_text(
            json.dumps(
                {
                    "lab": {
                        "bucket": store.bucket,
                        "access_key_id": store.access_key_id,
                        "secret_access_key": store.secret_access_key,
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

    async def test_it_refuses_a_database_that_is_not_local(
        self, environment, frames_dir, monkeypatch, capsys
    ):
        monkeypatch.setenv(
            "FISHSENSE_MIGRATION_DATABASE_URL",
            "postgresql+psycopg://o:p@fishsense-postgres-1/fishsense_services",
        )

        assert await main(["dev-seed", "--frames", str(frames_dir)]) == 1
        assert "not a local database" in capsys.readouterr().err

    async def test_it_seeds_garage_and_the_database(
        self, environment, frames_dir, owner_engine, capsys
    ):
        assert await main(["dev-seed", "--frames", str(frames_dir)]) == 0

        assert await _scalar(owner_engine, "SELECT count(*) FROM mobile_captures") == (
            dev_seed.MOBILE_CAPTURES
        )
        assert "3 Lite" in capsys.readouterr().out

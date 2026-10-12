"""Each tenant's objects live in its own bucket, under its own key.

PLAN.md §9.11: Garage grants keys per bucket, so a tenant prefix isolates
nothing; bucket-per-tenant was due before the first partner upload, and mobile
sync is that upload. The API presigns a client's uploads and checks what
arrived, with the tenant's own key -- and a tenant with no store configured
gets none, rather than falling back to a shared bucket.

Upload integrity (§9.19, decided here): the client declares each file's
SHA-256 and size; the presigned PUT signs both, so Garage itself refuses any
other bytes, and commit reads them back with HEAD.
"""

import base64
import hashlib
import json
import urllib.error
import urllib.request

import pytest

from fishsense_services_api.tenant_stores import (
    NoTenantStore,
    StoredObject,
    TenantStore,
    TenantStores,
)
from garage import REGION

BODY = b"a fish, in bytes " * 512


def _sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def _put(upload, data: bytes) -> int:
    request = urllib.request.Request(
        upload.url, data=data, method="PUT", headers=upload.headers
    )
    try:
        return urllib.request.urlopen(request).status
    except urllib.error.HTTPError as error:
        return error.code


def _get(url: str) -> bytes:
    return urllib.request.urlopen(url).read()


@pytest.fixture(scope="module")
def stores(garage) -> TenantStores:
    lab = garage.tenant_store("fishsense-tenant-lab")
    partner = garage.tenant_store("fishsense-tenant-partner")
    return TenantStores(
        endpoint_url=garage.endpoint_url,
        public_endpoint_url=garage.endpoint_url,
        region=REGION,
        stores={
            "lab": TenantStore(lab.bucket, lab.access_key_id, lab.secret_access_key),
            "partner": TenantStore(
                partner.bucket, partner.access_key_id, partner.secret_access_key
            ),
        },
    )


def test_a_presigned_upload_of_the_declared_bytes_is_stored(stores):
    lab = stores.for_tenant("lab")
    upload = lab.presign_put(
        "mobile/a/rgb.jpg",
        sha256=_sha256(BODY),
        size=len(BODY),
        content_type="image/jpeg",
    )

    assert _put(upload, BODY) == 200
    assert lab.head("mobile/a/rgb.jpg") == StoredObject(
        size=len(BODY), sha256=_sha256(BODY)
    )


def test_bytes_other_than_the_declared_ones_are_refused_by_the_store(stores):
    lab = stores.for_tenant("lab")
    upload = lab.presign_put(
        "mobile/b/rgb.jpg",
        sha256=_sha256(BODY),
        size=len(BODY),
        content_type="image/jpeg",
    )
    tampered = BODY[:-1] + b"!"

    assert _put(upload, tampered) == 400
    assert lab.head("mobile/b/rgb.jpg") is None


def test_an_upload_of_another_size_is_refused(stores):
    lab = stores.for_tenant("lab")
    upload = lab.presign_put(
        "mobile/c/rgb.jpg",
        sha256=_sha256(BODY),
        size=len(BODY),
        content_type="image/jpeg",
    )
    upload.headers["Content-Length"] = str(len(BODY) + 1)

    assert _put(upload, BODY + b"x") != 200


def test_an_upload_goes_to_the_tenants_own_bucket(stores, garage):
    partner = stores.for_tenant("partner")
    upload = partner.presign_put(
        "mobile/d/rgb.jpg",
        sha256=_sha256(BODY),
        size=len(BODY),
        content_type="image/jpeg",
    )
    _put(upload, BODY)

    assert "/fishsense-tenant-partner/" in upload.url
    assert stores.for_tenant("lab").head("mobile/d/rgb.jpg") is None
    assert partner.head("mobile/d/rgb.jpg") is not None


def test_a_presigned_read_returns_the_object(stores):
    lab = stores.for_tenant("lab")
    upload = lab.presign_put(
        "mobile/e/rgb.jpg",
        sha256=_sha256(BODY),
        size=len(BODY),
        content_type="image/jpeg",
    )
    _put(upload, BODY)

    assert _get(lab.presign_get("mobile/e/rgb.jpg", expires_seconds=60)) == BODY


def test_urls_are_signed_for_the_public_endpoint(garage):
    """Phones and browsers reach Garage at its public address; the API's own
    checks use the internal one. A signature covers the host, so presigning
    must use the address the client will call."""
    stores = TenantStores(
        endpoint_url=garage.endpoint_url,
        public_endpoint_url="https://s3.example.test",
        region=REGION,
        stores={"lab": TenantStore("b", "k", "s")},
    )

    lab = stores.for_tenant("lab")
    upload = lab.presign_put(
        "x", sha256=_sha256(b""), size=0, content_type="image/jpeg"
    )

    assert upload.url.startswith("https://s3.example.test/b/x?")
    assert lab.presign_get("x", expires_seconds=60).startswith(
        "https://s3.example.test/"
    )


def test_a_tenant_with_no_store_gets_none():
    stores = TenantStores(
        endpoint_url="http://garage.test",
        public_endpoint_url="http://garage.test",
        region=REGION,
        stores={},
    )

    with pytest.raises(NoTenantStore):
        stores.for_tenant("partner")


def test_stores_load_from_the_rendered_file(tmp_path):
    rendered = tmp_path / "tenant-stores.json"
    rendered.write_text(
        json.dumps(
            {
                "conservation-angler": {
                    "bucket": "fishsense-tenant-conservation-angler",
                    "access_key_id": "GKca",
                    "secret_access_key": "never-printed",
                }
            }
        )
    )

    stores = TenantStores.from_file(
        rendered,
        endpoint_url="http://garage.test",
        public_endpoint_url="https://s3.example.test",
        region=REGION,
    )

    assert stores.tenants == {"conservation-angler"}
    assert "never-printed" not in repr(stores)
    assert "never-printed" not in repr(stores.for_tenant("conservation-angler"))


def test_an_empty_rendered_file_is_no_stores(tmp_path):
    """Until krg-infra seeds the stores, the soft render is empty: the API
    starts, and no tenant can sync."""
    rendered = tmp_path / "stores.json"
    rendered.write_text("\n")

    stores = TenantStores.from_file(
        rendered,
        endpoint_url="http://garage.test",
        public_endpoint_url="http://garage.test",
        region=REGION,
    )

    assert stores.tenants == set()


# The stores are optional: whatever is wrong with them, only mobile sync may
# stop -- never the API (code review of #63).


def _load(path):
    return TenantStores.from_file(
        path,
        endpoint_url="http://garage.test",
        public_endpoint_url="http://garage.test",
        region=REGION,
    )


def test_a_missing_file_is_no_stores(tmp_path, caplog):
    """The container can start before vault-agent first renders the file."""
    with caplog.at_level("WARNING"):
        stores = _load(tmp_path / "not-rendered-yet.json")

    assert stores.tenants == set()
    assert any("not-rendered-yet.json" in r.getMessage() for r in caplog.records)


def test_a_malformed_file_is_no_stores_and_says_so(tmp_path, caplog):
    rendered = tmp_path / "stores.json"
    rendered.write_text('{"lab": {"bucket": ')

    with caplog.at_level("ERROR"):
        stores = _load(rendered)

    assert stores.tenants == set()
    assert any("stores.json" in r.getMessage() for r in caplog.records)


def test_an_incomplete_entry_is_skipped_and_the_rest_kept(tmp_path, caplog):
    rendered = tmp_path / "stores.json"
    rendered.write_text(
        json.dumps(
            {
                "lab": {"bucket": "b", "access_key_id": "k", "secret_access_key": "s"},
                "partner": {"bucket": "p", "access_key_id": "k"},
            }
        )
    )

    with caplog.at_level("ERROR"):
        stores = _load(rendered)

    assert stores.tenants == {"lab"}
    assert any("partner" in r.getMessage() for r in caplog.records)

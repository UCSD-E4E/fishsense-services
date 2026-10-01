"""Where every object lives: the key layout the orchestrator issues.

Ported from fishsense-lite@77e8f8e5 libs/fishsense-shared/tests/
test_object_store.py (the key contract: raw, slate PDF and JPEG keys, the
per-stage folders, prefix slash-stripping). v1's rules, kept:

* raw frames are keyed by checksum (``raw/{checksum}.ORF``), so a dive in
  several cohorts stages once;
* a processed JPEG is ``{folder}/{checksum}.JPG`` in the labels bucket, with
  one folder per stage -- the lattice renders in their own, or they would
  overwrite the laser JPEG a project is already serving;
* scratch keys carry a content-type segment (``raw/``, ``slate_pdf/``) because
  they share a bucket; JPEG keys don't.

v2 changes, each pinned here (PLAN.md §9.11, decided 2026-09-27):

* **every new key is under ``tenants/{tenant_id}/``**, with the rest of the key
  independent of the tenant, so moving to bucket-per-tenant later swaps that
  prefix for a bucket and changes nothing else;
* **JPEGs v1 already wrote stay readable where they are**
  (``{labels_prefix}/{folder}/{checksum}.JPG``): Label Studio tasks and label
  ``image_url``s point at them. A frame migrated from v1 resolves to its new key
  first, then v1's; a frame v2 ingested never resolves to a v1 key, so no
  tenant can reach an object by a checksum that is not its own;
* a slate PDF is keyed by the template's uuid, as v2's templates are.
"""

import uuid

import pytest

from fishsense_services_contracts.object_store import ObjectRef, ObjectStoreConnection
from fishsense_services_orchestrator.object_store import layout as sut

TENANT = uuid.UUID("7b0c7d4e-3f58-4d8e-9a3c-0c5f1f7f2d11")
OTHER = uuid.UUID("0d4b0c43-6a3e-4d1f-8f55-0e7d1f0a9b22")
SLATE = uuid.UUID("5b0f1f5e-9a3c-4d8e-8e1d-1a2b3c4d5e6f")
CHECKSUM = "0123456789abcdef0123456789abcdef"


def _settings(**overrides) -> ObjectStoreConnection:
    values = {
        "endpoint_url": "https://s3.e4e.example",
        "region": "garage",
        "access_key_id": "k",
        "secret_access_key": "s",
        "bucket": "fishsense-lite",
        "labels_bucket": "labels-fishsense-lite",
        "legacy_labels_prefix": "fishsense-lite",
    }
    values.update(overrides)
    return ObjectStoreConnection(**values)


@pytest.fixture(name="layout")
def layout_fixture() -> sut.ObjectLayout:
    return sut.ObjectLayout(_settings())


# -- the key contract ----------------------------------------------------------


def test_a_raw_frame_is_staged_under_its_tenant_by_checksum(layout):
    assert layout.raw(TENANT, CHECKSUM) == ObjectRef(
        bucket="fishsense-lite", key=f"tenants/{TENANT}/raw/{CHECKSUM}.ORF"
    )


def test_a_slate_pdf_is_staged_under_its_tenant_by_template_uuid(layout):
    assert layout.slate_pdf(TENANT, SLATE) == ObjectRef(
        bucket="fishsense-lite", key=f"tenants/{TENANT}/slate_pdf/{SLATE}.pdf"
    )


@pytest.mark.parametrize(
    "folder",
    [
        sut.LASER_JPEG_FOLDER,
        sut.SPECIES_JPEG_FOLDER,
        sut.HEADTAIL_JPEG_FOLDER,
        sut.SLATE_JPEG_FOLDER,
        sut.CHECKERBOARD_LATTICE_JPEG_FOLDER,
    ],
)
def test_a_new_processed_jpeg_is_in_the_labels_bucket_under_its_tenant(layout, folder):
    assert layout.processed_jpeg(TENANT, folder, CHECKSUM) == ObjectRef(
        bucket="labels-fishsense-lite", key=f"tenants/{TENANT}/{folder}/{CHECKSUM}.JPG"
    )


def test_the_stage_folders_are_v1s():
    """Populate embeds them in task URIs a labeler follows; spelled once."""
    assert sut.JPEG_FOLDERS == (
        "preprocess_jpeg",
        "preprocess_groups_jpeg",
        "preprocess_headtail_jpeg",
        "preprocess_slate_images_jpeg",
        "checkerboard_lattice_jpeg",
    )


def test_an_unknown_folder_is_refused(layout):
    """A misspelt folder would write JPEGs no Label Studio task points at."""
    with pytest.raises(ValueError, match="folder"):
        layout.processed_jpeg(TENANT, "preprocess_jpg", CHECKSUM)


@pytest.mark.parametrize("checksum", ["", "../x", "a/b"])
def test_a_checksum_that_is_not_one_is_refused(layout, checksum):
    """A checksum is the last key segment; one carrying a slash would put an
    object somewhere no reader looks."""
    with pytest.raises(ValueError, match="checksum"):
        layout.raw(TENANT, checksum)


def test_single_bucket_layouts_keep_working():
    """v1's default: no labels bucket set means JPEGs share the scratch bucket."""
    layout = sut.ObjectLayout(_settings(labels_bucket=None))

    assert layout.processed_jpeg(TENANT, "preprocess_jpeg", CHECKSUM).bucket == (
        "fishsense-lite"
    )


# -- tenancy: a prefix that swaps for a bucket -----------------------------------


def _every_key(layout, tenant):
    return [
        layout.raw(tenant, CHECKSUM),
        layout.slate_pdf(tenant, SLATE),
        *(layout.processed_jpeg(tenant, f, CHECKSUM) for f in sut.JPEG_FOLDERS),
    ]


def test_every_key_the_layout_issues_is_under_its_tenants_prefix(layout):
    for ref in _every_key(layout, TENANT):
        assert ref.key.startswith(f"tenants/{TENANT}/"), ref


def test_below_the_tenant_prefix_the_layout_is_the_same_for_every_tenant(layout):
    """What makes bucket-per-tenant a bucket-for-prefix swap (§9.11): strip the
    prefix, and two tenants' keys are identical."""
    ours = [
        (r.bucket, r.key.removeprefix(f"tenants/{TENANT}/"))
        for r in _every_key(layout, TENANT)
    ]
    theirs = [
        (r.bucket, r.key.removeprefix(f"tenants/{OTHER}/"))
        for r in _every_key(layout, OTHER)
    ]

    assert ours == theirs


def test_the_tenant_prefix_is_one_segment(layout):
    assert layout.tenant_prefix(TENANT) == f"tenants/{TENANT}"


# -- the legacy key resolver -----------------------------------------------------


def test_a_jpeg_v1_wrote_is_where_v1_wrote_it(layout):
    """v1's `jpeg_key`, in v1's labels bucket: what every migrated Label Studio
    task and label `image_url` points at."""
    assert layout.legacy_processed_jpeg("preprocess_jpeg", CHECKSUM) == ObjectRef(
        bucket="labels-fishsense-lite",
        key=f"fishsense-lite/preprocess_jpeg/{CHECKSUM}.JPG",
    )


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("", f"preprocess_jpeg/{CHECKSUM}.JPG"),
        (None, f"preprocess_jpeg/{CHECKSUM}.JPG"),
        ("fishsense-lite", f"fishsense-lite/preprocess_jpeg/{CHECKSUM}.JPG"),
        ("/fishsense-lite/", f"fishsense-lite/preprocess_jpeg/{CHECKSUM}.JPG"),
    ],
)
def test_the_legacy_key_handles_v1s_prefixes(prefix, expected):
    """v1's `test_jpeg_key_prefix_handling`: no double slash, no "None"."""
    layout = sut.ObjectLayout(_settings(legacy_labels_prefix=prefix))

    assert layout.legacy_processed_jpeg("preprocess_jpeg", CHECKSUM).key == expected


def test_a_frame_from_v1_resolves_to_v1s_key_then_its_new_one(layout):
    """v1's key first. v1 overwrote a frame's JPEG in place, so its URL never
    changed -- existing Label Studio tasks and label image_urls point at it,
    and populate dedupes tasks by URL. A migrated frame keeps that key for
    good: a redraw overwrites it where it is (`processed_jpeg_target`)."""
    assert layout.processed_jpeg_candidates(
        TENANT, "preprocess_jpeg", CHECKSUM, from_v1=True
    ) == [
        layout.legacy_processed_jpeg("preprocess_jpeg", CHECKSUM),
        layout.processed_jpeg(TENANT, "preprocess_jpeg", CHECKSUM),
    ]


def test_a_frame_v2_ingested_never_resolves_to_a_v1_key(layout):
    """v1's keys carry no tenant. Only a frame migrated from v1 -- which is the
    lab tenant's -- may resolve to one, or a partner with the same checksum
    could read a lab JPEG."""
    assert layout.processed_jpeg_candidates(
        TENANT, "preprocess_jpeg", CHECKSUM, from_v1=False
    ) == [layout.processed_jpeg(TENANT, "preprocess_jpeg", CHECKSUM)]

"""Stage 9's orchestrator activities: select, resolve, stage the PDF, clear.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_select_next_high_priority_dive_for_slate_preprocessing_activity.py
(the selector's thin wrapper), test_stage_slate_pdf_activity.py (names and
reasons are v1's) and the resolver's payload assembly from
test_resolve_slate_preprocess_inputs_activity.py -- the cohort and the frame
selection themselves are the API store's, tested on Postgres there.

v2 changes, each pinned here:

* the selector takes the oldest candidate across every tenant served;
* the resolver hands the processor refs, not checksums: each frame's staged
  raw key, the key its composite is written to -- over the JPEG where it
  already is, so a migrated frame's redraw keeps the URL its Label Studio
  task holds -- and the tenant's slate PDF key;
* the PDF is staged per tenant and template (v1: `slate_pdf/{slate_id}.pdf`),
  from the template's share-relative NAS path;
* a dive the resolver cannot resolve is a final refusal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.slate_store import (
    SlateInputsUnavailable,
    SlatePreprocessCandidate,
    SlatePreprocessCapture,
    SlatePreprocessInputs,
    SlateTemplate,
)
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings
from fishsense_services_orchestrator.object_store.contracts import StagingTarget
from fishsense_services_orchestrator.object_store.layout import ObjectLayout
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)
from fishsense_services_orchestrator.slates.activities import SlateActivities
from fishsense_services_orchestrator.slates.contracts import (
    ClearSlateFlagsInput,
    SlatePdfTarget,
)
from fishsense_services_orchestrator.slates.pdfs import SlatePdfs

BUCKET = "fishsense-test"
LABELS = "labels-fishsense-test"
LAB, REEF = uuid.UUID(int=1), uuid.UUID(int=2)
DIVE = uuid.UUID(int=440)
SLATE = uuid.UUID(int=7)
T0 = datetime(2026, 9, 1, tzinfo=UTC)
K = [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, 1.0]]
D = [-0.1, 0.05, 0.0, 0.0, 0.0]
NAS = NasSettings(
    url="https://nas.example.test:6021",
    username="svc",
    password="unused",
    raw_root_path="/fishsense_data/REEF/data",
)


def _template(**overrides) -> SlateTemplate:
    values = {
        "id": SLATE,
        "name": "V-Slate 2",
        "dpi": 300,
        "reference_points": [(0.0, 0.0), (1.0, 1.0)],
        "source_path": "slates/v1/slate.pdf",
    }
    values.update(overrides)
    return SlateTemplate(**values)


class FakeCatalog:
    def __init__(self, *, candidates=None, inputs=None, template=None):
        self.candidates = candidates or {}
        self.inputs = inputs
        self.template = template
        self.cleared = []

    async def member_tenants(self):
        return [LAB, REEF]

    async def next_dive_for_slate_preprocessing(self, tenant_id):
        return self.candidates.get(tenant_id)

    async def slate_preprocess_inputs(self, tenant_id, dive_id):
        if isinstance(self.inputs, Exception):
            raise self.inputs
        return self.inputs

    async def slate_template(self, tenant_id, slate_template_id):
        return (
            self.template
            if self.template and self.template.id == slate_template_id
            else None
        )

    async def clear_slate_reprocess_flags(self, tenant_id, dive_id, *, checksums):
        self.cleared.append((tenant_id, dive_id, checksums))
        return 3


class FakeNas:
    def __init__(self, data: bytes = b"pdf-bytes"):
        self.data = data
        self.calls = []

    def download_to(self, *, src_path: str, dest_dir: str) -> None:
        self.calls.append(src_path)
        (Path(dest_dir) / Path(src_path).name).write_bytes(self.data)


@pytest.fixture(name="s3")
def s3_fixture():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        client.create_bucket(Bucket=LABELS)
        yield client


def _store(s3) -> OrchestratorObjectStore:
    settings = ObjectStoreConnection(
        endpoint_url="http://garage.example.com",
        region="garage",
        access_key_id="k",
        secret_access_key="s",
        bucket=BUCKET,
        labels_bucket=LABELS,
        legacy_labels_prefix="fishsense-lite",
    )
    return OrchestratorObjectStore(s3, ObjectLayout(settings))


def _activities(s3, catalog, nas=None) -> SlateActivities:
    store = _store(s3)
    nas = nas or FakeNas()
    return SlateActivities(
        catalog=catalog,
        store=store,
        pdfs=SlatePdfs(
            catalog=catalog,
            store=store,
            nas_settings=NAS,
            nas_client_factory=lambda: nas,
        ),
    )


async def _run(fn, *args):
    return await ActivityEnvironment().run(fn, *args)


# ---------- the selector ----------


async def test_returns_the_oldest_candidate_across_tenants(s3):
    catalog = FakeCatalog(
        candidates={
            LAB: SlatePreprocessCandidate(uuid.UUID(int=10), T0 + timedelta(hours=2)),
            REEF: SlatePreprocessCandidate(uuid.UUID(int=20), T0),
        }
    )

    target = await _run(
        _activities(s3, catalog).select_next_dive_for_slate_preprocessing
    )

    assert target == StagingTarget(tenant_id=REEF, dive_id=uuid.UUID(int=20))


async def test_returns_none_when_no_dive(s3):
    """v1's `test_returns_none_when_no_dive`: an empty cohort is a quiet
    firing, not an error."""
    target = await _run(
        _activities(s3, FakeCatalog()).select_next_dive_for_slate_preprocessing
    )

    assert target is None


# ---------- the resolver ----------


def _inputs(captures) -> SlatePreprocessInputs:
    return SlatePreprocessInputs(
        dive_id=DIVE,
        slate_template=_template(),
        camera_calibration_id=uuid.UUID(int=99),
        camera_matrix=K,
        distortion_coefficients=D,
        captures=captures,
    )


async def test_the_payload_carries_the_template_camera_and_frames(s3):
    frame = SlatePreprocessCapture(uuid.UUID(int=3), "c" * 32, from_v1=False)
    catalog = FakeCatalog(inputs=_inputs([frame]))

    plan = await _run(
        _activities(s3, catalog).resolve_slate_preprocess_inputs,
        StagingTarget(tenant_id=LAB, dive_id=DIVE),
    )

    payload = plan.payload
    assert (payload.dive_id, payload.slate_template_id) == (DIVE, SLATE)
    assert payload.slate_dpi == 300
    assert payload.reference_points == [(0.0, 0.0), (1.0, 1.0)]
    assert (payload.camera_matrix, payload.distortion_coefficients) == (K, D)
    assert payload.slate_pdf.key == f"tenants/{LAB}/slate_pdf/{SLATE}.pdf"
    assert payload.slate_pdf.bucket == BUCKET
    (image,) = payload.images
    assert image.capture_id == frame.capture_id
    assert (image.raw.bucket, image.raw.key) == (
        BUCKET,
        f"tenants/{LAB}/raw/{'c' * 32}.ORF",
    )
    assert (image.jpeg.bucket, image.jpeg.key) == (
        LABELS,
        f"tenants/{LAB}/preprocess_slate_images_jpeg/{'c' * 32}.JPG",
    )
    assert plan.checksums == ["c" * 32]


async def test_a_migrated_frame_is_redrawn_where_v1_wrote_it(s3):
    """Label Studio tasks and label image_urls hold v1's key, so a redraw
    overwrites it in place, as v1 did, and the URL never moves."""
    v1_key = f"fishsense-lite/preprocess_slate_images_jpeg/{'d' * 32}.JPG"
    s3.put_object(Bucket=LABELS, Key=v1_key, Body=b"old")
    frame = SlatePreprocessCapture(uuid.UUID(int=4), "d" * 32, from_v1=True)

    plan = await _run(
        _activities(
            s3, FakeCatalog(inputs=_inputs([frame]))
        ).resolve_slate_preprocess_inputs,
        StagingTarget(tenant_id=LAB, dive_id=DIVE),
    )

    assert plan.payload.images[0].jpeg.key == v1_key


async def test_a_dive_that_cannot_be_resolved_is_refused_for_good(s3):
    """v1 raised a ValueError; retrying reads the same rows to the same
    answer, so it is final."""
    catalog = FakeCatalog(inputs=SlateInputsUnavailable("dive has no slate template"))

    with pytest.raises(ApplicationError) as excinfo:
        await _run(
            _activities(s3, catalog).resolve_slate_preprocess_inputs,
            StagingTarget(tenant_id=LAB, dive_id=DIVE),
        )

    assert excinfo.value.non_retryable
    assert excinfo.value.type == "SlateInputsUnavailable"
    assert "no slate template" in str(excinfo.value)


# ---------- staging the slate PDF (v1's test_stage_slate_pdf_activity) ----------


def _pdf_key(tenant=LAB) -> str:
    return f"tenants/{tenant}/slate_pdf/{SLATE}.pdf"


async def test_skips_nas_when_pdf_already_present(s3):
    s3.put_object(Bucket=BUCKET, Key=_pdf_key(), Body=b"old")
    nas = FakeNas()

    result = await _run(
        _activities(s3, FakeCatalog(template=_template()), nas).stage_slate_pdf,
        SlatePdfTarget(tenant_id=LAB, slate_template_id=SLATE),
    )

    assert result is True
    assert nas.calls == []
    # already-present PDF is not re-written
    assert s3.get_object(Bucket=BUCKET, Key=_pdf_key())["Body"].read() == b"old"


async def test_downloads_and_puts_when_pdf_missing(s3):
    """The template's path is share-relative; the activity must rewrite it to
    absolute before passing it to the NAS client."""
    nas = FakeNas(b"pdf-bytes")

    result = await _run(
        _activities(s3, FakeCatalog(template=_template()), nas).stage_slate_pdf,
        SlatePdfTarget(tenant_id=LAB, slate_template_id=SLATE),
    )

    assert result is True
    assert nas.calls == ["/fishsense_data/REEF/data/slates/v1/slate.pdf"]
    assert s3.get_object(Bucket=BUCKET, Key=_pdf_key())["Body"].read() == b"pdf-bytes"


async def test_raises_when_slate_missing(s3):
    with pytest.raises(ApplicationError, match="not found") as excinfo:
        await _run(
            _activities(s3, FakeCatalog()).stage_slate_pdf,
            SlatePdfTarget(tenant_id=LAB, slate_template_id=SLATE),
        )
    assert excinfo.value.non_retryable


async def test_raises_when_slate_path_is_empty(s3):
    with pytest.raises(ApplicationError, match="no NAS path"):
        await _run(
            _activities(
                s3, FakeCatalog(template=_template(source_path=""))
            ).stage_slate_pdf,
            SlatePdfTarget(tenant_id=LAB, slate_template_id=SLATE),
        )


async def test_the_pdf_is_staged_per_tenant(s3):
    """v2: every key is under its tenant, so one tenant's staged template
    does not stand in for another's."""
    s3.put_object(Bucket=BUCKET, Key=_pdf_key(REEF), Body=b"theirs")
    nas = FakeNas()

    await _run(
        _activities(s3, FakeCatalog(template=_template()), nas).stage_slate_pdf,
        SlatePdfTarget(tenant_id=LAB, slate_template_id=SLATE),
    )

    assert len(nas.calls) == 1


async def test_a_migrated_template_is_staged_from_v1s_pdf(s3):
    """v2: v1 staged each template once, at `slate_pdf/{v1 id}.pdf`, and it is
    still there. A migrated template is copied from it to the tenant's key
    rather than fetched from the NAS again -- v1's key is only read."""
    s3.put_object(Bucket=BUCKET, Key="slate_pdf/12.pdf", Body=b"v1-bytes")
    nas = FakeNas()

    result = await _run(
        _activities(s3, FakeCatalog(template=_template(v1_id=12)), nas).stage_slate_pdf,
        SlatePdfTarget(tenant_id=LAB, slate_template_id=SLATE),
    )

    assert result is True
    assert nas.calls == []
    assert s3.get_object(Bucket=BUCKET, Key=_pdf_key())["Body"].read() == b"v1-bytes"
    assert s3.get_object(Bucket=BUCKET, Key="slate_pdf/12.pdf")["Body"].read() == (
        b"v1-bytes"
    )


async def test_a_migrated_template_whose_v1_pdf_is_gone_is_fetched(s3):
    nas = FakeNas(b"pdf-bytes")

    await _run(
        _activities(s3, FakeCatalog(template=_template(v1_id=12)), nas).stage_slate_pdf,
        SlatePdfTarget(tenant_id=LAB, slate_template_id=SLATE),
    )

    assert len(nas.calls) == 1
    assert s3.get_object(Bucket=BUCKET, Key=_pdf_key())["Body"].read() == b"pdf-bytes"


async def test_only_a_migrated_template_reads_a_v1_key(s3):
    """v1's keys are by integer id, and a v2 template has none: nothing at a
    v1 key can be its PDF."""
    s3.put_object(Bucket=BUCKET, Key="slate_pdf/12.pdf", Body=b"another")
    nas = FakeNas(b"pdf-bytes")

    await _run(
        _activities(s3, FakeCatalog(template=_template()), nas).stage_slate_pdf,
        SlatePdfTarget(tenant_id=LAB, slate_template_id=SLATE),
    )

    assert len(nas.calls) == 1
    assert s3.get_object(Bucket=BUCKET, Key=_pdf_key())["Body"].read() == b"pdf-bytes"


def _pdf(width: float, height: float) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page(width=width, height=height)
    out = doc.tobytes()
    doc.close()
    return out


async def test_the_sync_stages_a_pdf_it_needs():
    """v2: every carried-over project's PDF lives at v1's key, not the
    tenant's, so a sync that only read scratch (v1) would fail every slate
    project after cutover until its dive re-entered stage 9. The aspect read
    stages the template first when it is missing."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        s3.create_bucket(Bucket=LABELS)
        nas = FakeNas(_pdf(216.0, 108.0))
        store = _store(s3)
        pdfs = SlatePdfs(
            catalog=FakeCatalog(template=_template()),
            store=store,
            nas_settings=NAS,
            nas_client_factory=lambda: nas,
        )

        aspect = await ActivityEnvironment().run(pdfs.aspect, LAB, SLATE)
        again = await ActivityEnvironment().run(pdfs.aspect, LAB, SLATE)

    assert aspect == pytest.approx(2.0) and again == pytest.approx(2.0)
    assert len(nas.calls) == 1, "staged once, then read from scratch"


async def test_the_sync_reads_a_migrated_templates_pdf_from_v1s_key(s3):
    """Every carried-over slate project's template is a migrated one, and its
    PDF is at v1's key: the sync needs neither the NAS nor a NAS path."""
    s3.put_object(Bucket=BUCKET, Key="slate_pdf/12.pdf", Body=_pdf(216.0, 108.0))
    nas = FakeNas()
    pdfs = SlatePdfs(
        catalog=FakeCatalog(template=_template(v1_id=12, source_path=None)),
        store=_store(s3),
        nas_settings=NAS,
        nas_client_factory=lambda: nas,
    )

    aspect = await ActivityEnvironment().run(pdfs.aspect, LAB, SLATE)

    assert aspect == pytest.approx(2.0)
    assert nas.calls == []


async def test_the_sync_cannot_stage_an_unknown_template(s3):
    pdfs = SlatePdfs(
        catalog=FakeCatalog(),
        store=_store(s3),
        nas_settings=NAS,
        nas_client_factory=FakeNas,
    )

    with pytest.raises(ValueError, match="not found"):
        await ActivityEnvironment().run(pdfs.aspect, LAB, SLATE)


# ---------- clearing the flags ----------


async def test_clears_in_the_scope_it_is_given(s3):
    catalog = FakeCatalog()

    cleared = await _run(
        _activities(s3, catalog).clear_slate_reprocess_flags,
        ClearSlateFlagsInput(tenant_id=LAB, dive_id=DIVE, checksums=["a"]),
    )
    await _run(
        _activities(s3, catalog).clear_slate_reprocess_flags,
        ClearSlateFlagsInput(tenant_id=LAB, dive_id=DIVE),
    )

    assert cleared == 3
    assert catalog.cleared == [(LAB, DIVE, ["a"]), (LAB, DIVE, None)]

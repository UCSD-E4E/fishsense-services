"""Shared fakes for the species stage's orchestrator tests: v1's integer image
ids become capture uuids (`capture(n)`), and the catalog answers from memory
the way v1's tests faked the SDK client."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from fishsense_services_api.species_store import (
    CameraIntrinsicsRow,
    SpeciesCapture,
    SpeciesLabelRow,
    SpeciesPreprocessFacts,
)
from fishsense_services_contracts.object_store import (
    ObjectRef,
    ObjectStoreConnection,
)
from fishsense_services_orchestrator.object_store.layout import ObjectLayout

TENANT = uuid.UUID("00000000-0000-0000-0000-00000000000a")
DIVE = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
T0 = datetime(2025, 1, 1, tzinfo=UTC)
K = [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, 1.0]]
D = [-0.1, 0.05, 0.0, 0.0, 0.0]

LAYOUT = ObjectLayout(
    ObjectStoreConnection(
        endpoint_url="https://s3.example.test",
        region="garage",
        access_key_id="unused",
        secret_access_key="unused",
        bucket="fishsense-lite",
        labels_bucket="labels-fishsense-lite",
        legacy_labels_prefix="fishsense-lite",
    )
)


def capture(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def checksum_of(name: str) -> str:
    """A 32-character checksum spelled from a short name ("aaa" -> "aaa...")."""
    return (name * 32)[:32]


def image(n: int, name: str, *, from_v1: bool = False, number=None) -> SpeciesCapture:
    return SpeciesCapture(
        capture_id=capture(n),
        number=n if number is None else number,
        checksum=checksum_of(name),
        from_v1=from_v1,
        captured_at=T0 + timedelta(seconds=n),
    )


def species(
    n: int,
    *,
    project: int | None = 70,
    completed: bool = False,
    superseded: bool = False,
    needs_reprocess: bool = False,
    number: int | None = None,
    **fields,
) -> SpeciesLabelRow:
    values = {
        "grouping": None,
        "top_three_photos_of_group": None,
        "content_of_image": None,
        "fish_measurable_category": None,
        "fish_angle_category": None,
        "fish_curved_category": None,
    }
    values.update(fields)
    return SpeciesLabelRow(
        id=uuid.uuid4(),
        number=n * 5 if number is None else number,
        capture_id=capture(n),
        ls_project_id=project,
        ls_task_id=None if project is None else n * 11,
        completed=completed,
        superseded=superseded,
        needs_reprocess=needs_reprocess,
        **values,
    )


def facts(
    *,
    images=(),
    clusters=(),
    valid=(),
    labels=(),
    device=uuid.UUID(int=99),
    intrinsics=CameraIntrinsicsRow(K, D),
) -> SpeciesPreprocessFacts:
    return SpeciesPreprocessFacts(
        device_id=device,
        intrinsics=intrinsics,
        captures=list(images),
        prediction_clusters=[[capture(n) for n in cluster] for cluster in clusters],
        valid_laser=frozenset(capture(n) for n in valid),
        species_labels=list(labels),
    )


class FakeStore:
    """The orchestrator's object store, as the species activities use it."""

    def __init__(self, jpegs=None):
        self.layout = LAYOUT
        #: checksum -> ObjectRef of an existing JPEG; None: every JPEG is
        #: written, at the tenant's key.
        self.jpegs = None if jpegs is None else dict(jpegs)
        self.located = []

    async def processed_jpeg_target(self, tenant_id, folder, checksum, *, from_v1):
        if from_v1:
            return self.layout.legacy_processed_jpeg(folder, checksum)
        return self.layout.processed_jpeg(tenant_id, folder, checksum)

    async def locate_processed_jpeg(self, tenant_id, folder, checksum, *, from_v1):
        self.located.append((tenant_id, folder, checksum, from_v1))
        if self.jpegs is None:
            return self.layout.processed_jpeg(tenant_id, folder, checksum)
        return self.jpegs.get(checksum)


@dataclass
class FakeSpeciesCatalog:
    """`fishsense_services_api.species_store.SpeciesCatalog`, in memory."""

    tenants: list = field(default_factory=lambda: [TENANT])
    preprocess: SpeciesPreprocessFacts | None = None
    population: object = None
    grouping: object = None
    candidates: dict = field(default_factory=dict)
    population_candidates: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    recorded: list = field(default_factory=list)
    superseded: list = field(default_factory=list)
    persisted: list = field(default_factory=list)
    slates: dict = field(default_factory=dict)
    targets: dict = field(default_factory=dict)
    links: list = field(default_factory=list)
    notes: dict = field(default_factory=dict)

    async def member_tenants(self):
        return self.tenants

    async def next_dive_for_species_preprocessing(self, tenant_id):
        return self.candidates.get(tenant_id)

    async def dives_needing_species_population(self, tenant_id):
        return self.population_candidates.get(tenant_id, [])

    async def species_preprocess_facts(self, tenant_id, dive_id):
        return self.preprocess

    async def set_species_needs_reprocess(
        self, tenant_id, dive_id, value, *, only_incomplete=True, capture_ids=None
    ):
        self.calls.append(("flag", dive_id, value, capture_ids))
        return 0

    async def species_population_facts(self, tenant_id, dive_id):
        return self.population

    async def record_species_label(
        self, tenant_id, *, capture_id, ls_project_id, ls_task_id, image_url
    ):
        self.recorded.append((capture_id, ls_project_id, ls_task_id, image_url))

    async def supersede_species_labels(self, tenant_id, label_ids):
        self.superseded.extend(label_ids)
        return len(label_ids)

    async def species_grouping_facts(self, tenant_id, dive_id):
        return self.grouping

    async def persist_label_studio_clusters(self, tenant_id, dive_id, groups):
        if self.grouping is not None and self.grouping.already_grouped:
            return None
        self.persisted.append((dive_id, groups))
        return len(groups)

    async def slate_templates_by_name(self, tenant_id):
        return self.slates

    async def calibration_targets_by_name(self, tenant_id):
        return self.targets

    async def set_dive_slate_template(self, tenant_id, dive_id, slate_template_id):
        self.links.append(("slate", dive_id, slate_template_id))
        return True

    async def set_dive_calibration_target(self, tenant_id, dive_id, target_id):
        self.links.append(("target", dive_id, target_id))
        return True

    async def note_unidentified_slate(self, tenant_id, dive_id, note):
        if self.notes.get(dive_id):
            return False
        self.notes[dive_id] = note
        return True


def with_(row, **changes):
    return replace(row, **changes)


def jpeg_ref(name: str) -> ObjectRef:
    return LAYOUT.processed_jpeg(TENANT, "preprocess_groups_jpeg", checksum_of(name))

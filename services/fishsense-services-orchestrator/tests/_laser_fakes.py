"""In-memory stand-ins for the laser activities' dependencies: the catalog
(`fishsense_services_api.laser_store.LaserCatalog`, which the API's tests pin
on real Postgres), the object store and Label Studio. v1's tests faked the API
client and the SDK the same way."""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fishsense_services_api.laser_store import (
    MissionCamera,
    ForeignRows,
    GateInputs,
    LabelPopulation,
    LaserCandidate,
    LaserCapture,
    LaserPopulation,
    PopulationChanged,
    TaskTargets,
    ValidationWrite,
)
from fishsense_services_contracts.object_store import ObjectRef, ObjectStoreConnection
from fishsense_services_orchestrator.labels.label_studio import LabelStudioProject
from fishsense_services_orchestrator.object_store.layout import ObjectLayout

T0 = datetime(2025, 3, 6, 17, 0, tzinfo=UTC)
K = [[3000.0, 0.0, 2000.0], [0.0, 3000.0, 1500.0], [0.0, 0.0, 1.0]]
D = [-0.05, 0.01, 0.0, 0.0, 0.0]


def capture(number, *, checksum=None, from_v1=False):
    return LaserCapture(
        capture_id=uuid.uuid5(uuid.NAMESPACE_OID, f"capture-{number}"),
        number=number,
        checksum=checksum or f"{number:032x}",
        from_v1=from_v1,
        captured_at=T0 + timedelta(seconds=number),
    )


def candidate(mission_id, *, age_days=0, number=1):
    return LaserCandidate(mission_id, T0 - timedelta(days=age_days), number)


@dataclass
class FakeCatalog:
    """Answers what a test sets; records what the activities write."""

    tenants: list = field(default_factory=list)
    next_mission: dict = field(default_factory=dict)  # (selector, tenant) -> candidate
    mission_lists: dict = field(default_factory=dict)  # (selector, tenant) -> [cand]
    captures: list = field(default_factory=list)
    camera: MissionCamera | None = None
    gate: GateInputs | None = None
    population: LaserPopulation | None = None
    targets: dict = field(default_factory=dict)  # auto_accepted_only -> targets
    labels: LabelPopulation | None = None
    has_labels_in_project: bool = False
    numbers: dict = field(default_factory=dict)  # (tenant, number) -> dive
    raise_on_write: Exception | None = None
    revive_raises: Exception | None = None
    calls: list = field(default_factory=list)

    async def member_tenants(self):
        return list(self.tenants)

    def _next(self, selector, tenant):
        return self.next_mission.get((selector, tenant))

    async def next_mission_for_laser_preprocessing(self, tenant):
        return self._next("preprocess", tenant)

    async def next_mission_for_laser_prediction(self, tenant):
        return self._next("predict", tenant)

    async def next_mission_for_laser_auto_accept(self, tenant):
        return self._next("gate", tenant)

    async def missions_needing_laser_population(self, tenant):
        return self.mission_lists.get(("populate", tenant), [])

    async def missions_with_complete_laser_labeling(self, tenant):
        return self.mission_lists.get(("complete", tenant), [])

    async def laser_preprocess_captures(self, tenant, mission):
        return list(self.captures)

    async def laser_predict_captures(self, tenant, mission):
        return list(self.captures)

    async def mission_camera(self, tenant, mission):
        return self.camera

    async def clear_laser_reprocess_flags(self, tenant, mission, capture_ids):
        self.calls.append(("clear", tenant, mission, capture_ids))
        return 0 if capture_ids == [] else 3

    def _write(self, *call):
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self.calls.append(call)

    async def persist_laser_predictions(self, tenant, mission, predictions):
        self._write("persist", tenant, mission, list(predictions))
        return len(predictions)

    async def laser_gate_inputs(self, tenant, mission):
        return self.gate

    async def record_laser_gate_verdicts(self, tenant, mission, verdicts):
        self._write("verdicts", tenant, mission, list(verdicts))
        return len(verdicts)

    async def laser_populate_items(self, tenant, mission):
        return self.population

    async def mission_has_laser_labels_in_project(self, tenant, mission, project):
        return self.has_labels_in_project

    async def record_populated_laser_labels(self, tenant, labels):
        self.calls.append(("record", tenant, list(labels)))
        return len(labels)

    async def laser_task_targets(self, tenant, mission, *, auto_accepted_only=False):
        return self.targets.get(auto_accepted_only, TaskTargets(1, [], []))

    async def mark_laser_labels_auto_accepted(self, tenant, tasks):
        self.calls.append(("mark", tenant, list(tasks)))
        return len(tasks)

    async def laser_label_population(self, tenant, mission):
        return self.labels

    async def apply_laser_validation(self, tenant, mission, supersedes, line):
        self._write("validation", tenant, mission, list(supersedes), line)
        return ValidationWrite(
            superseded=len(supersedes), line_appended=line is not None
        )

    async def revive_laser_labels(self, tenant, mission, numbers, fingerprint):
        if self.revive_raises is not None:
            raise self.revive_raises
        self.calls.append(("revive", tenant, mission, list(numbers), fingerprint))
        return len(numbers)

    async def mission_by_number(self, tenant, number):
        return self.numbers.get((tenant, number))

    async def mission_numbers(self, tenant):
        return sorted(n for t, n in self.numbers if t == tenant)


SETTINGS = ObjectStoreConnection(
    endpoint_url="https://s3.test",
    region="garage",
    access_key_id="k",
    secret_access_key="s",
    bucket="scratch",
    labels_bucket="labels",
    legacy_labels_prefix="fishsense-lite",
)


class FakeStore:
    """The orchestrator's object store: the real layout, JPEGs from a set."""

    def __init__(self, present=()):
        self.layout = ObjectLayout(SETTINGS)
        self.present = set(present)  # checksums whose laser JPEG is written

    async def locate_processed_jpeg(self, tenant, folder, checksum, *, from_v1):
        if checksum not in self.present:
            return None
        if from_v1:
            return self.layout.legacy_processed_jpeg(folder, checksum)
        return self.layout.processed_jpeg(tenant, folder, checksum)

    async def processed_jpeg_target(self, tenant, folder, checksum, *, from_v1):
        located = await self.locate_processed_jpeg(
            tenant, folder, checksum, from_v1=from_v1
        )
        return located or self.layout.processed_jpeg(tenant, folder, checksum)


class FakeLabelStudio:
    """Label Studio as the laser activities use it."""

    def __init__(self, *, tasks=None, predictions=None, untouched=None, title=""):
        self.tasks = dict(tasks or {})  # task id -> url
        self._ids = itertools.count(1000)
        self.imports: list = []
        self.updates: list = []
        self.predictions_by_project = dict(predictions or {})
        self.created_predictions: list = []
        self.untouched = untouched  # project -> set, or None: every task
        self.annotations: list = []
        self.title = title

    async def task_image_urls(self, project_id, *, beat):
        return list(self.tasks.items())

    async def import_tasks(self, project_id, tasks, *, beat):
        self.imports.append(list(tasks))
        for task in tasks:
            self.tasks[next(self._ids)] = task["data"]["image"]

    async def update_project(self, project_id, **fields):
        self.updates.append((project_id, fields))

    async def project(self, project_id):
        return LabelStudioProject(id=project_id, title=self.title)

    async def predictions(self, project_id):
        return list(self.predictions_by_project.get(project_id, []))

    async def create_prediction(self, task_id, model_version, result):
        self.created_predictions.append((task_id, model_version, list(result)))

    async def untouched_task_ids(self, project_id):
        if self.untouched is None:
            return set(self.tasks)
        return set(self.untouched.get(project_id, set()))

    async def create_annotation(self, task_id, project_id, result, ground_truth):
        self.annotations.append((task_id, project_id, list(result), ground_truth))


class FakeLabelProjects:
    def __init__(self, project_id=500):
        self.project_id = project_id
        self.calls: list = []

    async def ensure_mission_project(
        self, tenant, mission, kind, *, suffix, labeling_config_xml
    ):
        self.calls.append((tenant, mission, kind, suffix, labeling_config_xml))
        return self.project_id


__all__ = [
    "D",
    "FakeCatalog",
    "FakeLabelProjects",
    "FakeLabelStudio",
    "FakeStore",
    "ForeignRows",
    "K",
    "ObjectRef",
    "PopulationChanged",
    "candidate",
    "capture",
]

"""Finding or creating a dive's Label Studio project, record first.

v1 (fishsense-lite@77e8f8e5 create_{laser,species,headtail,dive_slate,
checkerboard_lattice}_label_studio_project_activity) built the title and
searched Label Studio for it every time. v2 records every project it creates
or finds (label_studio_projects) and looks there first, falling back to v1's
title search, which heals the record, so projects v1 created are still found.

What these pin:

* a recorded project is used without a title search, and is still healed
  and given its storage, as v1 did for the project its search found;
* a renamed dive keeps its project (v1 created a second one);
* a project found by title, or created, is recorded against the dive;
* a recorded project deleted in Label Studio is not fatal: the title search
  and create run as in v1;
* the title embeds the dive's number, so it is v1's title for a migrated dive;
* a project is recorded as soon as it exists, so a failure registering its
  storage cannot leave a project the registry doesn't know.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from label_studio_sdk.core import ApiError

from fishsense_services_api.label_project_store import DiveForTitle, RecordedProject
from fishsense_services_orchestrator.labels import populate as pu
from fishsense_services_orchestrator.labels.label_studio import LabelStudioClient
from fishsense_services_orchestrator.labels.populate import (
    LabelProjects,
    LabelStudioStorageSettings,
)

TENANT = uuid.uuid4()
DIVE = uuid.uuid4()
CONFIG = "<View><Image name='img' value='$image'/></View>"
STORAGE = LabelStudioStorageSettings(
    bucket="labels", endpoint_url="http://garage", region="garage",
    access_key="ak", secret_key="sk",
)  # fmt: skip


@dataclass
class FakeCatalog:
    """label_project_store's semantics, in memory."""

    dives: dict = field(default_factory=dict)
    #: (kind, dive_id, ls_project_id, title), oldest first.
    records: list = field(default_factory=list)

    async def dive_for_title(self, tenant_id, dive_id):
        assert tenant_id == TENANT
        return self.dives.get(dive_id)

    async def recorded_project(self, tenant_id, kind, *, dive_id, title):
        assert tenant_id == TENANT
        for k, d, p, t in reversed(self.records):
            if k == kind and t is not None and d == dive_id:
                if dive_id is not None or t == title:
                    return RecordedProject(p, t)
        return None

    async def record_project(self, tenant_id, kind, *, dive_id, ls_project_id, title):
        assert tenant_id == TENANT
        self.records = [
            r for r in self.records if (r[0], r[2]) != (kind, ls_project_id)
        ]
        self.records.append((kind, dive_id, ls_project_id, title))


def _sdk(*, projects=(), detail=None, created_id=900):
    sdk = MagicMock()
    sdk.workspaces.list.return_value = []
    sdk.projects.list.return_value = list(projects)
    sdk.import_storage.s3.list.return_value = []
    sdk.projects.create.return_value = SimpleNamespace(id=created_id)
    if detail is None:
        sdk.projects.get.side_effect = ApiError(status_code=404, body={})
    else:
        sdk.projects.get.return_value = detail
    return sdk


def _projects(sdk, catalog):
    return LabelProjects(
        catalog=catalog,
        label_studio=LabelStudioClient(sdk),
        workspace="",
        storage=STORAGE,
    )


def _catalog():
    return FakeCatalog(dives={DIVE: DiveForTitle(number=393, name="dive-alpha")})


async def test_a_recorded_project_is_used_without_a_title_search():
    catalog = _catalog()
    catalog.records.append(("laser", DIVE, 73, "dive-alpha #393 - Laser Labeling"))
    sdk = _sdk(detail=SimpleNamespace(id=73, title="whatever", label_config=CONFIG))

    pid = await _projects(sdk, catalog).ensure_dive_project(
        TENANT, DIVE, "laser", suffix="Laser Labeling", labeling_config_xml=CONFIG
    )

    assert pid == 73
    sdk.projects.get.assert_called_once_with(id=73)
    sdk.projects.list.assert_not_called()
    sdk.projects.create.assert_not_called()


async def test_a_recorded_project_is_healed_and_given_its_storage():
    """What v1 did for the project its title search found."""
    catalog = _catalog()
    catalog.records.append(("laser", DIVE, 73, "t"))
    sdk = _sdk(detail=SimpleNamespace(id=73, title="t", label_config="<View/>"))

    await _projects(sdk, catalog).ensure_dive_project(
        TENANT, DIVE, "laser", suffix="Laser Labeling", labeling_config_xml=CONFIG
    )

    sdk.projects.update.assert_called_once_with(id=73, label_config=CONFIG)
    assert sdk.import_storage.s3.create.call_args.kwargs["project"] == 73


async def test_a_renamed_dive_keeps_its_project():
    """v2 change: v1's next populate after a rename created a second project
    under the new title, splitting the dive's labels across two."""
    catalog = _catalog()
    catalog.records.append(("laser", DIVE, 73, "old-name #393 - Laser Labeling"))
    sdk = _sdk(detail=SimpleNamespace(id=73, title="old", label_config=CONFIG))

    pid = await _projects(sdk, catalog).ensure_dive_project(
        TENANT, DIVE, "laser", suffix="Laser Labeling", labeling_config_xml=CONFIG
    )

    assert pid == 73
    sdk.projects.create.assert_not_called()


async def test_a_migrated_project_is_found_by_v1s_title_and_recorded():
    """migrate-v1's records carry no title, so the first create after
    cutover finds the project the way v1 did, and records it."""
    catalog = _catalog()
    catalog.records.append(("laser", uuid.uuid4(), 73, None))  # migrate-v1's
    title = "dive-alpha #393 - Laser Labeling"
    sdk = _sdk(projects=[SimpleNamespace(id=73, title=title, label_config=CONFIG)])

    pid = await _projects(sdk, catalog).ensure_dive_project(
        TENANT, DIVE, "laser", suffix="Laser Labeling", labeling_config_xml=CONFIG
    )

    assert pid == 73
    sdk.projects.create.assert_not_called()
    assert catalog.records == [("laser", DIVE, 73, title)]


async def test_a_new_project_is_created_and_recorded():
    catalog = _catalog()
    sdk = _sdk(created_id=901)

    pid = await _projects(sdk, catalog).ensure_dive_project(
        TENANT, DIVE, "species", suffix="Species Labeling", labeling_config_xml=CONFIG
    )

    assert pid == 901
    sdk.projects.create.assert_called_once_with(
        title="dive-alpha #393 - Species Labeling", label_config=CONFIG, workspace=None
    )
    assert catalog.records == [
        ("species", DIVE, 901, "dive-alpha #393 - Species Labeling")
    ]


async def test_a_recorded_project_deleted_in_label_studio_is_replaced():
    catalog = _catalog()
    catalog.records.append(("laser", DIVE, 73, "dive-alpha #393 - Laser Labeling"))
    sdk = _sdk(created_id=902)  # get(73) is a 404

    pid = await _projects(sdk, catalog).ensure_dive_project(
        TENANT, DIVE, "laser", suffix="Laser Labeling", labeling_config_xml=CONFIG
    )

    assert pid == 902
    assert catalog.records[-1] == (
        "laser",
        DIVE,
        902,
        "dive-alpha #393 - Laser Labeling",
    )


async def test_a_dive_with_no_record_is_an_error():
    """v1 raised when the API had no such dive; so does v2 (for a dive of
    another tenant too, which the store does not see)."""
    sdk = _sdk()

    with pytest.raises(RuntimeError, match="no such dive"):
        await _projects(sdk, FakeCatalog()).ensure_dive_project(
            TENANT, DIVE, "laser", suffix="Laser Labeling", labeling_config_xml=CONFIG
        )
    sdk.projects.create.assert_not_called()


async def test_the_lattice_study_is_one_project_found_by_its_title():
    """The lattice study has no dive (v1 keeps its labeler blind to it)."""
    catalog = FakeCatalog()
    sdk = _sdk(created_id=500)
    projects = _projects(sdk, catalog)

    first = await projects.ensure_project(
        TENANT, "checkerboard_lattice", title="Checkerboard Lattice Verification",
        labeling_config_xml=CONFIG,
    )  # fmt: skip
    sdk.projects.get.side_effect = None
    sdk.projects.get.return_value = SimpleNamespace(
        id=500, title="Checkerboard Lattice Verification", label_config=CONFIG
    )
    again = await projects.ensure_project(
        TENANT, "checkerboard_lattice", title="Checkerboard Lattice Verification",
        labeling_config_xml=CONFIG,
    )  # fmt: skip

    assert first == again == 500
    sdk.projects.create.assert_called_once()
    assert catalog.records == [
        ("checkerboard_lattice", None, 500, "Checkerboard Lattice Verification")
    ]


async def test_a_project_is_recorded_before_its_storage(monkeypatch):
    """If registering the storage fails, the project already exists in Label
    Studio; the registry must know it, or the retry would rely on the title
    search alone."""

    async def storage_fails(*_a, **_k):
        raise RuntimeError("garage down")

    monkeypatch.setattr(pu, "ensure_label_studio_s3_storage", storage_fails)
    catalog = _catalog()
    sdk = _sdk(created_id=903)

    with pytest.raises(RuntimeError, match="garage down"):
        await _projects(sdk, catalog).ensure_dive_project(
            TENANT, DIVE, "laser", suffix="Laser Labeling", labeling_config_xml=CONFIG
        )

    assert catalog.records == [("laser", DIVE, 903, "dive-alpha #393 - Laser Labeling")]


async def test_an_unknown_kind_is_refused_before_label_studio_is_touched():
    sdk = _sdk()

    with pytest.raises(ValueError, match="kind"):
        await _projects(sdk, _catalog()).ensure_dive_project(
            TENANT, DIVE, "guesswork", suffix="X", labeling_config_xml=CONFIG
        )
    sdk.projects.list.assert_not_called()
    sdk.projects.create.assert_not_called()

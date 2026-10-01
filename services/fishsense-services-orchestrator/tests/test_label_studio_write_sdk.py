"""The write side against the real Label Studio SDK, over a mocked HTTP transport.

The other write-side tests fake the SDK. These don't: the SDK's own client,
pager and models build the requests and read Label Studio's JSON, so they pin
what goes over the wire -- the workspace filter, the draft create, the storage
registration, the import body, the PATCHes -- and that the adapter reads what
the SDK's models hand back (hosted Label Studio's resolve-wrapped task URLs,
its paged project list).
"""

from __future__ import annotations

import base64
import json
from collections import Counter

import httpx
from label_studio_sdk.client import LabelStudio
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_orchestrator.labels import populate as pu
from fishsense_services_orchestrator.labels.label_studio import (
    LabelStudioClient,
    LabelStudioProject,
)
from fishsense_services_orchestrator.labels.populate import LabelStudioStorageSettings

STORAGE = LabelStudioStorageSettings(
    bucket="labels-fishsense-lite",
    prefix="fishsense-lite",
    endpoint_url="https://s3.e4e.ucsd.edu",
    region="garage",
    access_key="ro-key",
    secret_key="ro-secret",
)
CONFIG = '<View><Image name="img" value="$image"/></View>'


class FakeLabelStudio:
    """Just enough of hosted Label Studio's REST API, recording every request."""

    def __init__(self, *, projects=(), tasks=(), storages=()):
        self.projects = {p["id"]: dict(p) for p in projects}
        self.tasks = list(tasks)
        self.storages = list(storages)
        self.requests: list[tuple[str, str, dict]] = []
        self.next_id = 900

    def _body(self, request):
        return json.loads(request.content) if request.content else {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.requests.append((method, path, dict(request.url.params)))
        page = int(request.url.params.get("page", "1"))
        if path == "/api/workspaces/":
            return httpx.Response(200, json=[{"id": 3, "title": "Personal"},
                                             {"id": 7, "title": "FishSense"}])  # fmt: skip
        if path == "/api/projects/" and method == "GET":
            if page > 1:
                return httpx.Response(404, json={"detail": "Invalid page."})
            results = list(self.projects.values())
            return httpx.Response(200, json={"count": len(results), "results": results})
        if path == "/api/projects/" and method == "POST":
            body = self._body(request)
            self.requests[-1] = (method, path, body)
            project = {"id": self.next_id, **body}
            self.projects[project["id"]] = project
            return httpx.Response(201, json=project)
        if path.startswith("/api/projects/") and path.endswith("/import"):
            body = self._body(request)
            self.requests[-1] = (method, path, {"tasks": body, **request.url.params})
            project = int(path.split("/")[3])
            for task in body:
                self.next_id += 1
                s3 = task["data"]["image"]
                wrapped = base64.b64encode(s3.encode()).decode()
                self.tasks.append({
                    "id": self.next_id, "project": project,
                    "data": {**task["data"],
                             "image": f"/tasks/{self.next_id}/resolve/?fileuri={wrapped}"},
                })  # fmt: skip
            return httpx.Response(201, json={"task_count": len(body), "import": 1})
        if path.startswith("/api/projects/"):
            project_id = int(path.split("/")[3])
            if project_id not in self.projects:
                return httpx.Response(404, json={"detail": "Not found."})
            if method == "PATCH":
                body = self._body(request)
                self.requests[-1] = (method, path, body)
                self.projects[project_id].update(body)
            return httpx.Response(200, json=self.projects[project_id])
        if path == "/api/storages/s3/" and method == "GET":
            return httpx.Response(200, json=self.storages)
        if path == "/api/storages/s3/" and method == "POST":
            body = self._body(request)
            self.requests[-1] = (method, path, body)
            self.storages.append({"id": len(self.storages) + 1, **body})
            return httpx.Response(201, json=self.storages[-1])
        if path == "/api/tasks/":
            if page > 1:
                return httpx.Response(404, json={"detail": "Invalid page."})
            project = int(request.url.params["project"])
            tasks = [t for t in self.tasks if t["project"] == project]
            return httpx.Response(200, json={"tasks": tasks, "total": len(tasks)})
        return httpx.Response(500, json={"detail": f"unexpected {method} {path}"})

    def client(self) -> LabelStudioClient:
        return LabelStudioClient(
            LabelStudio(
                base_url="https://label-studio.test",
                api_key="unused",
                httpx_client=httpx.Client(transport=httpx.MockTransport(self.handle)),
            )
        )

    def sent(self, method, path_end):
        return [body for m, p, body in self.requests
                if m == method and p.endswith(path_end)]  # fmt: skip


async def test_a_project_is_created_as_a_draft_in_the_workspace_with_its_storage():
    ls = FakeLabelStudio()

    project_id = await ActivityEnvironment().run(
        pu.create_or_get_label_studio_project,
        ls.client(),
        project_title="Reef #393 - Laser Calibration Labeling",
        labeling_config_xml=CONFIG,
        workspace="FishSense",
        storage=STORAGE,
    )

    assert project_id == 900
    listings = [
        p for m, path, p in ls.requests if path == "/api/projects/" and m == "GET"
    ]
    assert {page["workspaces"] for page in listings} == {"7"}
    assert ls.sent("POST", "/api/projects/") == [
        {"title": "Reef #393 - Laser Calibration Labeling",
         "label_config": CONFIG, "workspace": 7}
    ]  # fmt: skip
    (storage,) = ls.sent("POST", "/api/storages/s3/")
    assert storage == {
        "project": 900, "title": "garage", "bucket": "labels-fishsense-lite",
        "prefix": "fishsense-lite", "s3_endpoint": "https://s3.e4e.ucsd.edu",
        "region_name": "garage", "aws_access_key_id": "ro-key",
        "aws_secret_access_key": "ro-secret", "presign": True,
        "use_blob_urls": False,
    }  # fmt: skip


async def test_an_existing_project_is_found_healed_and_not_given_a_second_storage():
    ls = FakeLabelStudio(
        projects=[{"id": 42, "title": "Reef #393 - Laser Calibration Labeling",
                   "label_config": "<View/>"}],
        storages=[{"id": 1, "project": 42, "title": "garage",
                   "bucket": "labels-fishsense-lite"}],
    )  # fmt: skip

    project_id = await ActivityEnvironment().run(
        pu.create_or_get_label_studio_project,
        ls.client(),
        project_title="Reef #393 - Laser Calibration Labeling",
        labeling_config_xml=CONFIG,
        workspace="",
        storage=STORAGE,
    )

    assert project_id == 42
    assert ls.sent("POST", "/api/projects/") == []
    assert ls.sent("PATCH", "/api/projects/42/") == [{"label_config": CONFIG}]
    assert ls.sent("POST", "/api/storages/s3/") == []


async def test_tasks_are_imported_once_and_anchored_through_the_resolve_wrapper():
    """Hosted Label Studio lists a task's image as its presign resolve-wrapper.
    Read through the SDK's model, it must still match the built s3:// URL, or
    every run re-imports everything."""
    ls = FakeLabelStudio(projects=[{"id": 42, "title": "t"}])
    images = [
        pu.TaskImage(
            n, ObjectRef(bucket=STORAGE.bucket,
                         key=f"{STORAGE.prefix}/preprocess_jpeg/{c * 32}.JPG"), None
        )  # fmt: skip
        for n, c in ((1, "a"), (2, "b"))
    ]
    tasks = [{"data": pu.build_task_data(i)} for i in images]
    recorded = []

    async def record_label(item, task_id):
        recorded.append((item.number, task_id))

    async def populate():
        return await pu.import_tasks_and_record_labels(
            ls.client(), project_id=42, tasks=tasks,
            record_label=record_label, items=images,
        )  # fmt: skip

    first = await ActivityEnvironment().run(populate)
    second = await ActivityEnvironment().run(populate)

    (imported,) = ls.sent("POST", "/api/projects/42/import")
    assert [t["data"]["image_id"] for t in imported["tasks"]] == [1, 2]
    assert imported["return_task_ids"] == "true"
    assert first.complete and second.complete
    assert sorted(set(recorded)) == [(1, 901), (2, 902)]


async def test_publishing_and_showing_predictions_patch_the_project():
    ls = FakeLabelStudio(
        projects=[
            {"id": 42, "title": "Reef #94 - HeadTail Labeling", "model_version": ""}
        ]
    )
    client = ls.client()

    await pu.publish_label_studio_project(client, 42)
    updated = await pu.ensure_project_shows_predictions(
        client, 94, {42: Counter({"v2": 3})}, "v2"
    )

    assert updated == 1
    assert ls.sent("PATCH", "/api/projects/42/") == [
        {"is_published": True},
        {"model_version": "v2"},
    ]


async def test_a_missing_project_reads_as_none():
    assert await FakeLabelStudio().client().project(42) is None


async def test_a_project_reads_its_title_config_and_model_version():
    ls = FakeLabelStudio(
        projects=[
            {"id": 42, "title": "T", "label_config": CONFIG, "model_version": "v2"}
        ]
    )

    project = await ls.client().project(42)

    assert project == LabelStudioProject(
        id=42, title="T", label_config=CONFIG, model_version="v2"
    )

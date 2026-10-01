"""The Label Studio write side: workspace, create-or-get, heal, S3 storage, publish.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/tests/
test_populate_utils_workspace.py and the create/storage half of
test_create_laser_label_studio_project_activity.py (which v1 says covers the
create helper all four stages share). Names, bodies and reasons are v1's.

v2 adaptations, none of which changes behaviour:

* the SDK is wrapped once (`LabelStudioClient`), and the helpers take it
  rather than each fetching a global client;
* the workspace and the storage come from settings objects passed in, not a
  global `settings.reload()`;
* the title is built from the dive's `number` and name, which the caller
  looked up (`label_project_store.dive_for_title`), not fetched here.

v2 changes are marked where they are pinned. The species labeling-config tests
in v1's file belong to the species slice, which owns that XML.
"""

from __future__ import annotations

# Tests exercise internal helpers directly.
# pylint: disable=protected-access

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from label_studio_sdk.core import ApiError
from temporalio.testing import ActivityEnvironment

from fishsense_services_orchestrator.labels import label_studio as ls_mod
from fishsense_services_orchestrator.labels import populate as pu
from fishsense_services_orchestrator.labels.label_studio import LabelStudioClient
from fishsense_services_orchestrator.labels.populate import LabelStudioStorageSettings


def _storage(**overrides) -> LabelStudioStorageSettings:
    fields = {
        "bucket": "fishsense-test",
        "prefix": "",
        "endpoint_url": "http://garage.example.com",
        "region": "garage",
        "access_key": "ak",
        "secret_key": "sk",
        **overrides,
    }
    return LabelStudioStorageSettings(**fields)


def _workspace(ws_id: int, title: str):
    w = MagicMock()
    w.id = ws_id
    w.title = title
    return w


def _fake_ls(*, workspaces=(), existing_projects=(), created_id=123):
    ls = MagicMock()
    ls.workspaces.list.return_value = list(workspaces)
    ls.projects.list.return_value = list(existing_projects)
    created = MagicMock()
    created.id = created_id
    ls.projects.create.return_value = created
    return ls


async def _noop(*_a, **_k):
    return None


# -- the workspace ------------------------------------------------------------------


async def test_resolve_workspace_id_matches_by_title():
    ls = _fake_ls(workspaces=[_workspace(3, "Other"), _workspace(7, "FishSense")])
    assert await LabelStudioClient(ls).workspace_id("FishSense") == 7


async def test_resolve_workspace_id_none_when_unset():
    ls = _fake_ls(workspaces=[_workspace(7, "FishSense")])
    assert await LabelStudioClient(ls).workspace_id("  ") is None
    ls.workspaces.list.assert_not_called()


async def test_resolve_workspace_id_raises_when_configured_but_missing():
    ls = _fake_ls(workspaces=[_workspace(7, "FishSense")])
    with pytest.raises(RuntimeError, match="workspace 'Nope' not found"):
        await LabelStudioClient(ls).workspace_id("Nope")


# -- create or get ------------------------------------------------------------------


async def _create_or_get(ls, title, xml, *, workspace=""):
    return await pu.create_or_get_label_studio_project(
        LabelStudioClient(ls),
        project_title=title,
        labeling_config_xml=xml,
        workspace=workspace,
        storage=_storage(),
    )


async def test_create_scopes_and_creates_in_workspace(monkeypatch):
    ls = _fake_ls(workspaces=[_workspace(7, "FishSense")], existing_projects=[])
    monkeypatch.setattr(pu, "ensure_label_studio_s3_storage", _noop)

    pid = await _create_or_get(
        ls, "2024 dive 3 - Laser Labeling", "<View/>", workspace="FishSense"
    )

    assert pid == 123
    ls.projects.list.assert_called_once_with(workspaces=[7])
    _, kwargs = ls.projects.create.call_args
    assert kwargs["workspace"] == 7


async def test_create_idempotent_finds_existing_in_workspace(monkeypatch):
    existing = MagicMock()
    existing.id = 55
    existing.title = "X - Laser Labeling"
    ls = _fake_ls(workspaces=[_workspace(7, "FishSense")], existing_projects=[existing])
    monkeypatch.setattr(pu, "ensure_label_studio_s3_storage", _noop)

    pid = await _create_or_get(
        ls, "X - Laser Labeling", "<View/>", workspace="FishSense"
    )

    assert pid == 55
    ls.projects.create.assert_not_called()


async def test_create_does_not_publish(monkeypatch):
    # Projects are created as drafts (no is_published on create) and never
    # published from the create path -- publishing is deferred to the populate
    # activities once the task set is complete (see publish_label_studio_project).
    ls = _fake_ls(workspaces=[], existing_projects=[])
    monkeypatch.setattr(pu, "ensure_label_studio_s3_storage", _noop)

    await _create_or_get(ls, "X - Laser Labeling", "<View/>")

    _, kwargs = ls.projects.create.call_args
    assert "is_published" not in kwargs
    ls.projects.update.assert_not_called()


async def test_publish_label_studio_project_sets_is_published():
    ls = _fake_ls()

    await pu.publish_label_studio_project(LabelStudioClient(ls), 55)

    ls.projects.update.assert_called_once_with(id=55, is_published=True)


async def test_create_without_workspace_uses_default(monkeypatch):
    ls = _fake_ls(workspaces=[], existing_projects=[])
    monkeypatch.setattr(pu, "ensure_label_studio_s3_storage", _noop)

    pid = await _create_or_get(ls, "X - Laser Labeling", "<View/>")

    assert pid == 123
    ls.projects.list.assert_called_once_with()  # no workspace filter
    _, kwargs = ls.projects.create.call_args
    assert kwargs["workspace"] is None


# v1's test_create_laser_label_studio_project_activity.py, the helper's half.


async def test_returns_existing_project_id_by_title_match(monkeypatch):
    expected_title = "dive-alpha #393 - Laser Calibration Labeling"
    ls = _fake_ls(
        existing_projects=[
            SimpleNamespace(id=42, title="Random Other Project", label_config=None),
            SimpleNamespace(id=73, title=expected_title, label_config="<View/>"),
        ]
    )
    monkeypatch.setattr(pu, "ensure_label_studio_s3_storage", _noop)

    assert await _create_or_get(ls, expected_title, "<View/>") == 73
    ls.projects.create.assert_not_called()


async def test_creates_project_when_no_match_and_xml_present(monkeypatch):
    expected_title = "dive-alpha #393 - Laser Calibration Labeling"
    ls = _fake_ls(existing_projects=[], created_id=101)
    monkeypatch.setattr(pu, "ensure_label_studio_s3_storage", _noop)

    assert await _create_or_get(ls, expected_title, "<View><Image/></View>") == 101
    ls.projects.create.assert_called_once_with(
        title=expected_title, label_config="<View><Image/></View>", workspace=None
    )


async def test_raises_when_no_match_and_xml_constant_empty(monkeypatch):
    ls = _fake_ls(existing_projects=[])
    monkeypatch.setattr(pu, "ensure_label_studio_s3_storage", _noop)

    with pytest.raises(RuntimeError) as exc_info:
        await _create_or_get(ls, "dive-alpha #393 - Laser Calibration Labeling", "")

    assert "labeling-config XML" in str(exc_info.value)
    ls.projects.create.assert_not_called()


async def test_a_throttled_create_is_retried_not_failed(monkeypatch):
    """v2 change: v1 backed off 429s only on the import path's listing and
    import, so a throttle on create, heal, storage or publish failed the
    activity. Every write-side call now goes through the adapter's throttle
    handling."""
    monkeypatch.setattr(ls_mod, "_throttle_sleep", _noop)
    monkeypatch.setattr(pu, "ensure_label_studio_s3_storage", _noop)
    ls = _fake_ls(existing_projects=[], created_id=101)
    ls.projects.create.side_effect = [
        ApiError(status_code=429, body={"detail": "Request was throttled."}),
        SimpleNamespace(id=101),
    ]

    pid = await ActivityEnvironment().run(
        _create_or_get, ls, "X - Laser Labeling", "<View/>"
    )

    assert pid == 101
    assert ls.projects.create.call_count == 2


# -- titles ---------------------------------------------------------------------------


def test_same_named_dives_get_distinct_titles():
    # Real prod case: dives 439 and 440 share the (mislabeled) name. The
    # #number tail must keep their LS projects distinct.
    t439 = pu.build_per_dive_title(
        439, "101624_AlligatorDeep_FSL02", "Species Labeling"
    )
    t440 = pu.build_per_dive_title(
        440, "101624_AlligatorDeep_FSL02", "Species Labeling"
    )
    assert t439 != t440
    assert "#439" in t439 and "#440" in t440
    assert len(t439) <= pu.LS_PROJECT_TITLE_MAX


def test_title_truncates_long_name_but_keeps_id():
    t = pu.build_per_dive_title(393, "x" * 80, "Laser Calibration Labeling")
    assert len(t) <= pu.LS_PROJECT_TITLE_MAX
    assert "#393 - Laser Calibration Labeling" in t


def test_title_nameless_dive_is_id_and_suffix():
    assert pu.build_per_dive_title(7, None, "Species Labeling") == (
        "#7 - Species Labeling"
    )


def test_title_of_a_long_name_is_v1s_exact_title():
    """v1's worked example, character for character: a migrated project is
    found only if v2 builds exactly the title v1 did."""
    long_name = "2024-08-21 Florida Keys reef survey dive 03 (cohort A)"
    assert pu.build_per_dive_title(393, long_name, "Laser Calibration Labeling") == (
        "2024-08-21 Flori #393 - Laser Calibration Labeling"
    )


# -- image URLs and the S3 storage ------------------------------------------------------


def test_normalize_image_url_decodes_hosted_ls_resolve_wrapper():
    s3 = "s3://labels-fishsense-lite/fishsense-lite/preprocess_groups_jpeg/abc.JPG"
    wrapper = "/tasks/999/resolve/?fileuri=" + base64.b64encode(s3.encode()).decode()
    # Hosted LS lists tasks with the resolve-wrapper; must decode back to s3://
    # so dedup/resolve match the built s3:// URLs (else re-import every run).
    assert pu._normalize_image_url(wrapper) == s3
    assert pu._normalize_image_url(s3) == s3  # raw s3:// passes through
    assert pu._normalize_image_url(None) is None
    assert pu._normalize_image_url("/tasks/1/resolve/?fileuri=!!bad") == (
        "/tasks/1/resolve/?fileuri=!!bad"  # undecodable -> returned as-is
    )


# -- labeling-config self-heal ------------------------------------------------------------
#
# Editing a `<STAGE>_LABELING_CONFIG_XML` constant used to affect only
# projects created after the deploy: `create_or_get_label_studio_project`
# found the existing project by title and returned its id untouched, so
# every already-created per-dive project kept the config it was born with.
# A taxonomy change (e.g. swapping the Fish Model choices) therefore never
# reached annotators. These pin the converge-on-drift behavior.


def _project(pid: int, title: str, label_config=None):
    p = MagicMock()
    p.id = pid
    p.title = title
    p.label_config = label_config
    return p


_CFG_A = '<View><Choices name="x"><Choice value="Old"/></Choices></View>'
_CFG_B = '<View><Choices name="x"><Choice value="New"/></Choices></View>'


async def test_heal_rewrites_config_when_choices_changed():
    ls = _fake_ls()
    changed = await pu.heal_labeling_config(
        LabelStudioClient(ls), _project(5, "T", _CFG_A), _CFG_B
    )

    assert changed is True
    ls.projects.update.assert_called_once_with(id=5, label_config=_CFG_B)


async def test_heal_is_noop_when_only_formatting_differs():
    """The anti-churn guard. LS reformats `label_config` server-side, so a
    raw string compare would report drift forever and re-PATCH every project
    on every hourly run."""
    reformatted = (
        "<View>\n"
        '  <Choices   name="x">\n'
        '    <Choice value="Old"></Choice>\n'
        "  </Choices>\n"
        "</View>\n"
    )
    ls = _fake_ls()
    changed = await pu.heal_labeling_config(
        LabelStudioClient(ls), _project(5, "T", reformatted), _CFG_A
    )

    assert changed is False
    ls.projects.update.assert_not_called()


async def test_heal_compares_unparseable_configs_by_whitespace():
    """Unparseable on either side: compare whitespace-normalized text rather
    than looping on a PATCH that can never converge."""
    ls = _fake_ls()
    client = LabelStudioClient(ls)

    assert not await pu.heal_labeling_config(client, _project(5, "T", "<a  b"), "<a b")
    assert await pu.heal_labeling_config(client, _project(5, "T", "<a b"), "<a c")


async def test_heal_fetches_detail_when_list_omits_config():
    ls = _fake_ls()
    ls.projects.get.return_value = _project(5, "T", _CFG_A)

    changed = await pu.heal_labeling_config(
        LabelStudioClient(ls), _project(5, "T", None), _CFG_B
    )

    ls.projects.get.assert_called_once_with(id=5)
    assert changed is True


async def test_heal_survives_ls_rejecting_the_config():
    """LS refuses a config that would invalidate existing annotations. That
    must not fail the whole populate stage."""
    ls = _fake_ls()
    ls.projects.update.side_effect = ApiError(status_code=400, body="in use")

    changed = await pu.heal_labeling_config(
        LabelStudioClient(ls), _project(5, "T", _CFG_A), _CFG_B
    )

    assert changed is False  # swallowed, not raised


async def test_create_or_get_heals_existing_project_config(monkeypatch):
    """End of the wiring: an existing per-dive project converges on the
    current constant instead of keeping its birth config."""
    existing = _project(55, "X - Species Labeling", _CFG_A)
    ls = _fake_ls(workspaces=[], existing_projects=[existing])
    monkeypatch.setattr(pu, "ensure_label_studio_s3_storage", _noop)

    pid = await _create_or_get(ls, "X - Species Labeling", _CFG_B)

    assert pid == 55
    ls.projects.create.assert_not_called()
    ls.projects.update.assert_called_once_with(id=55, label_config=_CFG_B)


def test_a_task_shows_the_jpeg_where_the_object_store_located_it():
    """The URL is the located JPEG's, not one rebuilt from settings: v1's key
    for a migrated frame (so the URL dedupe still matches its tasks), the
    tenant's for a new one -- never a key nothing wrote."""
    from fishsense_services_contracts.object_store import ObjectRef
    from fishsense_services_orchestrator.labels.populate import (
        TaskImage,
        build_task_data,
    )

    ref = ObjectRef(bucket="labels-fishsense-lite",
                    key="tenants/0/preprocess_jpeg/abc.JPG")  # fmt: skip

    data = build_task_data(TaskImage(number=7, image=ref, captured_at=None))

    assert data["image"] == data["img"] == ref.uri

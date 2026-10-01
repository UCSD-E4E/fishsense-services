"""The workspace-wide labeling-config reconcile.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_reconcile_labeling_configs_activity.py.

The heal inside create-or-get only runs during populate, and populate stops
dispatching for a dive once it is fully populated, so it converges projects
that are still filling and never the finished ones labelers work in. Measured
in prod on 2026-07-21 after the Fish Model taxonomy swap: all 11 species
projects still in the populate cohort picked up the new choices;
`082923_FishModels_FSL02 #58 - Species Labeling` (pid 274353), fully populated
and out of cohort, kept the old list. The out-of-cohort case is the one these
tests care most about.

v2 changes, each pinned here:

* **the configs are declared by the slices that own them.** v1 imported the
  four stages' XML constants into the reconcile module; in v2 each kind's
  slice declares its `LabelingConfig` in `<package>/labeling_config.py`, and
  the reconcile discovers them at startup (as the stage registry discovers
  stages), so a malformed one fails the worker's start;
* Label Studio is reached through the one adapter (`LabelStudioClient`), whose
  calls back off through 429s.
"""

from __future__ import annotations

import sys
import textwrap
import uuid
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from label_studio_sdk.core import ApiError
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_orchestrator.labels.label_studio import LabelStudioClient
from fishsense_services_orchestrator.ops.labeling_configs import activities as sut
from fishsense_services_orchestrator.ops.labeling_configs.activities import (
    ReconcileLabelingConfigsResult,
)
from fishsense_services_orchestrator.ops.labeling_configs.registry import (
    InvalidLabelingConfig,
    LabelingConfig,
    labeling_configs,
)
from fishsense_services_orchestrator.ops.labeling_configs.workflow import (
    ReconcileLabelingConfigsWorkflow,
)

SPECIES_XML = '<View><Choices name="species"><Choice value="Grouper"/></Choices></View>'
HEADTAIL_XML = '<View><KeyPointLabels name="kp-1"><Label value="Snout"/></KeyPointLabels></View>'  # fmt: skip
OLD_SPECIES_XML = '<View><Choices name="x"><Choice value="George"/></Choices></View>'

CONFIGS = (
    LabelingConfig(kind="species", title_suffix="Species Labeling", xml=SPECIES_XML),
    LabelingConfig(kind="head_tail", title_suffix="HeadTail Labeling", xml=HEADTAIL_XML),
)  # fmt: skip


def _project(pid: int, title: str, label_config: str | None):
    p = MagicMock()
    p.id = pid
    p.title = title
    p.label_config = label_config
    return p


def _workspace(ws_id: int, title: str):
    w = MagicMock()
    w.id = ws_id
    w.title = title
    return w


def _fake_ls(projects, *, workspaces=()):
    ls = MagicMock()
    ls.workspaces.list.return_value = list(workspaces)
    ls.projects.list.return_value = list(projects)
    return ls


async def _reconcile(ls, *, workspace="", configs=CONFIGS):
    activities = sut.LabelingConfigActivities(
        label_studio_factory=lambda: LabelStudioClient(ls),
        workspace=workspace,
        configs=configs,
    )
    return await ActivityEnvironment().run(activities.reconcile_labeling_configs)


# -- the reconcile (v1's five) ------------------------------------------------------


async def test_heals_a_project_whose_dive_left_the_populate_cohort():
    """The 274353 case: fully populated, out of cohort, stale config."""
    stale = _project(
        274353, "082923_FishModels_FSL02 #58 - Species Labeling", OLD_SPECIES_XML
    )
    ls = _fake_ls([stale])

    result = await _reconcile(ls)

    assert result.healed == 1
    ls.projects.update.assert_called_once_with(id=274353, label_config=SPECIES_XML)


async def test_is_a_no_op_when_every_config_is_current():
    """Runs hourly: an unchanged pass must write nothing."""
    ls = _fake_ls(
        [
            _project(1, "d #1 - Species Labeling", SPECIES_XML),
            _project(2, "d #2 - HeadTail Labeling", HEADTAIL_XML),
        ]
    )

    result = await _reconcile(ls)

    assert (result.scanned, result.healed, result.unchanged) == (2, 0, 2)
    ls.projects.update.assert_not_called()


async def test_routes_each_project_to_its_own_stage_config():
    """A head/tail project must never receive the species config."""
    ls = _fake_ls(
        [
            _project(1, "d #1 - Species Labeling", OLD_SPECIES_XML),
            _project(2, "d #2 - HeadTail Labeling", OLD_SPECIES_XML),
        ]
    )

    await _reconcile(ls)

    sent = {
        c.kwargs["id"]: c.kwargs["label_config"]
        for c in ls.projects.update.call_args_list
    }
    assert sent == {1: SPECIES_XML, 2: HEADTAIL_XML}


async def test_leaves_projects_it_does_not_own_alone():
    """The workspace holds unrelated projects (demos, Coral Gardeners); the
    per-dive title suffix keeps the reconcile off them."""
    ls = _fake_ls(
        [
            _project(9, "Coral Gardeners", OLD_SPECIES_XML),
            _project(10, "Demo Project: Image Captioning", OLD_SPECIES_XML),
        ]
    )

    result = await _reconcile(ls)

    assert (result.scanned, result.unrecognized) == (0, 2)
    ls.projects.update.assert_not_called()


async def test_one_rejected_project_does_not_stop_the_pass():
    """LS refuses a config that would invalidate existing annotations. That
    must not abort the walk."""
    ls = _fake_ls(
        [
            _project(1, "d #1 - Species Labeling", OLD_SPECIES_XML),
            _project(2, "d #2 - Species Labeling", OLD_SPECIES_XML),
        ]
    )
    ls.projects.update.side_effect = [ApiError(status_code=400, body="in use"), None]

    result = await _reconcile(ls)

    assert result.scanned == 2
    assert result.healed == 1  # the second one still went through


# -- where it looks -----------------------------------------------------------------


async def test_walks_only_the_configured_workspace():
    """Every tenant's projects share one workspace; the reconcile walks it,
    and nothing else."""
    ls = _fake_ls([], workspaces=[_workspace(3, "Other"), _workspace(7, "FishSense")])

    await _reconcile(ls, workspace="FishSense")

    ls.projects.list.assert_called_once_with(workspaces=[7])


async def test_walks_every_project_when_no_workspace_is_configured():
    """OSS Label Studio, locally: no workspace, so every project."""
    ls = _fake_ls([])

    await _reconcile(ls, workspace="")

    ls.projects.list.assert_called_once_with()


async def test_a_listing_without_the_config_is_healed_from_the_detail_view():
    """`projects.list` may omit `label_config`; the detail view has it."""
    listed = _project(5, "d #5 - Species Labeling", None)
    ls = _fake_ls([listed])
    ls.projects.get.return_value = _project(5, listed.title, SPECIES_XML)

    result = await _reconcile(ls)

    assert (result.scanned, result.unchanged) == (1, 1)
    ls.projects.get.assert_called_once_with(id=5)


async def test_the_longest_suffix_wins():
    """A suffix that ends another must not shadow it (v1 matched longest
    first)."""
    configs = (
        LabelingConfig(kind="species", title_suffix="Slate Labeling", xml=SPECIES_XML),
        LabelingConfig(kind="slate", title_suffix="Dive Slate Labeling", xml=HEADTAIL_XML),
    )  # fmt: skip
    ls = _fake_ls([_project(1, "d #1 - Dive Slate Labeling", OLD_SPECIES_XML)])

    await _reconcile(ls, configs=configs)

    ls.projects.update.assert_called_once_with(id=1, label_config=HEADTAIL_XML)


async def test_with_no_configs_declared_nothing_is_touched():
    """Until the kinds' slices declare theirs, a pass changes nothing."""
    ls = _fake_ls([_project(1, "d #1 - Species Labeling", OLD_SPECIES_XML)])

    result = await _reconcile(ls, configs=())

    assert (result.scanned, result.unrecognized) == (0, 1)
    ls.projects.update.assert_not_called()


# -- the configs, declared by the slices that own them ---------------------------------


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    """A package tree standing in for the orchestrator's: `write(pkg, body)`
    gives package `pkg` a `labeling_config.py`."""
    root = tmp_path / "fake_orchestrator"
    root.mkdir()
    (root / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))

    def write(pkg: str, body: str | None) -> None:
        (root / pkg).mkdir()
        (root / pkg / "__init__.py").write_text("")
        if body is not None:
            (root / pkg / "labeling_config.py").write_text(textwrap.dedent(body))

    yield write
    for name in [m for m in sys.modules if m.startswith("fake_orchestrator")]:
        del sys.modules[name]


def _discover():
    import fake_orchestrator  # pylint: disable=import-error,import-outside-toplevel

    return labeling_configs(fake_orchestrator)


IMPORT = "from fishsense_services_orchestrator.ops.labeling_configs.registry import LabelingConfig\n"  # fmt: skip


def test_each_slice_declares_its_config_in_its_own_package(fake_root):
    fake_root("species", IMPORT + 'LABELING_CONFIGS = [LabelingConfig("species", "Species Labeling", "<View/>")]')  # fmt: skip
    fake_root("headtail", IMPORT + 'LABELING_CONFIGS = [LabelingConfig("head_tail", "HeadTail Labeling", "<View/>")]')  # fmt: skip
    fake_root("clustering", None)

    assert {c.kind for c in _discover()} == {"species", "head_tail"}


def test_a_module_that_declares_nothing_fails_the_start(fake_root):
    fake_root("species", "CONFIG = 1\n")

    with pytest.raises(InvalidLabelingConfig, match="LABELING_CONFIGS"):
        _discover()


def test_an_unknown_kind_fails_the_start(fake_root):
    fake_root("fish", IMPORT + 'LABELING_CONFIGS = [LabelingConfig("fish", "Fish Labeling", "<View/>")]')  # fmt: skip

    with pytest.raises(InvalidLabelingConfig, match="kind"):
        _discover()


def test_two_configs_for_one_suffix_fail_the_start(fake_root):
    fake_root("a", IMPORT + 'LABELING_CONFIGS = [LabelingConfig("species", "Species Labeling", "<View/>")]')  # fmt: skip
    fake_root("b", IMPORT + 'LABELING_CONFIGS = [LabelingConfig("head_tail", "Species Labeling", "<View/>")]')  # fmt: skip

    with pytest.raises(InvalidLabelingConfig, match="Species Labeling"):
        _discover()


def test_an_empty_config_fails_the_start():
    """An empty XML would push an empty config onto every project of the kind."""
    with pytest.raises(InvalidLabelingConfig):
        LabelingConfig(kind="species", title_suffix="Species Labeling", xml="  ")
    with pytest.raises(InvalidLabelingConfig):
        LabelingConfig(kind="species", title_suffix="", xml="<View/>")


def test_the_orchestrators_declarations_load():
    """Whatever the kinds' slices have declared so far loads and validates,
    as it must for the worker to start."""
    assert all(isinstance(c, LabelingConfig) for c in labeling_configs())


# -- the workflow -------------------------------------------------------------------


async def test_the_workflow_runs_the_reconcile_with_v1s_timeouts():
    """Sized for a workspace-wide walk (a list, plus a detail fetch per project
    the list omits the config of); heartbeated per project, so a slow Label
    Studio shows up as a heartbeat timeout rather than eating the window."""

    @activity.defn(name="reconcile_labeling_configs")
    async def stub() -> ReconcileLabelingConfigsResult:
        return ReconcileLabelingConfigsResult(scanned=2, healed=1, unchanged=1)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="test-reconcile",
            workflows=[ReconcileLabelingConfigsWorkflow],
            activities=[stub],
        ):
            handle = await env.client.start_workflow(
                ReconcileLabelingConfigsWorkflow.run,
                id=f"test-reconcile-{uuid.uuid4()}",
                task_queue="test-reconcile",
            )
            result = await handle.result()
            history = await handle.fetch_history()

    assert (result.scanned, result.healed, result.unchanged) == (2, 1, 1)
    (scheduled,) = [
        e.activity_task_scheduled_event_attributes
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    assert scheduled.schedule_to_close_timeout.ToTimedelta() == timedelta(minutes=15)
    assert scheduled.heartbeat_timeout.ToTimedelta() == timedelta(minutes=2)

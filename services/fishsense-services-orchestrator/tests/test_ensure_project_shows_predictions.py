"""Which `model_version` a per-dive Label Studio project is pointed at.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/tests/
test_ensure_project_shows_predictions.py. Names, bodies and reasons are v1's;
the fake is the SDK under `LabelStudioClient`, and the dive is named by its
`number` (v1's dive id for a migrated dive), which is what titles embed.

Attaching a prediction is not enough to show one: LS surfaces predictions to
annotators only for the version named in the *project's* `model_version`. A
labeler worked five frames of dive 94 by hand while 334 predictions sat
invisible, because both backfills attach via `predictions.create`, which does
not set that field, while `import_tasks` sets it for free.

The two rules below are each a bug that was written and then caught:

* the tier is chosen by **count**, not by "newest wins" -- switching a project
  showing 40 fallback predictions to a tier holding 2 blanks 38 tasks to
  reveal 2;
* only **per-dive** projects are touched -- `model_version` is project-global
  while these counts come from one dive, so on a shared project two dives at
  different tiers would overwrite each other on alternate runs.
"""

from __future__ import annotations

import asyncio
from collections import Counter

from fishsense_services_orchestrator.labels.label_studio import LabelStudioClient
from fishsense_services_orchestrator.labels.populate import (
    ensure_project_shows_predictions,
)

DIVE = 94
CURRENT = "v2 crop=1800x1350"
FALLBACK = "v-1 crop=1800x1350"


class _LS:
    """Stands in for the LS SDK's `.projects.get/update`."""

    def __init__(self, projects):
        # projects: {id: (title, model_version)}
        self.projects = self
        self._projects = dict(projects)
        self.updates = []
        self.raises_on = set()

    def get(self, id):  # noqa: A002  pylint: disable=redefined-builtin
        if id in self.raises_on:
            raise RuntimeError("no write scope for this project")
        title, version = self._projects[id]
        return type("_P", (), {"title": title, "model_version": version})()

    def update(
        self, id, model_version
    ):  # noqa: A002  pylint: disable=redefined-builtin
        self.updates.append((id, model_version))
        title, _ = self._projects[id]
        self._projects[id] = (title, model_version)


def _per_dive(version="", dive=DIVE):
    return (f"2023 Some Dive #{dive} - HeadTail Labeling", version)


def _run(ls, tags, dive=DIVE, current=CURRENT):
    return asyncio.run(
        ensure_project_shows_predictions(LabelStudioClient(ls), dive, tags, current)
    )


def test_an_unset_project_is_pointed_at_the_tier_it_holds():
    ls = _LS({7: _per_dive("")})
    assert _run(ls, {7: Counter({CURRENT: 5})}) == 1
    assert ls.updates == [(7, CURRENT)]


def test_an_already_correct_project_is_left_alone():
    ls = _LS({7: _per_dive(CURRENT)})
    assert _run(ls, {7: Counter({CURRENT: 5})}) == 0
    assert not ls.updates


def test_an_all_fallback_dive_is_pinned_to_the_fallback_tier():
    """Pinning the current tag on a dive with no SAM 3.1 predictions points the
    project at a version it does not have, so every task stays blank."""
    ls = _LS({7: _per_dive("")})
    assert _run(ls, {7: Counter({FALLBACK: 40})}) == 1
    assert ls.updates == [(7, FALLBACK)]


def test_a_visible_fallback_tier_is_not_traded_for_a_smaller_current_one():
    """The regression: 40 visible fallback predictions must not be blanked to
    reveal 2 upgraded ones. Coverage wins; it flips on its own once the upgrade
    pass overtakes the old tier."""
    ls = _LS({7: _per_dive(FALLBACK)})
    assert _run(ls, {7: Counter({FALLBACK: 40, CURRENT: 2})}) == 0
    assert not ls.updates


def test_the_current_tier_wins_once_it_is_the_majority():
    ls = _LS({7: _per_dive(FALLBACK)})
    assert _run(ls, {7: Counter({FALLBACK: 10, CURRENT: 30})}) == 1
    assert ls.updates == [(7, CURRENT)]


def test_a_tie_breaks_toward_the_current_tier():
    ls = _LS({7: _per_dive("")})
    assert _run(ls, {7: Counter({FALLBACK: 5, CURRENT: 5})}) == 1
    assert ls.updates == [(7, CURRENT)]


def test_a_shared_project_is_never_touched():
    """`model_version` is project-global. On the grandfathered shared layout
    two dives at different tiers would overwrite each other every run, each
    write blanking the other dive's tasks."""
    ls = _LS({76: ("HeadTail Labeling (canonical)", "")})
    assert _run(ls, {76: Counter({CURRENT: 100})}) == 0
    assert not ls.updates


def test_another_dives_project_is_never_touched():
    ls = _LS({7: _per_dive("", dive=103)})
    assert _run(ls, {7: Counter({CURRENT: 5})}, dive=94) == 0
    assert not ls.updates


def test_a_dive_does_not_claim_a_project_whose_number_extends_its_own():
    """v2 change: v1 matched `#{dive_id}` as a substring, so dive 9 claimed
    dive 94's project (`#9` is in `#94`) and could repoint it at dive 9's
    tier. The number must end where the title's number ends."""
    ls = _LS({7: _per_dive("", dive=94)})
    assert _run(ls, {7: Counter({CURRENT: 5})}, dive=9) == 0
    assert not ls.updates


def test_a_failure_does_not_stop_the_other_projects():
    """The predictions are attached and correct either way; display config is
    not worth failing an activity that already did its work."""
    ls = _LS({7: _per_dive(""), 8: _per_dive("")})
    ls.raises_on = {7}
    assert _run(ls, {7: Counter({CURRENT: 1}), 8: Counter({CURRENT: 1})}) == 1
    assert ls.updates == [(8, CURRENT)]


def test_an_empty_count_is_skipped():
    ls = _LS({7: _per_dive("")})
    assert _run(ls, {7: Counter()}) == 0
    assert not ls.updates

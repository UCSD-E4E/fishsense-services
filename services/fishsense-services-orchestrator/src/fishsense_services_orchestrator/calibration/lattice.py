"""The checkerboard lattice study's Label Studio project: create, and import.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/
(create_checkerboard_lattice_label_studio_project_activity.py,
populate_checkerboard_lattice_label_studio_project_activity.py).

**Why a human is in this loop.** A uniformly mis-latticed detection -- every
corner two squares apart while the body points label them one apart -- is
still a perfect grid, so no residual can see it, and it scales every depth,
baseline and length. Five of fourteen checkerboard calibrations carried a
baseline that is a simple multiple of the consensus 10.4 cm. A person
looking at the drawn lattice can say which.

v1's rules, kept:

* **one project for the whole study**, not per dive: a per-dive project would
  tell the judge which calibration a frame came from, and the hypothesis
  under test predicts faults concentrate in five specific dives;
* the question is narrow and about spacing (`lattice_verdict` required;
  `lattice_fault` names the fault kind);
* **predictions go inline at import** (Label Studio shows only the version
  the project's `model_version` names, which an import sets for free when
  tasks carry predictions); keypoints are percentages of the rectified frame;
* **task data carries the image and nothing else**: every data key is a
  sortable column, and spacing, grid shape or a monotonic id would unblind
  the study. The checksum rides in the URL, so a verdict stays traceable;
* no label row is written (a no-op `record_label`): verdicts are read back
  off Label Studio; the import helper is still used for its dedup-by-URL and
  its async-import handling;
* the project is published only when the import completed (the study has no
  next firing to reconcile a partial one).

v2 changes: the project is titled per tenant -- v1's title for `lab`,
`"{title} ({slug})"` for any other, since every tenant shares one workspace
(port-plan) -- and found or created through `LabelProjects`, kind
`checkerboard_lattice`, no dive; a task's image is the render's `ObjectRef`.
"""

from __future__ import annotations

from typing import Any, Iterable, List

from temporalio import activity

from fishsense_services_contracts.slate_calibration import CheckerboardLatticeRender
from fishsense_services_orchestrator.calibration.contracts import (
    LatticeImport,
    LatticeProject,
)
from fishsense_services_orchestrator.labels.populate import (
    LS_PROJECT_TITLE_MAX,
    build_image_url,
    import_tasks_and_record_labels,
    publish_label_studio_project,
)

__all__ = [
    "CHECKERBOARD_LATTICE_LABELING_CONFIG_XML",
    "CHECKERBOARD_LATTICE_PROJECT_TITLE",
    "LATTICE_MODEL_VERSION",
    "LatticeProjectActivities",
    "build_lattice_task",
    "lattice_predictions",
    "lattice_project_title",
    "renders_worth_labeling",
]

CHECKERBOARD_LATTICE_PROJECT_TITLE = "Checkerboard Lattice Verification"

#: The tenant whose project keeps v1's bare title (every v1 row is `lab`'s).
_V1_TENANT = "lab"

CHECKERBOARD_LATTICE_LABELING_CONFIG_XML = """\
<View>

  <Header value="Does every marked point sit on a board corner, with none skipped?"/>

  <Image name="image" value="$image" zoom="true" zoomControl="true"
         brightnessControl="true" contrastControl="true"/>

  <KeyPointLabels name="lattice" toName="image" opacity="0.9" strokewidth="3">
    <Label value="Detected corner" background="#FF3B30"/>
  </KeyPointLabels>

  <Header value="Verdict"/>
  <Choices name="lattice_verdict" toName="image" choice="single" required="true" showInLine="true">
    <Choice value="Correct"/>
    <Choice value="Incorrect"/>
    <Choice value="Cannot tell"/>
  </Choices>

  <Header value="If incorrect, what is wrong?"/>
  <Choices name="lattice_fault" toName="image" choice="single">
    <Choice value="Skips corners - marks are 2 or more squares apart"/>
    <Choice value="Denser than the squares"/>
    <Choice value="Marks are not on corners at all"/>
    <Choice value="Different board from the E4E 14x10"/>
  </Choices>

</View>
"""

# Must match the labeling config's control names: a mismatch is silent -- the
# prediction is stored and nothing renders.
_KEYPOINT_FROM_NAME = "lattice"
_KEYPOINT_TO_NAME = "image"
_KEYPOINT_LABEL = "Detected corner"

#: One fixed value: there is only ever one detector behind this study.
LATTICE_MODEL_VERSION = "checkerboard-lattice-v1"


def lattice_project_title(tenant_slug: str) -> str:
    """v1's title for `lab`; `"{title} ({slug})"` otherwise, the slug cut to
    Label Studio's 50-character limit."""
    if tenant_slug == _V1_TENANT:
        return CHECKERBOARD_LATTICE_PROJECT_TITLE
    budget = LS_PROJECT_TITLE_MAX - len(CHECKERBOARD_LATTICE_PROJECT_TITLE) - 3
    return f"{CHECKERBOARD_LATTICE_PROJECT_TITLE} ({tenant_slug[:budget]})"


def _is_placeable(render: CheckerboardLatticeRender) -> bool:
    return (
        render.image is not None
        and bool(render.corners)
        and bool(render.width)
        and bool(render.height)
    )


def renders_worth_labeling(
    renders: Iterable[CheckerboardLatticeRender],
) -> List[CheckerboardLatticeRender]:
    """Drop frames the detector rejected, and frames rendered with nothing
    placeable: both reach a labeler as an image with no marks, whose only
    honest verdict is about the detector's hit rate, not the lattice."""
    return [r for r in renders if r.skip_reason is None and _is_placeable(r)]


def lattice_predictions(render: CheckerboardLatticeRender) -> list:
    """The Label Studio `predictions` for one render, or [] when unplaceable:
    one keypoint per detected corner, as a percentage of the frame."""
    if not _is_placeable(render):
        return []
    width = float(render.width)
    height = float(render.height)
    return [
        {
            "model_version": LATTICE_MODEL_VERSION,
            "result": [
                {
                    "from_name": _KEYPOINT_FROM_NAME,
                    "to_name": _KEYPOINT_TO_NAME,
                    "type": "keypointlabels",
                    "original_width": render.width,
                    "original_height": render.height,
                    "image_rotation": 0,
                    "value": {
                        "x": float(x) / width * 100,
                        "y": float(y) / height * 100,
                        "width": 0.3,
                        "keypointlabels": [_KEYPOINT_LABEL],
                    },
                }
                for x, y in render.corners
            ],
        }
    ]


def build_lattice_task(render: CheckerboardLatticeRender) -> dict:
    """One task: the overlay JPEG, and its corners as a prediction. The data
    carries the image and nothing else -- a blinding requirement."""
    url = build_image_url(render.image)
    return {
        "data": {"image": url, "img": url},
        "predictions": lattice_predictions(render),
    }


async def _record_nothing(_item: Any, _task_id: int) -> None:
    """No label row for this study -- verdicts are read off Label Studio."""
    return None


class LatticeProjectActivities:
    def __init__(self, *, label_projects, label_studio) -> None:
        self._projects = label_projects
        self._ls = label_studio

    @activity.defn(name="create_checkerboard_lattice_label_studio_project")
    async def create_checkerboard_lattice_label_studio_project(
        self, project: LatticeProject
    ) -> int:
        """The tenant's study project, found or created; its id."""
        title = lattice_project_title(project.tenant_slug)
        project_id = await self._projects.ensure_project(
            project.tenant_id,
            "checkerboard_lattice",
            title=title,
            labeling_config_xml=CHECKERBOARD_LATTICE_LABELING_CONFIG_XML,
            dive_id=None,
        )
        activity.logger.info(
            "lattice-verification LS project_id=%d title=%r", project_id, title
        )
        return project_id

    @activity.defn(name="populate_checkerboard_lattice_label_studio_project")
    async def populate_checkerboard_lattice_label_studio_project(
        self, payload: LatticeImport
    ) -> int:
        """Import the rendered lattices as tasks; the count imported."""
        worth = renders_worth_labeling(payload.renders)
        activity.logger.info(
            "lattice verification: %d of %d renders worth labeling",
            len(worth),
            len(payload.renders),
        )
        if not worth:
            return 0

        result = await import_tasks_and_record_labels(
            self._ls,
            project_id=payload.ls_project_id,
            tasks=[build_lattice_task(r) for r in worth],
            record_label=_record_nothing,
            items=worth,
        )
        if result.complete:
            await publish_label_studio_project(self._ls, payload.ls_project_id)
        else:
            activity.logger.error(
                "lattice verification: %d task(s) deferred by LS; leaving "
                "project_id=%d unpublished. Re-run to reconcile.",
                result.deferred,
                payload.ls_project_id,
            )
        return result.recorded

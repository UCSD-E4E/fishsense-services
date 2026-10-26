"""Every step the automatic chain runs through `_step` can record its failure
(2026-10-26: head/tail v5's `keypoints` step was not in the table's step check,
so mission 347's failure could not be recorded and its run failed instead)."""

import inspect
import re

from fishsense_services_api.automatic_results_store import AUTOMATIC_RESULTS_STEPS
from fishsense_services_orchestrator.automatic_results import workflow


def test_every_step_name_the_workflow_records_is_allowed():
    named = set(re.findall(r'_step\(target, "([a-z_]+)"', inspect.getsource(workflow)))
    assert named, "no _step calls found: the pattern is stale"
    assert named <= set(AUTOMATIC_RESULTS_STEPS), named - set(AUTOMATIC_RESULTS_STEPS)

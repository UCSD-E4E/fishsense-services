"""Every child the slate and calibration parents dispatch is served by the
processor, on the queue it is dispatched to, with the payload it is sent.

v1 pinned its queues in fishsense-lite@77e8f8e5 libs/fishsense-shared/
task_queues.py and the data-worker's role lists, for v1's reason: a child
dispatched by a name nothing registers, or onto a queue whose pod does not
serve it, is not an error -- it is accepted and sits `Running` until its
execution timeout, with nothing in either worker's logs. The orchestrator
dispatches by name; this reads the names and queues out of the parents'
source and checks them against the processor's registry.
"""

from __future__ import annotations

import inspect
import re
import typing

import pytest

from fishsense_services_contracts import (
    PROCESSOR_LIGHT_TASK_QUEUE,
    PROCESSOR_TASK_QUEUE,
)
from fishsense_services_contracts.slate_calibration import (
    PerformCheckerboardCalibrationInput,
    PreprocessSlateImagesInput,
    SlateCalibrationInput,
    VerifyCheckerboardLatticeInput,
)
from fishsense_services_orchestrator.calibration import workflows as calibration
from fishsense_services_orchestrator.slates import workflows as slates
from fishsense_services_processor.registry import (
    ROLE_TASK_QUEUES,
    registration_for_role,
)

#: child name -> (queue it is dispatched to, the payload type it is sent)
CHILDREN = {
    "PreprocessSlateImagesWorkflow": (PROCESSOR_TASK_QUEUE, PreprocessSlateImagesInput),
    "PerformLaserCalibrationWorkflow": (
        PROCESSOR_LIGHT_TASK_QUEUE,
        SlateCalibrationInput,
    ),
    "PerformCheckerboardCalibrationWorkflow": (
        PROCESSOR_TASK_QUEUE,
        PerformCheckerboardCalibrationInput,
    ),
    "VerifyCheckerboardLatticeWorkflow": (
        PROCESSOR_TASK_QUEUE,
        VerifyCheckerboardLatticeInput,
    ),
}


def _dispatched() -> dict[str, str]:
    """child name -> queue constant name, read from the parents' source."""
    found = {}
    for module in (slates, calibration):
        source = inspect.getsource(module)
        for name, queue in re.findall(
            r'execute_child_workflow\(\s*"([A-Za-z]+)",.*?task_queue=([A-Z_]+)',
            source,
            flags=re.S,
        ):
            found[name] = queue
    return found


def test_the_parents_dispatch_exactly_these_children():
    dispatched = _dispatched()

    assert set(dispatched) == set(CHILDREN)
    for name, queue in dispatched.items():
        assert globals()[queue] == CHILDREN[name][0], name


@pytest.mark.parametrize("name", sorted(CHILDREN))
def test_the_processor_serves_each_child_on_its_queue(name):
    queue, payload = CHILDREN[name]
    (role,) = [r for r, q in ROLE_TASK_QUEUES.items() if q == queue]
    served = {
        w.__temporal_workflow_definition.name: w
        for w in registration_for_role(role).workflows
    }

    assert name in served, f"{name} is not served on {queue}"
    hints = typing.get_type_hints(served[name].run)
    assert hints["payload"] is payload

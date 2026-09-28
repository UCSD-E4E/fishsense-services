"""The laser gate, the laser-label validator and remediation planning, as a
processor stage.

On the light role: rows in, numpy, rows out -- no image bytes -- so they must
not wait behind the per-image role's memory cap (v1 moved the gate and the
validator to the light queue on 2026-09-04, fishsense-lite@77e8f8e5 roles.py
`LIGHT_WORKFLOWS`).
"""

from fishsense_services_processor.laser_validation.activities import (
    evaluate_laser_auto_accept,
    plan_laser_supersede_remediation,
    validate_laser_labels_for_dive,
)
from fishsense_services_processor.laser_validation.workflows import (
    EvaluateLaserAutoAcceptWorkflow,
    PlanLaserSupersedeRemediationWorkflow,
    ValidateLaserLabelsForDiveWorkflow,
)
from fishsense_services_processor.registry import ROLE_LIGHT, Stage

STAGE = Stage(
    name="laser_validation",
    role=ROLE_LIGHT,
    workflows=[
        EvaluateLaserAutoAcceptWorkflow,
        ValidateLaserLabelsForDiveWorkflow,
        PlanLaserSupersedeRemediationWorkflow,
    ],
    activities=[
        evaluate_laser_auto_accept,
        validate_laser_labels_for_dive,
        plan_laser_supersede_remediation,
    ],
)

"""The phone measurement as a processor stage.

On the per-image role: each activity decodes a frame and runs the app's ONNX
segmentation (`fishial`, about 1.2 s a frame on CPU, 2026-11-09), so it runs
where concurrency is a memory ceiling, and needs no GPU or torch.
"""

from fishsense_services_processor.phone_measurement.activities import (
    PhoneMeasurementActivities,
)
from fishsense_services_processor.phone_measurement.workflow import (
    MeasurePhoneCapturesWorkflow,
)
from fishsense_services_processor.registry import ROLE_PER_IMAGE, Stage

_activities = PhoneMeasurementActivities()

STAGE = Stage(
    name="phone_measurement",
    role=ROLE_PER_IMAGE,
    workflows=[MeasurePhoneCapturesWorkflow],
    activities=[_activities.measure_phone_capture],
)

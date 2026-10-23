"""Calibration v3's replay payloads (v15): new models only."""

import uuid

from fishsense_services_contracts.automatic_results import FitAutomaticCalibrationInput
from fishsense_services_contracts.calibration_v3 import (
    CalibrationDraw,
    CalibrationV3Report,
    ReplayCalibrationInput,
)


def test_a_report_keeps_the_wire_name_and_its_draws():
    m = uuid.uuid4()
    r = CalibrationV3Report(mission_id=m, outcome="refused",
                            refusal_reason="too_few_frames",
                            draws=[CalibrationDraw(size=3, seed=0, outcome="accepted",
                                                   laser_axis=[0, 0, 1.0])])  # fmt: skip
    wire = r.model_dump(mode="json", by_alias=True)
    assert wire["dive_id"] == str(m) and wire["draws"][0]["size"] == 3
    assert CalibrationV3Report.model_validate(wire).mission_id == m


def test_a_replay_names_its_draws():
    payload = FitAutomaticCalibrationInput(tenant_id=uuid.uuid4(), mission_id=uuid.uuid4(),
                                           camera_matrix=[[1.0]], frames=[])  # fmt: skip
    r = ReplayCalibrationInput(payload=payload, draw_sizes=[3, 4], draws_per_size=20)
    assert (
        r.draw_sizes == [3, 4]
        and ReplayCalibrationInput(payload=payload).draws_per_size == 0
    )

"""The laser-colour backfill's payloads (v24, the owner, 2026-10-26).

Automatic dots predicted before contract v22 kept no colour. The backfill
reads each one's colour off the render it was predicted on, at the dot, with
fishsense-core's `classify_laser_color`, and records where it came from."""

import uuid

import pytest
from pydantic import ValidationError

from fishsense_services_contracts.laser_colour_backfill import (
    COLOUR_READING_SOURCE,
    COLOUR_READING_VERSION,
    ColourFrame,
    ColourFramesInput,
    ColourReading,
)


def test_a_request_carries_each_dot_and_its_render():
    m = uuid.uuid4()
    r = ColourFramesInput(tenant_id=uuid.uuid4(), mission_id=m, frames=[
        ColourFrame(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                    render_url="u", x=2066.3, y=1441.9)])  # fmt: skip
    wire = r.model_dump(mode="json", by_alias=True)

    assert wire["dive_id"] == str(m)
    back = ColourFramesInput.model_validate(wire).frames[0]
    assert (back.x, back.y) == (2066.3, 1441.9)


def test_a_reading_is_red_green_or_too_close_to_call():
    ids = {"prediction_id": uuid.uuid4(), "capture_id": uuid.uuid4()}
    red = ColourReading(**ids, status="read", color="red", margin=163.5)
    close = ColourReading(**ids, status="read", color=None, margin=2.0)
    gone = ColourReading(**ids, status="render_unavailable")

    assert (red.color, red.margin) == ("red", 163.5)
    assert (close.color, close.margin) == (None, 2.0)
    assert (gone.color, gone.margin) == (None, None)
    with pytest.raises(ValidationError):
        ColourReading(**ids, status="read", color="blue", margin=1.0)
    with pytest.raises(ValidationError):
        ColourReading(**ids, status="guessed")


def test_the_reading_names_its_source_and_version():
    assert COLOUR_READING_SOURCE == "render"
    assert COLOUR_READING_VERSION == 1

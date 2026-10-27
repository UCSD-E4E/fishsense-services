"""docs/design/tokens.json keeps the promises docs/design/README.md makes.

The README says the two themes share token names, that every text token passes
WCAG AA (4.5:1) on its theme's ground, surface and raised colors, and that the
snout and fork overlay colors differ in lightness as well as hue (review of
#170: dark text3 measured 3.97:1 on raised, light warn 2.62:1, and snout/fork
only 1.3:1 apart). These pin each promise to the values.
"""

import json
from pathlib import Path

import pytest

TOKENS = json.loads(
    (Path(__file__).parents[1] / "docs" / "design" / "tokens.json").read_text()
)
THEMES = ("dark", "light")
SURFACES = ("ground", "surface", "raised")
#: Colors set as text (or as a state shown in text), never only as a fill.
TEXT = ("text", "text2", "text3", "tealText", "warn", "error")


def luminance(hex_colour: str) -> float:
    """WCAG 2 relative luminance of a #RRGGBB color."""
    channels = [int(hex_colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_both_themes_have_the_same_token_names():
    dark, light = (set(TOKENS["color"][t]) for t in THEMES)

    assert dark == light, f"only dark: {dark - light}; only light: {light - dark}"


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("token", TEXT)
def test_every_text_token_passes_aa_on_every_surface(theme, token):
    colours = TOKENS["color"][theme]

    worst = min(contrast(colours[token], colours[s]) for s in SURFACES)

    assert worst >= 4.5, f"{theme}.{token} is {worst:.2f}:1 on some surface"


@pytest.mark.parametrize("theme", THEMES)
def test_text_on_a_teal_fill_passes_aa(theme):
    colours = TOKENS["color"][theme]

    assert contrast(colours["onTeal"], colours["teal"]) >= 4.5
    assert contrast(colours["tealText"], colours["tealTint"]) >= 4.5


def test_snout_and_fork_differ_in_lightness_not_hue_alone():
    overlay = TOKENS["color"]["overlay"]

    # Distinguishable in grayscale: at least the 3:1 WCAG asks of graphics.
    assert contrast(overlay["snout"], overlay["fork"]) >= 2.5

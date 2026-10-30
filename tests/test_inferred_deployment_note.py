"""The web recognizes the note ``infer-deployments`` leaves on each trip it makes.

Data's pages say how a deployment was made (docs/design/README.md: an inferred
trip never passes for a planned one); the web reads it from the note the API's
``deployment_inference.NOTE`` writes. The web cannot import the API, so this
keeps the web's pattern matching the API's note.
"""

import re
from pathlib import Path

from fishsense_services_api.deployment_inference import NOTE

WEB = Path(__file__).parents[1] / "apps" / "web" / "lib" / "data-view.ts"


def test_the_webs_pattern_matches_the_apis_note():
    source = WEB.read_text()
    pattern = re.search(r"const INFERRED = /(.*?)/;", source)
    assert pattern, "INFERRED not found in data-view.ts"

    assert re.match(pattern.group(1), f"{NOTE}: 6 sessions, 12 missions")

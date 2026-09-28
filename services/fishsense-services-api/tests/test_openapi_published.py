"""The web's typed client is generated from the API's OpenAPI document.

PLAN.md §3: clients are generated (``openapi-typescript`` for the web) and
drift-tested, so a spec and its clients can't silently diverge. The document
the web generates from is committed at ``apps/web/openapi.json``; this pins
that it is the API's current one. The web's own CI job pins the other half:
that ``lib/api/schema.d.ts`` is what ``openapi-typescript`` makes of it.

Regenerate with::

    uv run python -m fishsense_services_api.openapi > apps/web/openapi.json
    (cd apps/web && npm run api:generate)
"""

import json
from pathlib import Path

from fishsense_services_api.openapi import spec

PUBLISHED = Path(__file__).resolve().parents[3] / "apps" / "web" / "openapi.json"


def test_the_published_spec_is_the_apis():
    assert json.loads(PUBLISHED.read_text()) == spec()


def test_the_spec_is_built_without_a_database_or_an_identity_provider():
    """Generating clients (CI, a laptop) needs neither."""
    document = spec()

    assert document["info"]["title"] == "FishSense Services API"
    assert "/tenants/{slug}/dives" in document["paths"]

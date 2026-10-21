"""The web and the API agree on which tenant slugs are reserved.

The web serves each tenant under its slug (``/lab/fish``, docs/design/README.md),
so a slug that is also one of the web's top-level routes would be unreachable.
The API's ``add-tenant`` refuses them; the list lives in both packages (the API
image has no web code), and this keeps the two identical, and complete against
the web's ``app/`` directory.
"""

import re
from pathlib import Path

from fishsense_services_api.tenant_slugs import RESERVED_TENANT_SLUGS

ROOT = Path(__file__).parents[1]
WEB = ROOT / "apps" / "web"


def _web_list() -> set[str]:
    source = (WEB / "lib" / "tenant-paths.ts").read_text()
    body = re.search(r"RESERVED_SLUGS\s*=\s*\[(.*?)\]", source, re.S)
    assert body, "RESERVED_SLUGS not found in tenant-paths.ts"
    return set(re.findall(r'"([^"]+)"', body.group(1)))


def test_the_web_and_the_api_reserve_the_same_slugs():
    assert _web_list() == set(RESERVED_TENANT_SLUGS)


def test_every_top_level_route_is_reserved():
    routes = {
        p.name
        for p in (WEB / "app").iterdir()
        if p.is_dir() and not p.name.startswith(("[", "(", "_"))
    }
    public = {p.stem for p in (WEB / "public").iterdir() if not p.name.startswith(".")}

    assert routes - set(RESERVED_TENANT_SLUGS) == set()
    assert public - set(RESERVED_TENANT_SLUGS) == set()

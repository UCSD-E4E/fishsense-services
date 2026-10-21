"""Tenant slugs the web portal needs for itself (docs/design/README.md, Tenants).

The web serves each tenant under its slug (``/lab/fish``), so a tenant named
like one of the web's top-level routes, or a file it serves, would be
unreachable. ``add-tenant`` refuses these. The web keeps the same list in
``apps/web/lib/tenant-paths.ts`` (the API image has no web code), and
tests/test_reserved_tenant_slugs.py keeps the two identical.
"""

RESERVED_TENANT_SLUGS = frozenset(
    {
        # The web's top-level routes, and its static files.
        "api",
        "captures",
        "deployments",
        "fish",
        "missions",
        "portal",
        "favicon",
        "logo",
        "world",
        # Routes the design adds next.
        "admin",
        "map",
        "settings",
    }
)

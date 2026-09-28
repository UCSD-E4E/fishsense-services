"""The API's OpenAPI document, for generating clients (PLAN.md §3).

    uv run python -m fishsense_services_api.openapi > apps/web/openapi.json

Built from the app itself with an engine that never connects and a validator
that never validates, so neither a database nor an identity provider is needed.
"""

import json
import sys
from typing import Any

from sqlalchemy.ext.asyncio import create_async_engine

from fishsense_services_api.app import create_app
from fishsense_services_api.auth import StaticKeySource, TokenValidator


def spec() -> dict[str, Any]:
    app = create_app(
        engine=create_async_engine("postgresql+asyncpg://unused@127.0.0.1:1/none"),
        validator=TokenValidator(
            issuer="https://unused.invalid/",
            audiences=("unused",),
            keys=StaticKeySource({}),
        ),
    )
    return app.openapi()


def main() -> None:
    json.dump(spec(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

"""The API image installs the API package alone: no module of it may import
the contracts (or any other workspace package).

The uv workspace makes every package importable in tests, so an import that
the image cannot satisfy passes every unit test and crashes the deployed CLI
(`migrate` first) or server at startup -- #151's seed did, caught by the
end-to-end check. Values the API shares with the contracts are repeated in the
API and pinned by a test, as `laser_store.LASER_PREDICTOR_VERSION` is.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "services/fishsense-services-api/src/fishsense_services_api"
#: Workspace packages the API image does not install.
NOT_IN_THE_IMAGE = (
    "fishsense_services_contracts",
    "fishsense_services_orchestrator",
    "fishsense_services_processor",
)


def _imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_no_api_module_imports_a_package_its_image_lacks():
    offenders = sorted(
        f"{path.relative_to(ROOT)}: {name}"
        for path in API.rglob("*.py")
        for name in _imports(path)
        if name.split(".")[0] in NOT_IN_THE_IMAGE
    )
    assert offenders == []

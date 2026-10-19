"""Every name a module's `__all__` exports exists.

Phase 2a renamed dives to missions across the code; an `__all__` still naming
the old object would make `from module import *` raise, and hide what a module
offers. Skipped: entry points (`__main__`), migrations (Alembic runs them) and
modules needing an optional model.
"""

import importlib
import pkgutil

import pytest

PACKAGES = (
    "fishsense_services_api",
    "fishsense_services_contracts",
    "fishsense_services_orchestrator",
    "fishsense_services_processor",
)


def _modules():
    for package in PACKAGES:
        root = importlib.import_module(package)
        yield root
        for info in pkgutil.walk_packages(root.__path__, package + "."):
            if info.name.endswith("__main__") or ".migrations" in info.name:
                continue
            try:
                yield importlib.import_module(info.name)
            except ModuleNotFoundError:
                continue


@pytest.mark.parametrize("module", list(_modules()), ids=lambda m: m.__name__)
def test_every_exported_name_exists(module):
    missing = [n for n in getattr(module, "__all__", ()) if not hasattr(module, n)]

    assert not missing, f"{module.__name__}.__all__ names {missing}"

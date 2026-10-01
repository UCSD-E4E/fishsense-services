"""One package owns the database: fishsense-services-api.

Every table, migration and SQL statement lives in the API package, and the
row-level security that keeps tenants apart is enforced there (PLAN.md §4.5,
"Database ownership"). The orchestrator runs that package's catalogs as a
member of the tenants it serves, and may open the connection they run on --
nothing more. The processor and the contracts never touch the database.

So outside the API package: no database driver, no SQLAlchemy beyond
`create_async_engine`, and no SQL in string literals. A query that is needed
elsewhere is added to the API package and called from there.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Every package that is not the database's owner.
NOT_OWNERS = [
    ROOT / "services/fishsense-services-orchestrator/src",
    ROOT / "services/fishsense-services-processor/src",
    ROOT / "services/fishsense-services-contracts/src",
]

DRIVERS = ("asyncpg", "psycopg", "psycopg2", "alembic")

#: Opening the connection the API's catalogs run on is the one thing a
#: non-owner may do with SQLAlchemy.
ALLOWED_SQLALCHEMY = {("sqlalchemy.ext.asyncio", "create_async_engine")}

SQL = re.compile(
    r"\bSELECT\b[\s\S]*\bFROM\b"
    r"|\bINSERT\s+INTO\b"
    r"|\bUPDATE\s+\w+\s+SET\b"
    r"|\bDELETE\s+FROM\b"
    r"|\b(CREATE|ALTER|DROP)\s+(TABLE|VIEW|ROLE|POLICY|FUNCTION)\b"
)
# Case-sensitive on purpose: this repo writes SQL keywords in capitals, and
# prose ("select the oldest dive from ...") is not a query.


def _sources():
    for root in NOT_OWNERS:
        yield from sorted(root.rglob("*.py"))


def violations(path: Path) -> list[str]:
    """What `path` does that only the database's owner may."""
    found = []
    tree = ast.parse(path.read_text(), filename=str(path))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in DRIVERS or top == "sqlalchemy":
                    found.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            if top in DRIVERS:
                found.append(f"line {node.lineno}: from {node.module} import ...")
            elif top == "sqlalchemy":
                for alias in node.names:
                    if (node.module, alias.name) not in ALLOWED_SQLALCHEMY:
                        found.append(
                            f"line {node.lineno}: from {node.module} import {alias.name}"
                        )
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            if SQL.search(node.value):
                found.append(f"line {node.lineno}: SQL {node.value[:60]!r}")
    return found


@pytest.mark.parametrize(
    "path", list(_sources()), ids=lambda p: str(p.relative_to(ROOT))
)
def test_only_the_api_package_touches_the_database(path):
    assert violations(path) == []


def test_the_rule_sees_a_query(tmp_path):
    """The check itself: each kind of breach is caught, the allowance is not."""
    breach = tmp_path / "breach.py"
    breach.write_text(
        '"""Select the oldest dive from the cohort (prose, not a query)."""\n'
        "from sqlalchemy import text\n"
        "import asyncpg\n"
        "Q = 'SELECT id FROM tenants'\n"
        "from sqlalchemy.ext.asyncio import create_async_engine\n"
    )
    assert len(violations(breach)) == 3


def test_every_non_owner_package_is_scanned():
    assert all(root.is_dir() for root in NOT_OWNERS)
    assert len(list(_sources())) > 50

"""Superset's dashboards-as-code, moved from v1 onto v2's database.

v1's bundle (fishsense-lite deploy/incus/superset_volumes/docker/assets) is
re-imported on every converge by docker-init.sh, overwriting by uuid. v2 keeps
the uuids, so the import updates v1's objects in place rather than duplicating
them, and points the one database connection at v2's database, as the analytics
login. The virtual datasets' SQL is deploy/superset/datasets/*.sql, which the
API's suite runs against v2 (tests/test_superset_datasets.py): the bundle must
carry exactly that SQL.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from _deploy import INCUS, REPO

ASSETS = INCUS / "superset_volumes" / "docker" / "assets"
DATASET_SQL = REPO / "deploy" / "superset" / "datasets"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _strip_comment(sql: str) -> str:
    """The .sql files open with a provenance comment the bundle doesn't carry."""
    body = sql.split("*/", 1)[1] if sql.lstrip().startswith("/*") else sql
    return body.strip()


def test_the_connection_is_v2s_database_as_the_analytics_login():
    database = _load(ASSETS / "databases" / "FishSense.yaml")

    uri = database["sqlalchemy_uri"]
    assert uri == (
        "postgresql+psycopg2://fishsense_superset:__ANALYTICS_DB_PASSWORD__"
        "@postgres:5432/fishsense_services"
    )
    assert database["allow_dml"] is False
    # v1's uuid: the import repoints v1's connection instead of adding a second
    # one named FishSense (database names are unique).
    assert database["uuid"] == "11111111-1111-4111-8111-111111111111"


def test_init_injects_the_analytics_password_not_the_metadata_one():
    init = (INCUS / "superset_volumes" / "docker" / "docker-init.sh").read_text()
    assert "__ANALYTICS_DB_PASSWORD__|${ANALYTICS_DATABASE_PASSWORD}" in init


def test_the_virtual_datasets_carry_the_tested_sql():
    for sql_file in sorted(DATASET_SQL.glob("*.sql")):
        dataset = _load(ASSETS / "datasets" / "FishSense" / f"{sql_file.stem}.yaml")
        assert dataset["sql"].strip() == _strip_comment(sql_file.read_text()), sql_file


def test_every_reference_resolves():
    datasets = {
        _load(p)["uuid"]: _load(p)
        for p in (ASSETS / "datasets" / "FishSense").glob("*.yaml")
    }
    charts = {_load(p)["uuid"]: _load(p) for p in (ASSETS / "charts").glob("*.yaml")}
    database = _load(ASSETS / "databases" / "FishSense.yaml")["uuid"]

    for dataset in datasets.values():
        assert dataset["database_uuid"] == database, dataset["table_name"]
    for chart in charts.values():
        assert chart["dataset_uuid"] in datasets, chart["slice_name"]
    for path in (ASSETS / "dashboards").glob("*.yaml"):
        referenced = {
            node["meta"]["uuid"]
            for node in _load(path)["position"].values()
            if isinstance(node, dict) and node.get("type") == "CHART"
        }
        assert referenced and referenced <= set(charts), path.name


def test_the_fish_measurements_dataset_reads_the_research_views():
    """v1's SQL joined v1's tables by name; v2 keeps them as the `v1` views
    (migration 0031), schema-qualified here so no search_path is needed."""
    sql = _load(ASSETS / "datasets" / "FishSense" / "fish_measurements.yaml")["sql"]
    for table in ("measurement", "fish", "image", "dive", "species"):
        assert f"v1.{table} " in sql, table

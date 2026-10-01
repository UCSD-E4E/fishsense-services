"""The post-converge smoke test (PLAN.md §6.6 step 6): GO or NO-GO.

New in v2. v1's deploy had no such gate: `deploy.yml`'s green meant "the
converge fired", and v1's verify job only compared image pins (fishsense-lite
deploy.yml `verify-incus`). The cutover needs more than that before the portal
reopens -- the schema at head, the tenancy audit clean, the lab's data readable
the way the research repos read it, the schedules registered on the shared
Temporal, and every external dependency reachable -- and it needs one answer.

Every check runs against fakes here: the live probes are the thin boundary
(`LiveProbes`) the operator runs on the slot (docs/cutover.md).
"""

from __future__ import annotations

import uuid

import pytest

from fishsense_services_api.migrations import head_revision
from fishsense_services_orchestrator.ops import smoke as sut

LAB = uuid.UUID("00000000-0000-4000-8000-000000000001")


class FakeProbes:
    """A healthy slot; each test breaks one thing."""

    def __init__(self, **overrides):
        self.http = {
            "http://api:8000/healthz": (200, b'{"status":"ok"}'),
            "http://api:8000/openapi.json": (
                200,
                b'{"openapi":"3.1.0","paths":{"/healthz":{}}}',
            ),
            "http://web:3000/": (200, b"<html>FishSense</html>"),
        }
        self.revision = head_revision()
        self.violations: list[str] = []
        self.lab = LAB
        self.measurements = {490: 12}
        self.schedules = set(sut.expected_schedule_ids())
        self.workspace = 7
        self.sample = "fishsense-lite/laser_jpeg/0123abcd.JPG"
        self.calls: list[str] = []
        for name, value in overrides.items():
            setattr(self, name, value)

    async def http_get(self, url):
        self.calls.append(url)
        if url not in self.http:
            raise ConnectionError(f"connection refused: {url}")
        return self.http[url]

    async def migration_revision(self):
        return self.revision

    async def tenancy_violations(self):
        return self.violations

    async def lab_tenant_id(self):
        return self.lab

    async def research_measurement_count(self, dive_number):
        if isinstance(self.measurements, Exception):
            raise self.measurements
        return self.measurements.get(dive_number, 0)

    async def temporal_schedule_ids(self):
        if isinstance(self.schedules, Exception):
            raise self.schedules
        return self.schedules

    async def label_studio_workspace_id(self, name):
        if isinstance(self.workspace, Exception):
            raise self.workspace
        return self.workspace

    async def object_store_sample(self):
        if isinstance(self.sample, Exception):
            raise self.sample
        return self.sample


def _settings(**overrides):
    values = dict(
        api_url="http://api:8000",
        web_url="http://web:3000",
        label_studio_workspace="FishSense",
        dive_number=490,
        min_measurements=1,
    )
    values.update(overrides)
    return sut.SmokeOptions(**values)


async def _run(probes, **overrides):
    return await sut.run_checks(probes, _settings(**overrides))


def _failed(results):
    return {r.name for r in results if not r.ok}


# --- GO -------------------------------------------------------------------------


async def test_a_healthy_slot_is_go_and_every_check_is_reported():
    results = await _run(FakeProbes())

    assert _failed(results) == set()
    assert [r.name for r in results] == list(sut.CHECK_NAMES)
    assert sut.verdict(results) == 0


def test_the_checks_are_the_runbooks():
    """docs/cutover.md step 6 names these; each must stay a check."""
    assert sut.CHECK_NAMES == (
        "api healthz",
        "api openapi",
        "db at migration head",
        "tenancy audit",
        "lab tenant",
        "research reads the dive's measurements",
        "temporal schedules",
        "label studio",
        "web",
        "object store",
    )


# --- NO-GO, one broken thing at a time --------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "check"),
    [
        (
            {"http": {"http://web:3000/": (200, b""), "http://api:8000/openapi.json": (200, b"{}")}},  # fmt: skip
            "api healthz",
        ),
        ({"revision": "0033"}, "db at migration head"),
        ({"revision": None}, "db at migration head"),
        ({"violations": ["dives: no tenant policy"]}, "tenancy audit"),
        ({"lab": None}, "lab tenant"),
        ({"measurements": {490: 0}}, "research reads the dive's measurements"),
        (
            {"measurements": PermissionError("permission denied for view measurement")},
            "research reads the dive's measurements",
        ),
        ({"schedules": set()}, "temporal schedules"),
        ({"schedules": ConnectionError("CertificateExpired")}, "temporal schedules"),
        ({"workspace": None}, "label studio"),
        ({"workspace": PermissionError("401")}, "label studio"),
        ({"sample": None}, "object store"),
        ({"sample": PermissionError("AccessDenied")}, "object store"),
    ],
)
async def test_each_broken_dependency_is_no_go_and_named(overrides, check):
    results = await _run(FakeProbes(**overrides))

    assert check in _failed(results)
    assert sut.verdict(results) == 1


async def test_a_page_that_isnt_200_fails():
    probes = FakeProbes()
    probes.http["http://web:3000/"] = (502, b"Bad Gateway")

    assert _failed(await _run(probes)) == {"web"}


async def test_an_openapi_document_without_paths_fails():
    probes = FakeProbes()
    probes.http["http://api:8000/openapi.json"] = (200, b'{"openapi":"3.1.0"}')

    assert _failed(await _run(probes)) == {"api openapi"}


async def test_a_dive_with_fewer_measurements_than_expected_fails():
    results = await _run(FakeProbes(), min_measurements=13)

    failed = [r for r in results if not r.ok]
    assert [r.name for r in failed] == ["research reads the dive's measurements"]
    assert "12" in failed[0].detail and "13" in failed[0].detail


async def test_no_research_login_is_a_failure_not_a_skip():
    """Unconfigured is not a pass: the research repos are production consumers
    (PLAN.md §2.7) and the check is the only thing that reads as they do."""
    probes = FakeProbes(measurements=sut.NotConfigured("no research login"))

    results = await _run(probes)

    assert "research reads the dive's measurements" in _failed(results)


async def test_a_missing_schedule_is_named():
    expected = set(sut.expected_schedule_ids())
    missing = sorted(expected)[0]

    results = await _run(FakeProbes(schedules=expected - {missing}))

    (failure,) = [r for r in results if not r.ok]
    assert missing in failure.detail


async def test_v1s_schedules_still_present_are_no_go():
    """Step 5 deletes v1's schedules. Left in place (they were only paused),
    one un-paused by mistake would run v1's pipeline against the archive
    beside v2's -- and, sharing workflow class names and minute offsets, its
    scheduled runs collide with v2's ids."""
    probes = FakeProbes()
    probes.schedules = set(probes.schedules) | {"measure-fish-workflow-schedule"}

    results = await _run(probes)

    (failure,) = [r for r in results if not r.ok]
    assert failure.name == "temporal schedules"
    assert "measure-fish-workflow-schedule" in failure.detail


def test_the_backup_schedule_is_expected_too():
    assert "backup-databases" in sut.expected_schedule_ids()


async def test_one_check_raising_does_not_stop_the_others():
    probes = FakeProbes(schedules=RuntimeError("boom"), workspace=RuntimeError("boom"))

    results = await _run(probes)

    assert len(results) == len(sut.CHECK_NAMES)
    assert _failed(results) == {"temporal schedules", "label studio"}


async def test_a_hung_check_times_out_rather_than_hanging_the_gate():
    class Hung(FakeProbes):
        async def temporal_schedule_ids(self):
            import asyncio

            await asyncio.sleep(3600)

    results = await sut.run_checks(Hung(), _settings(), timeout_seconds=0.05)

    assert _failed(results) == {"temporal schedules"}


# --- the report -------------------------------------------------------------------


async def test_the_report_says_go_or_no_go_on_its_last_line(capsys):
    results = await _run(FakeProbes(lab=None))

    sut.print_report(results)

    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1].startswith("NO-GO")
    assert any(line.startswith("FAIL") and "lab tenant" in line for line in out)
    assert sum(line.startswith("PASS") for line in out) == len(sut.CHECK_NAMES) - 1


# --- the command line -------------------------------------------------------------


def test_the_dive_is_required():
    """A smoke test with no known dive proves nothing about the data."""
    with pytest.raises(SystemExit):
        sut.parse_args([])


def test_the_command_line_names_the_dive_and_the_floor():
    args = sut.parse_args(["--dive", "490", "--min-measurements", "5"])

    assert (args.dive, args.min_measurements) == (490, 5)


def test_main_exits_with_the_verdict(monkeypatch):
    monkeypatch.setattr(sut, "_live_probes", lambda: FakeProbes(lab=None))
    monkeypatch.setattr(sut, "_options", lambda args: _settings(dive_number=args.dive))

    assert sut.main(["--dive", "490"]) == 1


def test_main_is_go_on_a_healthy_slot(monkeypatch):
    monkeypatch.setattr(sut, "_live_probes", lambda: FakeProbes())
    monkeypatch.setattr(sut, "_options", lambda args: _settings(dive_number=args.dive))

    assert sut.main(["--dive", "490"]) == 0


def test_the_settings_default_to_the_interior_hosts(monkeypatch):
    """Run on the slot, inside the compose network: the API and the web by
    their service names, not through the public edge (whose failure is the
    edge's, and would read as ours)."""
    for name in ("API_URL", "WEB_URL", "RESEARCH_DATABASE_URL"):
        monkeypatch.delenv(f"FISHSENSE_SMOKE_{name}", raising=False)

    settings = sut.SmokeSettings()

    assert (settings.api_url, settings.web_url) == (
        "http://api:8000",
        "http://web:3000",
    )
    assert settings.research_database_url is None

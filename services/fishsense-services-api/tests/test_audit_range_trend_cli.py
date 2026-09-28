"""``fishsense-services-api audit-range-trend``: v1's range-trend audit script.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/scripts/audit_length_range_trend.py: per dive number, the
range trend of each rigid object, one line per object in v1's report format.
Read-only; runs as whatever role `FISHSENSE_DATABASE_URL` names, under the
tenant's RLS scope.

v2 changes: dives are named by number, within a tenant (`--tenant`, a slug or
an id: the app role sees a tenant only by id, so a slug needs a role that can
read `tenants`); the "insufficient" line quotes the thresholds the run used
(v1 quoted its defaults, even when overridden).
"""

import numpy as np
import pytest
from sqlalchemy import text

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    calibrated_dive,
    capture,
    depth,
    fish,
    forget_identities,
    laser_label,
    measurement,
    species_label,
    tenant,
)
from fishsense_services_api.cli import build_parser, format_range_trend_report, main
from fishsense_services_api.range_trend import RangeTrend

TREND = RangeTrend(
    n=24,
    depth_range_m=(0.91, 3.02),
    slope_pct_per_m=-13.3,
    ci_pct_per_m=(-15.1, -11.2),
    eps_deg=-0.781,
    flagged=True,
    note="length falls with range: in-plane calibration error ~-0.78 deg "
    "(rotated axis)",
)


# -- the report (v1's format) ----------------------------------------------------


def test_report_prints_one_line_per_object_in_v1s_format():
    lines = format_range_trend_report(
        490, {"Box": None, "Weasly Fish": TREND}, min_frames=8, min_range_ratio=2.0
    ).splitlines()

    assert lines == [
        "",
        "=== dive 490 ===",
        "  object             n   range (m)  slope %/m            95% CI    angle  note",
        "  Box              insufficient (need >= 8 frames beyond 0.8 m spanning"
        " >= 2.0x)",
        "  Weasly Fish       24 0.91-3.02     -13.30 [-15.10,-11.20]  -0.781d  "
        "FLAG length falls with range: in-plane calibration error ~-0.78 deg "
        "(rotated axis)",
    ]


def test_an_unflagged_object_carries_no_flag():
    trend = RangeTrend(24, (0.9, 3.0), 0.5, (-0.4, 1.3), 0.03, False, "")
    line = format_range_trend_report(
        60, {"Box": trend}, min_frames=8, min_range_ratio=2.0
    ).splitlines()[-1]
    assert line.endswith("+0.030d       ")


def test_a_dive_with_nothing_measured_says_so():
    assert format_range_trend_report(
        7, {}, min_frames=8, min_range_ratio=2.0
    ).splitlines() == ["", "=== dive 7 ===", "  no measured rigid objects"]


def test_insufficient_quotes_the_thresholds_the_run_used():
    report = format_range_trend_report(
        7, {"Box": None}, min_frames=5, min_range_ratio=1.5
    )
    assert "need >= 5 frames beyond 0.8 m spanning >= 1.5x" in report


# -- the command line --------------------------------------------------------------


def test_the_subcommand_parses_v1s_arguments():
    args = build_parser().parse_args(
        ["audit-range-trend", "--tenant", "lab", "490", "491", "492"]
    )
    assert args.command == "audit-range-trend"
    assert args.tenant == "lab"
    assert args.dive_numbers == [490, 491, 492]
    assert (args.min_frames, args.min_range_ratio) == (8, 2.0)

    args = build_parser().parse_args(
        [
            "audit-range-trend",
            "--tenant",
            "lab",
            "490",
            "--min-frames",
            "5",
            "--min-range-ratio",
            "1.5",
        ]
    )
    assert (args.min_frames, args.min_range_ratio) == (5, 1.5)


@pytest.mark.parametrize(
    "argv",
    [
        ["audit-range-trend", "490"],  # no tenant
        ["audit-range-trend", "--tenant", "lab"],  # no dive
        ["audit-range-trend", "--tenant", "lab", "dive490"],
    ],
)
def test_the_subcommand_refuses_incomplete_arguments(argv):
    with pytest.raises(SystemExit) as exit_:
        build_parser().parse_args(argv)
    assert exit_.value.code == 2


async def test_without_a_database_url_it_fails_naming_it(monkeypatch, capsys):
    monkeypatch.delenv("FISHSENSE_DATABASE_URL", raising=False)

    assert await main(["audit-range-trend", "--tenant", "lab", "490"]) == 2
    assert "FISHSENSE_DATABASE_URL" in capsys.readouterr().err


# -- end to end, on Postgres -------------------------------------------------------


async def _dive_number(owner_engine, dive_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT number FROM dives WHERE id = :d"), {"d": dive_id}
            )
        ).scalar_one()


async def _rotated_dive(owner_engine, lab):
    """A 10 cm baseline and a Weasly Fish read at 20 ranges under a -0.25 deg
    in-plane error: length (1 + eps z / b), v1's model."""
    dive_id, calibration = await calibrated_dive(owner_engine, lab)
    weasly = await fish(owner_engine, lab, model="Weasly Fish")
    eps = np.radians(-0.25)
    for z in np.linspace(0.9, 3.0, 20):
        capture_id = await capture(owner_engine, lab, dive_id)
        laser = await laser_label(owner_engine, lab, capture_id)
        await species_label(owner_engine, lab, capture_id, "Fish Model, Weasly Fish")
        await depth(owner_engine, lab, capture_id, laser, calibration, depth_m=z)
        await measurement(
            owner_engine,
            lab,
            capture_id,
            weasly,
            calibration,
            length_m=float(0.31 * (1 + eps * z / 0.1)),
            laser_label_id=laser,
        )
    return await _dive_number(owner_engine, dive_id)


async def test_audits_a_dive_as_the_app_role_by_tenant_id(
    owner_engine, app_url, monkeypatch, capsys
):
    lab = await tenant(owner_engine)
    number = await _rotated_dive(owner_engine, lab)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)

    code = await main(["audit-range-trend", "--tenant", str(lab), str(number)])

    out = capsys.readouterr().out
    assert code == 0
    assert f"=== dive {number} ===" in out
    (line,) = [ln for ln in out.splitlines() if "Weasly Fish" in ln]
    assert line.split()[2:4] == ["20", "0.90-3.00"]
    assert "-0.250d" in line and "FLAG" in line


async def test_a_slug_resolves_for_a_role_that_can_read_tenants(
    owner_engine, owner_url, monkeypatch, capsys
):
    lab = await tenant(owner_engine)
    number = await _rotated_dive(owner_engine, lab)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", owner_url)

    assert await main(["audit-range-trend", "--tenant", "lab", str(number)]) == 0
    assert "-0.250d" in capsys.readouterr().out


async def test_an_unresolvable_tenant_or_dive_is_named(
    owner_engine, app_url, monkeypatch, capsys
):
    lab = await tenant(owner_engine)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)

    # the app role sees no tenant by slug
    assert await main(["audit-range-trend", "--tenant", "lab", "1"]) == 1
    assert "lab" in capsys.readouterr().err

    assert await main(["audit-range-trend", "--tenant", str(lab), "999999"]) == 1
    assert "dive 999999" in capsys.readouterr().err

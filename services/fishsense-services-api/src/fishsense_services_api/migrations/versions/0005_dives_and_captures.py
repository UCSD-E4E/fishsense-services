"""Dives and captures -- the core of the Lite path -- and device kinds.

Same-tenant references are **composite foreign keys** on (tenant_id, …), so the
database itself refuses a row that points into another tenant. References
within a tenant are NO ACTION (checked at statement end): a dive with captures
can't be deleted, yet deleting a tenant cascades through all of it in one
statement -- RESTRICT, checked immediately, would block that.

Calibration refusals are *not* dive columns here: they belong to the laser
calibration entity (PLAN.md §4.3).

Revision ID: 0005
Revises: 0004
"""

from alembic import context, op

revision = "0005"
down_revision = "0004"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
DEVICE_KINDS = ("lite", "lite_flatport", "mobile", "multilens", "mono", "scout")


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _tenant_scoped(table: str, app_role: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {app_role}")


class UnmappableDeviceKinds(Exception):
    """Rows written before kinds were checked that 0005 can't map honestly."""


def _normalize_device_kinds(kinds: str) -> None:
    """Before 0005 any kind was accepted. Fold case/whitespace variants of known
    kinds (' LITE ' -> 'lite'); refuse anything else, by name, rather than invent
    a kind. The migration runs in one transaction, so a refusal changes nothing.
    """
    op.execute(f"""
        UPDATE devices SET kind = lower(btrim(kind))
        WHERE kind <> lower(btrim(kind)) AND lower(btrim(kind)) IN ({kinds})
        """)
    unmappable = (
        op.get_bind()
        .exec_driver_sql(
            f"SELECT kind, count(*) FROM devices WHERE kind NOT IN ({kinds}) "
            "GROUP BY kind ORDER BY kind"
        )
        .all()
    )
    if unmappable:
        found = ", ".join(f"{kind!r} ({n} rows)" for kind, n in unmappable)
        raise UnmappableDeviceKinds(
            f"devices have kinds 0005 can't map: {found}. Known kinds: "
            f"{', '.join(DEVICE_KINDS)}. Correct or delete those rows, then re-run "
            "migrate."
        )


def upgrade() -> None:
    app_role = _app_role()
    kinds = ", ".join(f"'{k}'" for k in DEVICE_KINDS)

    _normalize_device_kinds(kinds)
    op.execute(f"""
        ALTER TABLE devices
            ADD COLUMN v1_id bigint UNIQUE,
            ADD CONSTRAINT devices_kind_check CHECK (kind IN ({kinds})),
            ADD CONSTRAINT devices_tenant_id_id_key UNIQUE (tenant_id, id)
        """)

    op.execute("""
        CREATE TABLE dives (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            v1_id bigint UNIQUE,
            name text,
            source_path text NOT NULL,
            dived_at timestamptz NOT NULL,
            priority text NOT NULL DEFAULT 'low'
                CONSTRAINT dives_priority_check
                CHECK (priority IN ('low', 'high', 'none')),
            notes text,
            flip_dive_slate boolean NOT NULL DEFAULT false,
            device_id uuid,
            slate_template_id uuid REFERENCES slate_templates (id),
            calibration_target_id uuid REFERENCES calibration_targets (id),
            calibration_source_dive_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, source_path),
            FOREIGN KEY (tenant_id, device_id) REFERENCES devices (tenant_id, id),
            FOREIGN KEY (tenant_id, calibration_source_dive_id)
                REFERENCES dives (tenant_id, id),
            CONSTRAINT dives_calibration_not_self_check
                CHECK (calibration_source_dive_id <> id)
        )
        """)

    op.execute("""
        CREATE TABLE captures (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            v1_id bigint UNIQUE,
            dive_id uuid,
            device_id uuid,
            source_path text,
            raw_object_key text,
            captured_at timestamptz NOT NULL,
            checksum text NOT NULL,
            checksum_algorithm text NOT NULL DEFAULT 'md5'
                CONSTRAINT captures_checksum_algorithm_check
                CHECK (checksum_algorithm IN ('md5', 'sha256')),
            is_canonical boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, source_path),
            FOREIGN KEY (tenant_id, dive_id) REFERENCES dives (tenant_id, id),
            FOREIGN KEY (tenant_id, device_id) REFERENCES devices (tenant_id, id),
            CONSTRAINT captures_located_check
                CHECK (source_path IS NOT NULL OR raw_object_key IS NOT NULL),
            CONSTRAINT captures_md5_format_check
                CHECK (checksum_algorithm <> 'md5' OR checksum ~ '^[0-9a-f]{32}$')
        )
        """)
    # One canonical copy of a frame per tenant; duplicates under other dives
    # (half of v1's rows) are kept but never canonical.
    op.execute("""
        CREATE UNIQUE INDEX captures_canonical_checksum_key
            ON captures (tenant_id, checksum_algorithm, checksum)
            WHERE is_canonical
        """)

    for table in ("dives", "captures"):
        _tenant_scoped(table, app_role)


def downgrade() -> None:
    op.execute("DROP TABLE captures")
    op.execute("DROP TABLE dives")
    op.execute("""
        ALTER TABLE devices
            DROP CONSTRAINT devices_tenant_id_id_key,
            DROP CONSTRAINT devices_kind_check,
            DROP COLUMN v1_id
        """)

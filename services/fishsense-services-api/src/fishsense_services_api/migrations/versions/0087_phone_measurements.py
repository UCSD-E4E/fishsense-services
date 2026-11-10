"""Phone captures measured on the server (2026-11-09).

The owner's rule (2026-10-21): the server measures a phone capture that was
never measured, replicating FishSense Mobile (its `compute_length`), and a
measurement the phone synced counts. The owner, 2026-11-09: the server's
result is unvalidated, shown as such and reviewed before it counts. So it is
its own table, not a `measurements` row: it can't reach
`current_measurements`, the observations or a public total.

One row per run on a capture: the status (``measured``, or why not), the
length, snout and fork (frame px) and each point's depth, the segmentation
weights (`model_id`), fishsense-core's version and the app commit the
sequence was ported from (`algorithm`), and the mask the processor stored,
named in the row (the tenant's bucket, ``masks/phone/<capture>/...``).

Append-only (SELECT, INSERT), tenant-scoped under forced RLS.
``current_phone_measurements`` is each capture's latest row.

Revision ID: 0087
Revises: 0086
"""

from alembic import context, op

revision = "0087"
down_revision = "0086"

TABLE = "phone_measurements"
ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
STATUSES = ("measured", "no_fish", "headtail_failed", "depth_failed",
            "image_unavailable", "decode_failed")  # fmt: skip
#: The statuses that segmented a fish, and so may name a mask.
MASK_STATUSES = ("measured", "headtail_failed", "depth_failed")


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _values(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    app_role = _app_role()
    op.execute(f"""
        CREATE TABLE {TABLE} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            capture_id uuid NOT NULL,
            status text NOT NULL
                CONSTRAINT {TABLE}_status_check CHECK (status IN ({_values(STATUSES)})),
            length_m double precision CONSTRAINT {TABLE}_length_check CHECK (length_m > 0),
            snout_x double precision,
            snout_y double precision,
            fork_x double precision,
            fork_y double precision,
            snout_depth_m double precision,
            fork_depth_m double precision,
            mask_bucket text,
            mask_key text,
            mask_sha256 text CONSTRAINT {TABLE}_mask_sha256_check
                CHECK (mask_sha256 ~ '^[0-9a-f]{{64}}$'),
            mask_size_bytes bigint CONSTRAINT {TABLE}_mask_size_check
                CHECK (mask_size_bytes > 0),
            mask_width integer CONSTRAINT {TABLE}_mask_width_check CHECK (mask_width > 0),
            mask_height integer CONSTRAINT {TABLE}_mask_height_check CHECK (mask_height > 0),
            model_id text,
            core_version text,
            algorithm text NOT NULL,
            error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            CONSTRAINT {TABLE}_measured_check CHECK (
                status <> 'measured'
                OR (length_m IS NOT NULL AND snout_x IS NOT NULL
                    AND snout_y IS NOT NULL AND fork_x IS NOT NULL
                    AND fork_y IS NOT NULL)),
            CONSTRAINT {TABLE}_mask_whole_check CHECK (
                (mask_key IS NULL) = (mask_bucket IS NULL)
                AND (mask_key IS NULL) = (mask_sha256 IS NULL)
                AND (mask_key IS NULL) = (mask_size_bytes IS NULL)
                AND (mask_key IS NULL) = (mask_width IS NULL)
                AND (mask_key IS NULL) = (mask_height IS NULL)),
            CONSTRAINT {TABLE}_mask_status_check CHECK (
                mask_key IS NULL OR status IN ({_values(MASK_STATUSES)})),
            CONSTRAINT {TABLE}_mask_capture_check CHECK (
                mask_key IS NULL
                OR mask_key LIKE 'masks/phone/' || capture_id::text || '/%.png')
        )
        """)
    op.execute(f"CREATE INDEX {TABLE}_tenant_id_capture_id_seq_idx "
               f"ON {TABLE} (tenant_id, capture_id, seq)")  # fmt: skip
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {TABLE}
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    # Append-only: no UPDATE, no DELETE.
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO {app_role}")
    # The latest run per capture. security_invoker, so the table's RLS applies
    # to whoever reads the view.
    op.execute(f"""
        CREATE VIEW current_{TABLE} WITH (security_invoker = true) AS
        SELECT DISTINCT ON (tenant_id, capture_id) *
        FROM {TABLE}
        ORDER BY tenant_id, capture_id, seq DESC
        """)
    op.execute(f"GRANT SELECT ON current_{TABLE} TO {app_role}")


def downgrade() -> None:
    op.execute(f"DROP VIEW current_{TABLE}")
    op.execute(f"DROP TABLE {TABLE}")

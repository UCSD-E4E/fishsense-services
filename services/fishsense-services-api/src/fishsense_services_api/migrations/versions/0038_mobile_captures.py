"""FishSense Mobile captures: everything the app saved, kept (PLAN.md §9.6).

Mobile is a capture device, and this is scientific data: a measurement must
be reproducible from what the server holds. So a committed mobile capture
keeps every file the app saved and every number it recorded, and the request
as it arrived, so a field this schema doesn't model yet is still kept.

* ``captures`` (PLAN §4.3's core Capture) gains ``client_capture_uuid`` --
  the app's id for the capture, generated when it was taken, unique per tenant:
  the idempotency key of the sync -- ``uploaded_by`` (the user who took it:
  captures are bound to their capturer, so another login can't sync them),
  and location. A mobile capture has no dive and no NAS path:
  ``raw_object_key`` is its RGB in the tenant's bucket, ``checksum`` that
  file's SHA-256.
* ``mobile_captures`` -- the mobile CaptureExtension (§4.3), one per capture:
  device, app / fishsense-core / model versions, intrinsics (row-major K),
  camera pose when the app sends one, the depth / confidence / mask geometry
  including the depth buffer's row stride (ARKit pads rows; the app copies
  ``bytesPerRow``), the place name, the fish the phone measured, and the
  commit request as received (``manifest``).
* ``capture_objects`` -- every file of a capture: role (``rgb_jpeg``,
  ``rgb_lossless``, ``depth_f32``, ``confidence_u8``, ``mask_u8``, ...),
  bucket, key, SHA-256 and size, verified against the store at commit.
  Generic over device kinds.
* ``device_measurement_points`` -- the snout and fork pixels behind each
  ``source = 'device'`` measurement, whether the phone found them (``auto``)
  or the user placed them (``manual``); keyed by the app's own measurement
  uuid, so a re-sent version is a no-op. A redone selection is a new
  measurement row, never an edit.
* ``measurements.measured_at`` -- when a device measured (NULL for the
  server's). **Current is the latest *measured*** (§9.13): a version that
  reaches the server late (a stale retry) must not displace a newer one, so
  ``current_measurements`` orders by ``measured_at`` before ``seq``. The view
  is 0028's, with only that ORDER BY changed; for server rows, all NULL, the
  order is ``seq`` as before.

All tenant-scoped under forced RLS; the three new tables are append-only for
the app role (SELECT, INSERT), like ``measurements``.

Revision ID: 0038
Revises: 0037
"""

import importlib.util
from pathlib import Path

from alembic import context, op

revision = "0038"
down_revision = "0037"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
SHA256 = "'^[0-9a-f]{64}$'"


def _migration(filename: str):
    """A sibling migration's module, for the view text it defines."""
    spec = importlib.util.spec_from_file_location(
        f"_fishsense_migration_{Path(filename).stem}",
        Path(__file__).with_name(filename),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: 0028's `current_measurements`, whose text this migration changes in two
#: places only: its ORDER BY, and `m.*` spelled out as the columns it meant
#: before 0038 -- so the view's columns stay exactly as they were (no
#: `measured_at` among them), and the downgrade can drop the column again.
CURRENT_MEASUREMENTS_0028 = _migration("0028_measurement_views.py").CURRENT_MEASUREMENTS
assert CURRENT_MEASUREMENTS_0028.count("m.seq DESC") == 1
assert CURRENT_MEASUREMENTS_0028.count(" m.*") == 1


def _current_measurements(*, by_measured_at: bool) -> str:
    columns = op.get_bind().exec_driver_sql("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'measurements'
          AND column_name <> 'measured_at'
        ORDER BY ordinal_position
        """).scalars()
    view = CURRENT_MEASUREMENTS_0028.replace(
        " m.*", " " + ", ".join(f"m.{c}" for c in columns)
    )
    if by_measured_at:
        view = view.replace("m.seq DESC", "m.measured_at DESC NULLS LAST, m.seq DESC")
    return view


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _append_only(table: str, app_role: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    op.execute(f"GRANT SELECT, INSERT ON {table} TO {app_role}")


def upgrade() -> None:
    app_role = _app_role()

    op.execute(f"""
        ALTER TABLE captures
            ADD COLUMN client_capture_uuid uuid,
            ADD COLUMN uploaded_by uuid REFERENCES users (id),
            ADD COLUMN latitude double precision,
            ADD COLUMN longitude double precision,
            ADD COLUMN horizontal_accuracy_m double precision,
            ADD CONSTRAINT captures_client_capture_uuid_key
                UNIQUE (tenant_id, client_capture_uuid),
            ADD CONSTRAINT captures_client_capture_uploader_check
                CHECK (client_capture_uuid IS NULL OR uploaded_by IS NOT NULL),
            ADD CONSTRAINT captures_location_check CHECK (
                (latitude IS NULL) = (longitude IS NULL)
                AND (latitude IS NULL OR latitude BETWEEN -90 AND 90)
                AND (longitude IS NULL OR longitude BETWEEN -180 AND 180)
                AND (horizontal_accuracy_m IS NULL OR horizontal_accuracy_m >= 0)
            ),
            ADD CONSTRAINT captures_sha256_format_check
                CHECK (checksum_algorithm <> 'sha256' OR checksum ~ {SHA256})
        """)

    op.execute("""
        CREATE TABLE mobile_captures (
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            capture_id uuid NOT NULL,
            fish_id uuid NOT NULL,
            device_model text,
            device_info text,
            app_version text NOT NULL,
            core_version text,
            model_version text,
            place_name text,
            orientation text
                CONSTRAINT mobile_captures_orientation_check
                CHECK (orientation IN ('portrait', 'landscape')),
            intrinsics double precision[] NOT NULL
                CONSTRAINT mobile_captures_intrinsics_check
                CHECK (cardinality(intrinsics) = 9),
            camera_pose double precision[]
                CONSTRAINT mobile_captures_camera_pose_check
                CHECK (camera_pose IS NULL OR cardinality(camera_pose) = 16),
            depth_width integer NOT NULL CHECK (depth_width > 0),
            depth_height integer NOT NULL CHECK (depth_height > 0),
            depth_row_bytes integer NOT NULL,
            confidence_width integer NOT NULL CHECK (confidence_width > 0),
            confidence_height integer NOT NULL CHECK (confidence_height > 0),
            mask_width integer CHECK (mask_width > 0),
            mask_height integer CHECK (mask_height > 0),
            manifest jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, capture_id),
            UNIQUE (tenant_id, fish_id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            FOREIGN KEY (tenant_id, fish_id) REFERENCES fish (tenant_id, id),
            CONSTRAINT mobile_captures_depth_stride_check
                CHECK (depth_row_bytes >= depth_width * 4),
            CONSTRAINT mobile_captures_mask_geometry_check
                CHECK ((mask_width IS NULL) = (mask_height IS NULL))
        )
        """)
    _append_only("mobile_captures", app_role)

    op.execute(f"""
        CREATE TABLE capture_objects (
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            capture_id uuid NOT NULL,
            role text NOT NULL
                CONSTRAINT capture_objects_role_check CHECK (role ~ '^[a-z0-9_]+$'),
            bucket text NOT NULL,
            object_key text NOT NULL,
            sha256 text NOT NULL
                CONSTRAINT capture_objects_sha256_check CHECK (sha256 ~ {SHA256}),
            size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
            content_type text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, capture_id, role),
            UNIQUE (bucket, object_key),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id)
        )
        """)
    _append_only("capture_objects", app_role)

    op.execute("""
        CREATE TABLE device_measurement_points (
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            measurement_id uuid NOT NULL,
            client_measurement_uuid uuid NOT NULL,
            origin text NOT NULL
                CONSTRAINT device_measurement_points_origin_check
                CHECK (origin IN ('auto', 'manual')),
            snout_x double precision NOT NULL,
            snout_y double precision NOT NULL,
            fork_x double precision NOT NULL,
            fork_y double precision NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, measurement_id),
            UNIQUE (tenant_id, client_measurement_uuid),
            FOREIGN KEY (tenant_id, measurement_id)
                REFERENCES measurements (tenant_id, id)
        )
        """)
    _append_only("device_measurement_points", app_role)

    op.execute("ALTER TABLE measurements ADD COLUMN measured_at timestamptz")
    op.execute(_current_measurements(by_measured_at=True))


def downgrade() -> None:
    op.execute(_current_measurements(by_measured_at=False))
    op.execute("ALTER TABLE measurements DROP COLUMN measured_at")
    op.execute(CURRENT_MEASUREMENTS_0028)
    op.execute("DROP TABLE device_measurement_points")
    op.execute("DROP TABLE capture_objects")
    op.execute("DROP TABLE mobile_captures")
    op.execute("""
        ALTER TABLE captures
            DROP CONSTRAINT captures_sha256_format_check,
            DROP CONSTRAINT captures_location_check,
            DROP CONSTRAINT captures_client_capture_uploader_check,
            DROP CONSTRAINT captures_client_capture_uuid_key,
            DROP COLUMN horizontal_accuracy_m,
            DROP COLUMN longitude,
            DROP COLUMN latitude,
            DROP COLUMN uploaded_by,
            DROP COLUMN client_capture_uuid
        """)

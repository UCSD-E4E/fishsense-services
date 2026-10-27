"""Typed SQLAlchemy 2.0 models of the schema (PLAN.md §3: not SQLModel).

The migrations are the source of truth, because RLS policies and grants live
only there. These models mirror them for typed queries, and
``test_models_match_migrations`` fails the moment the two disagree.

The naming convention reproduces Postgres's own default constraint names, so
constraints the migrations create without explicit names still line up.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Identity,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from fishsense_services_api.mission_facts import PORT_SOURCES, PORTS, PURPOSES

NAMING_CONVENTION = {
    "pk": "%(table_name)s_pkey",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "fk": "%(table_name)s_%(column_0_N_name)s_fkey",
    "ix": "%(table_name)s_%(column_0_N_name)s_idx",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _listed(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=text("now()"))


def _tenant_id() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, ForeignKey("tenants.id", ondelete="CASCADE"))


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _id()
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    #: The partner org whose accounts join this tenant as members (0037).
    org_claim: Mapped[str | None] = mapped_column(Text, unique=True)
    #: Counts in the signed-out page's totals (0055); an operator opts it in.
    public_totals: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class User(Base):
    """The local record of an Authentik identity, keyed on its stable ``sub``."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _id()
    sub: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _created_at()
    #: What their token last said (0065): for their tenants' admins.
    name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    #: May open any tenant: read-only, or after an unlock its admin (0056).
    is_system_admin: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


class SystemAdminAccess(Base):
    """A system admin's visit to, or unlock of, a tenant they are not in (0056)."""

    __tablename__ = "system_admin_access"
    __table_args__ = (
        CheckConstraint("action IN ('view', 'unlock')"),
        CheckConstraint("(action = 'unlock') = (until IS NOT NULL)"),
        Index("system_admin_access_user_tenant_idx", "user_id", "tenant_id", "at"),
    )

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE")
    )
    action: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiToken(Base):
    """A personal token for a script or agent, acting as its owner (0066)."""

    __tablename__ = "api_tokens"
    __table_args__ = (
        CheckConstraint("btrim(name) <> '' AND length(name) <= 100"),
        CheckConstraint("scope IN ('read', 'edit')"),
        Index("api_tokens_user_id_idx", "user_id"),
    )

    id: Mapped[uuid.UUID] = _id()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(Text)
    #: ``read`` (the default) or ``edit``; fixed once made (a trigger).
    scope: Mapped[str] = mapped_column(Text, server_default=text("'read'"))
    #: The token's first characters, to tell tokens apart; never the secret.
    prefix: Mapped[str] = mapped_column(Text)
    #: SHA-256 of the token: the token itself is shown once and never kept.
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Membership(Base):
    __tablename__ = "memberships"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


# --- Global reference data: shared by all tenants, read-only to the app role --


def _number():
    """What people and tools call the row by (migration 0019): its v1 id when
    migrated, else the table's next number above v1's. A trigger fills it."""
    return mapped_column(BigInteger, unique=True)


def _v1_id() -> Mapped[int | None]:
    return mapped_column(BigInteger, unique=True, nullable=True)


class Species(Base):
    __tablename__ = "species"

    id: Mapped[uuid.UUID] = _id()
    scientific_name: Mapped[str | None] = mapped_column(Text, unique=True)
    common_name: Mapped[str | None] = mapped_column(Text)
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    created_at: Mapped[datetime] = _created_at()


class CalibrationTarget(Base):
    """A checkerboard; versioned by ``valid_from``, pitch per axis."""

    __tablename__ = "calibration_targets"
    __table_args__ = (UniqueConstraint("name", "valid_from"),)

    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(Text)
    interior_rows: Mapped[int] = mapped_column(Integer)
    interior_cols: Mapped[int] = mapped_column(Integer)
    pitch_x_m: Mapped[float] = mapped_column(Double)
    pitch_y_m: Mapped[float] = mapped_column(Double)
    notes: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[datetime] = _created_at()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()


class FishModelReference(Base):
    """A physical fish model's known length; versioned by ``valid_from``."""

    __tablename__ = "fish_model_references"
    __table_args__ = (UniqueConstraint("name", "valid_from"),)

    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(
        Text, ForeignKey("fish_models.name", onupdate="CASCADE")
    )
    known_length_m: Mapped[float] = mapped_column(Double)
    is_provisional: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    notes: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[datetime] = _created_at()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()


class SlateTemplate(Base):
    __tablename__ = "slate_templates"

    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(Text, unique=True)
    dpi: Mapped[int | None] = mapped_column(Integer)
    source_path: Mapped[str | None] = mapped_column(Text)
    reference_points: Mapped[list] = mapped_column(JSONB)
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    created_at: Mapped[datetime] = _created_at()


# --- Tenant-scoped data -----------------------------------------------------


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "serial"),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "name"),
        CheckConstraint(
            "kind IN ('lite', 'lite_flatport', 'mobile', 'multilens', 'mono', 'scout')",
            name="devices_kind_check",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    kind: Mapped[str] = mapped_column(Text)
    serial: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()


class Deployment(Base):
    """One field trip: e4e-data-management's dataset (0047). Never a processor
    Deployment, which is a Kubernetes object."""

    __tablename__ = "deployments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "name"),
        UniqueConstraint("tenant_id", "source_path"),
        CheckConstraint("ended_on >= started_on", name="deployments_dates_check"),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    number: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    name: Mapped[str] = mapped_column(Text)
    source_path: Mapped[str | None] = mapped_column(Text)
    started_on: Mapped[date | None] = mapped_column(Date)
    ended_on: Mapped[date | None] = mapped_column(Date)
    readme: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Mission(Base):
    """A Lite offload session, or a phone's outing (``source``, 0057);
    ``priority`` is v1's commit/park flag."""

    __tablename__ = "missions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "source_path"),
        ForeignKeyConstraint(
            ["tenant_id", "device_id"], ["devices.tenant_id", "devices.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "calibration_source_mission_id"],
            ["missions.tenant_id", "missions.id"],
        ),
        CheckConstraint(
            "priority IN ('low', 'high', 'none')", name="missions_priority_check"
        ),
        CheckConstraint(
            "calibration_source_mission_id <> id",
            name="missions_calibration_not_self_check",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "deployment_id"],
            ["deployments.tenant_id", "deployments.id"],
        ),
        Index(None, "tenant_id", "deployment_id"),
        # From mission_facts, which test_the_databases_purposes_are_mission_facts
        # pins to the migrated CHECKs.
        CheckConstraint(
            f"purpose IN ({_listed(PURPOSES)})", name="missions_purpose_check"
        ),
        CheckConstraint(f"port IN ({_listed(PORTS)})", name="missions_port_check"),
        CheckConstraint(
            "CASE WHEN port = 'unknown' THEN port_source IS NULL "
            f"ELSE port_source IS NOT NULL AND port_source IN ({_listed(PORT_SOURCES)}) "
            "END",
            name="missions_port_source_check",
        ),
        CheckConstraint("source IN ('lite', 'mobile')", name="missions_source_check"),
        CheckConstraint(
            "(source = 'lite') = (source_path IS NOT NULL)",
            name="missions_folder_is_lites",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    name: Mapped[str | None] = mapped_column(Text)
    #: ``lite`` (a folder of photos) or ``mobile`` (a phone's outing, 0057).
    source: Mapped[str] = mapped_column(Text, server_default=text("'lite'::text"))
    #: The Lite folder; a phone mission has none.
    source_path: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    priority: Mapped[str] = mapped_column(Text, server_default=text("'low'::text"))
    notes: Mapped[str | None] = mapped_column(Text)
    flip_mission_slate: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    slate_template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("slate_templates.id")
    )
    calibration_target_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("calibration_targets.id")
    )
    calibration_source_mission_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: When the species sync last wrote the slate template or calibration
    #: target: a calibration refusal older than this has expired (0022).
    calibration_links_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = _created_at()
    #: The field trip, when one is recorded (0047).
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: What the mission is for; NULL until someone says (0047).
    purpose: Mapped[str | None] = mapped_column(Text)
    #: The camera port, and what said so; ``unknown`` has no source (0047).
    port: Mapped[str] = mapped_column(Text, server_default=text("'unknown'::text"))
    port_source: Mapped[str | None] = mapped_column(Text)
    port_note: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(Text)
    site: Mapped[str | None] = mapped_column(Text)
    #: The mission's e4edm ``metadata.json``, verbatim.
    source_metadata: Mapped[dict | None] = mapped_column(JSONB)


class Capture(Base):
    """One frame; v1's ``image``. At most one canonical copy per tenant."""

    __tablename__ = "captures"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "source_path"),
        ForeignKeyConstraint(
            ["tenant_id", "mission_id"], ["missions.tenant_id", "missions.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "device_id"], ["devices.tenant_id", "devices.id"]
        ),
        CheckConstraint(
            "checksum_algorithm IN ('md5', 'sha256')",
            name="captures_checksum_algorithm_check",
        ),
        CheckConstraint(
            "source_path IS NOT NULL OR raw_object_key IS NOT NULL",
            name="captures_located_check",
        ),
        CheckConstraint(
            "checksum_algorithm <> 'md5' OR checksum ~ '^[0-9a-f]{32}$'",
            name="captures_md5_format_check",
        ),
        Index(
            "captures_canonical_checksum_key",
            "tenant_id",
            "checksum_algorithm",
            "checksum",
            unique=True,
            postgresql_where=text("is_canonical"),
        ),
        # A dive's captures, per dive (0029).
        Index("captures_tenant_id_mission_id_idx", "tenant_id", "mission_id"),
        # A phone's captures near a time, for grouping them into missions (0057).
        Index("captures_device_time_idx", "tenant_id", "device_id", "captured_at"),
        # A mobile capture's id from the app: the sync's idempotency key (0038).
        UniqueConstraint(
            "tenant_id",
            "client_capture_uuid",
            name="captures_client_capture_uuid_key",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    mission_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    source_path: Mapped[str | None] = mapped_column(Text)
    raw_object_key: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checksum: Mapped[str] = mapped_column(Text)
    checksum_algorithm: Mapped[str] = mapped_column(
        Text, server_default=text("'md5'::text")
    )
    is_canonical: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = _created_at()
    client_capture_uuid: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    latitude: Mapped[float | None] = mapped_column(Double)
    longitude: Mapped[float | None] = mapped_column(Double)
    horizontal_accuracy_m: Mapped[float | None] = mapped_column(Double)
    #: Where an imported phone capture came from (0058); a synced one has
    #: its uploader instead.
    imported_from: Mapped[str | None] = mapped_column(Text)
    #: EXIF FocalLength (0x920A), mm (0069): the zoom gate's input. NULL is
    #: unknown (not read yet, or a phone capture).
    focal_length_mm: Mapped[float | None] = mapped_column(Double)


class CameraCalibration(Base):
    """Intrinsics per device; append-only (current = latest ``seq`` per device)."""

    __tablename__ = "camera_calibrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "device_id"], ["devices.tenant_id", "devices.id"]
        ),
        CheckConstraint(
            "camera_model IN ('pinhole', 'axial_refractive')",
            name="camera_calibrations_camera_model_check",
        ),
        CheckConstraint(
            "medium IN ('air', 'water')", name="camera_calibrations_medium_check"
        ),
        CheckConstraint(
            "coordinate_frame IN ('jpeg', 'raw_sensor')",
            name="camera_calibrations_coordinate_frame_check",
        ),
        CheckConstraint("rms_px >= 0", name="camera_calibrations_rms_px_check"),
        CheckConstraint(
            "camera_model <> 'axial_refractive' OR port_model IS NOT NULL",
            name="camera_calibrations_axial_needs_port_check",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    device_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    camera_model: Mapped[str] = mapped_column(
        Text, server_default=text("'pinhole'::text")
    )
    medium: Mapped[str | None] = mapped_column(Text)
    coordinate_frame: Mapped[str | None] = mapped_column(Text)
    camera_matrix: Mapped[list] = mapped_column(JSONB)
    distortion_coefficients: Mapped[list] = mapped_column(JSONB)
    calibration_target_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("calibration_targets.id")
    )
    rms_px: Mapped[float | None] = mapped_column(Double)
    port_model: Mapped[str | None] = mapped_column(Text)
    port_model_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class LaserCalibration(Base):
    """Per dive, append-only; ``refused`` rows replace v1's dive columns."""

    __tablename__ = "laser_calibrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "mission_id"], ["missions.tenant_id", "missions.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "camera_calibration_id"],
            ["camera_calibrations.tenant_id", "camera_calibrations.id"],
        ),
        CheckConstraint(
            "producer IN ('slate', 'checkerboard', 'dots_range', 'dots_two_ranges',"
            " 'dots_apparent_size', 'bench')",
            name="laser_calibrations_producer_check",
        ),
        CheckConstraint(
            "outcome IN ('accepted', 'refused')",
            name="laser_calibrations_outcome_check",
        ),
        CheckConstraint(
            "slate_template_id IS NULL OR calibration_target_id IS NULL",
            name="laser_calibrations_one_target_check",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    v1_refusal_dive_id: Mapped[int | None] = mapped_column(
        BigInteger, unique=True, nullable=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    mission_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    camera_calibration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    producer: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    laser_position: Mapped[list | None] = mapped_column(JSONB)
    laser_axis: Mapped[list | None] = mapped_column(JSONB)
    refusal_reason: Mapped[str | None] = mapped_column(Text)
    inputs_as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gate_verdicts: Mapped[dict | None] = mapped_column(JSONB)
    lever_arm_m: Mapped[float | None] = mapped_column(Double)
    observation_count: Mapped[int | None] = mapped_column(Integer)
    residual_m: Mapped[float | None] = mapped_column(Double)
    core_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    #: The target the fit (or refusal) used; at most one (0024).
    slate_template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("slate_templates.id")
    )
    calibration_target_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("calibration_targets.id")
    )


class LaserCalibrationRefusalClear(Base):
    """An operator's clear of a refusal, appended (0024): v1
    nulled the dive's refusal columns; v2's refusals are append-only rows."""

    __tablename__ = "laser_calibration_refusal_clears"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        # Named: the default names pass Postgres's 63-character limit.
        UniqueConstraint(
            "tenant_id",
            "laser_calibration_id",
            name="laser_calibration_refusal_clears_refusal_key",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "laser_calibration_id"],
            ["laser_calibrations.tenant_id", "laser_calibrations.id"],
            name="laser_calibration_refusal_clears_refusal_fkey",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    laser_calibration_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class MissionLaserLine(Base):
    """The within-dive laser-dot line fit; append-only (latest ``seq`` per dive)."""

    __tablename__ = "mission_laser_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "mission_id"], ["missions.tenant_id", "missions.id"]
        ),
        CheckConstraint(
            "inlier_count <= n_points",
            name="mission_laser_lines_inliers_within_points_check",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    mission_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    a: Mapped[float] = mapped_column(Double)
    b: Mapped[float] = mapped_column(Double)
    c: Mapped[float] = mapped_column(Double)
    n_points: Mapped[int] = mapped_column(Integer)
    inlier_count: Mapped[int] = mapped_column(Integer)
    inlier_fraction: Mapped[float] = mapped_column(Double)
    residual_std: Mapped[float] = mapped_column(Double)
    label_noise_mad: Mapped[float] = mapped_column(Double)
    #: Which estimator produced `label_noise_mad`; its scale changed with
    #: fishsense-core 4.1.0 (migration 0017).
    noise_estimator: Mapped[str] = mapped_column(Text)
    line_confidence: Mapped[float] = mapped_column(Double)
    fitted_at: Mapped[datetime] = _created_at()


# --- Label Studio labels: one shared core, four kinds -------------------------

LABEL_SOURCES = "('human', 'auto_accept', 'pre_annotation', 'import', 'review')"


class _LabelCore:
    """Columns every label kind shares (migration 0010)."""

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source: Mapped[str | None] = mapped_column(Text)
    ls_project_id: Mapped[int | None] = mapped_column(Integer)
    ls_task_id: Mapped[int | None] = mapped_column(Integer)
    ls_labeler_id: Mapped[int | None] = mapped_column(Integer)
    ls_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    superseded: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    needs_reprocess: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    ls_payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()

    @declared_attr.directive
    def __table_args__(cls) -> tuple:
        table = cls.__tablename__
        return (
            UniqueConstraint("tenant_id", "id"),
            UniqueConstraint("tenant_id", "ls_task_id"),
            UniqueConstraint("tenant_id", "capture_id", "ls_project_id"),
            ForeignKeyConstraint(
                ["tenant_id", "capture_id"], ["captures.tenant_id", "captures.id"]
            ),
            CheckConstraint(f"source IN {LABEL_SOURCES}", name=f"{table}_source_check"),
            *cls._extra_table_args(),
        )

    @classmethod
    def _extra_table_args(cls) -> tuple:
        return ()


class _ReviewLabel:
    """A label a review made (0063): it names its review and the render its
    points are on. Only laser and head/tail labels come from reviews."""

    #: The review that made it; a label of ``source = 'review'`` has one.
    review_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: The render (``capture_renders``) its pixels are on.
    render_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    @classmethod
    def _extra_table_args(cls) -> tuple:
        table = cls.__tablename__
        return (
            ForeignKeyConstraint(
                ["tenant_id", "review_id"],
                ["reviews.tenant_id", "reviews.id"],
                name=f"{table}_review_fk",
            ),
            ForeignKeyConstraint(
                ["tenant_id", "render_id"],
                ["capture_renders.tenant_id", "capture_renders.id"],
                name=f"{table}_render_fk",
            ),
            CheckConstraint(
                "(source = 'review') = (review_id IS NOT NULL AND render_id IS NOT NULL)",
                name=f"{table}_review_label_check",
            ),
            # Stage 14 looks for reviewed frames on every selection.
            Index(
                f"{table}_review_idx",
                "tenant_id",
                "capture_id",
                postgresql_where=text("source = 'review' AND NOT superseded"),
            ),
        )


class LaserLabel(_ReviewLabel, _LabelCore, Base):
    __tablename__ = "laser_labels"

    x: Mapped[float | None] = mapped_column(Double)
    y: Mapped[float | None] = mapped_column(Double)
    label: Mapped[str | None] = mapped_column(Text)
    #: Why it was superseded, as v1 records it (migration 0017); NULL unknown.
    superseded_reason: Mapped[str | None] = mapped_column(Text)


class HeadTailLabel(_ReviewLabel, _LabelCore, Base):
    __tablename__ = "head_tail_labels"

    head_x: Mapped[float | None] = mapped_column(Double)
    head_y: Mapped[float | None] = mapped_column(Double)
    tail_x: Mapped[float | None] = mapped_column(Double)
    tail_y: Mapped[float | None] = mapped_column(Double)
    #: Why it was superseded: ``review`` (0063); NULL otherwise.
    superseded_reason: Mapped[str | None] = mapped_column(Text)


class SlateLabel(_LabelCore, Base):
    __tablename__ = "slate_labels"

    upside_down: Mapped[bool | None] = mapped_column(Boolean)
    reference_points: Mapped[list | None] = mapped_column(JSONB)
    slate_rectangle: Mapped[list | None] = mapped_column(JSONB)
    skipped_points: Mapped[list | None] = mapped_column(JSONB)
    image_url: Mapped[str | None] = mapped_column(Text)
    #: The slate detector's prediction that queued this frame (migration
    #: 0035); NULL for a frame a person marked, and every v1 row.
    slate_presence_prediction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: Why it was retired on purpose (0077), e.g. ``mirrored``; only on a
    #: superseded label. NULL: live, or dead-lettered.
    superseded_reason: Mapped[str | None] = mapped_column(Text)

    @classmethod
    def _extra_table_args(cls) -> tuple:
        return (
            CheckConstraint(
                "superseded_reason IS NULL OR superseded",
                name="slate_labels_superseded_reason_check",
            ),
            ForeignKeyConstraint(
                ["tenant_id", "slate_presence_prediction_id"],
                [
                    "slate_presence_predictions.tenant_id",
                    "slate_presence_predictions.id",
                ],
            ),
        )


class SpeciesLabel(_LabelCore, Base):
    __tablename__ = "species_labels"

    image_url: Mapped[str | None] = mapped_column(Text)
    grouping: Mapped[str | None] = mapped_column(Text)
    top_three_photos_of_group: Mapped[bool | None] = mapped_column(Boolean)
    content_of_image: Mapped[str | None] = mapped_column(Text)
    fish_measurable_category: Mapped[str | None] = mapped_column(Text)
    fish_angle_category: Mapped[str | None] = mapped_column(Text)
    fish_curved_category: Mapped[str | None] = mapped_column(Text)
    fish_angle_degrees: Mapped[float | None] = mapped_column(Double)


class LabelStudioSyncCursor(Base):
    __tablename__ = "label_studio_sync_cursors"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "ls_project_id"),
        CheckConstraint(
            "kind IN ('laser', 'head_tail', 'slate', 'species')",
            name="label_studio_sync_cursors_kind_check",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    kind: Mapped[str] = mapped_column(Text)
    ls_project_id: Mapped[int] = mapped_column(Integer)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- Model predictions: append-only, latest ``seq`` per capture ----------------


class _PredictionCore:
    """Columns every prediction kind shares (migration 0011)."""

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Double, server_default=text("0"))
    predictor_version: Mapped[int | None] = mapped_column(Integer)
    checkpoint: Mapped[str | None] = mapped_column(Text)
    core_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()

    @declared_attr.directive
    def __table_args__(cls) -> tuple:
        return (
            UniqueConstraint("tenant_id", "id"),
            ForeignKeyConstraint(
                ["tenant_id", "capture_id"], ["captures.tenant_id", "captures.id"]
            ),
            *cls._extra_table_args(),
        )

    @classmethod
    def _extra_table_args(cls) -> tuple:
        return ()


class LaserPrediction(_PredictionCore, Base):
    __tablename__ = "laser_predictions"

    x: Mapped[float | None] = mapped_column(Double)
    y: Mapped[float | None] = mapped_column(Double)
    color: Mapped[str | None] = mapped_column(Text)
    color_margin: Mapped[float | None] = mapped_column(Double)
    rejected_out_of_region: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    auto_accept: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    gate_verdict: Mapped[str | None] = mapped_column(Text)
    line_offset_px: Mapped[float | None] = mapped_column(Double)
    line_position_z: Mapped[float | None] = mapped_column(Double)


class LaserPredictionVerdict(Base):
    """The auto-accept gate's verdict on one laser prediction; append-only
    (migration 0021). The latest per prediction is its verdict; a new
    prediction has none, so it reads as unjudged."""

    __tablename__ = "laser_prediction_verdicts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "prediction_id"],
            ["laser_predictions.tenant_id", "laser_predictions.id"],
        ),
        CheckConstraint(
            "gate_verdict IN ('auto_accepted', 'off_line', 'along_line_outlier', "
            "'audit_sample', 'dive_ineligible', 'no_prediction')",
            name="laser_prediction_verdicts_gate_verdict_check",
        ),
        CheckConstraint(
            "NOT auto_accept OR gate_verdict = 'auto_accepted'",
            name="laser_prediction_verdicts_accept_is_a_verdict_check",
        ),
        Index(
            "laser_prediction_verdicts_prediction_idx",
            "tenant_id",
            "prediction_id",
            "seq",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    prediction_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    auto_accept: Mapped[bool] = mapped_column(Boolean)
    gate_verdict: Mapped[str] = mapped_column(Text)
    line_offset_px: Mapped[float | None] = mapped_column(Double)
    line_position_z: Mapped[float | None] = mapped_column(Double)
    created_at: Mapped[datetime] = _created_at()


class SlatePrediction(_PredictionCore, Base):
    __tablename__ = "slate_predictions"

    reference_points: Mapped[list | None] = mapped_column(JSONB)
    rejected_reason: Mapped[str | None] = mapped_column(Text)


class HeadTailPrediction(_PredictionCore, Base):
    __tablename__ = "head_tail_predictions"

    head_x: Mapped[float | None] = mapped_column(Double)
    head_y: Mapped[float | None] = mapped_column(Double)
    tail_x: Mapped[float | None] = mapped_column(Double)
    tail_y: Mapped[float | None] = mapped_column(Double)
    mask_area_px: Mapped[int | None] = mapped_column(Integer)
    silhouette_ratio: Mapped[float | None] = mapped_column(Double)
    crop_x: Mapped[int | None] = mapped_column(Integer)
    crop_y: Mapped[int | None] = mapped_column(Integer)
    laser_label_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(Text, server_default=text("'predicted'::text"))
    rejected_low_confidence: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    #: The kept mask's box, [x_min, y_min, x_max, y_max) (migration 0033).
    mask_bbox: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))

    @classmethod
    def _extra_table_args(cls) -> tuple:
        return (
            ForeignKeyConstraint(
                ["tenant_id", "laser_label_id"],
                ["laser_labels.tenant_id", "laser_labels.id"],
            ),
        )


class SpeciesPrediction(Base):
    """A BioCLIP species pre-annotation, appended (migration 0034; new in v2).
    Shown to a labeler as a suggestion; never a species label."""

    __tablename__ = "species_predictions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "capture_id"], ["captures.tenant_id", "captures.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "headtail_prediction_id"],
            ["head_tail_predictions.tenant_id", "head_tail_predictions.id"],
        ),
        CheckConstraint(
            "status IN ('predicted', 'decode_failed')",
            name="species_predictions_status_check",
        ),
        Index(
            "species_predictions_tenant_id_capture_id_seq_idx",
            "tenant_id",
            "capture_id",
            "seq",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    headtail_prediction_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(Text)
    predictor_version: Mapped[int] = mapped_column(Integer)
    model_id: Mapped[str] = mapped_column(Text)
    predicted_choice: Mapped[str | None] = mapped_column(Text)
    top1_probability: Mapped[float | None] = mapped_column(Double)
    margin: Mapped[float | None] = mapped_column(Double)
    top5: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = _created_at()


class SlatePresencePrediction(Base):
    """The slate detector's P(slate) for one frame, appended (migration
    0035; new in v2). The latest per capture is current."""

    __tablename__ = "slate_presence_predictions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "capture_id"], ["captures.tenant_id", "captures.id"]
        ),
        CheckConstraint(
            "status IN ('predicted', 'decode_failed')",
            name="slate_presence_predictions_status_check",
        ),
        CheckConstraint(
            "probability BETWEEN 0 AND 1",
            name="slate_presence_predictions_probability_check",
        ),
        CheckConstraint(
            "weights_sha256 ~ '^[0-9a-f]{64}$'",
            name="slate_presence_predictions_weights_sha256_check",
        ),
        CheckConstraint(
            "input_width > 0", name="slate_presence_predictions_input_width_check"
        ),
        CheckConstraint(
            "input_height > 0", name="slate_presence_predictions_input_height_check"
        ),
        CheckConstraint(
            "(status = 'predicted') = (probability IS NOT NULL)",
            name="slate_presence_predictions_scored_check",
        ),
        Index(
            "slate_presence_predictions_tenant_id_capture_id_seq_idx",
            "tenant_id",
            "capture_id",
            "seq",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(Text)
    probability: Mapped[float | None] = mapped_column(Double)
    model_name: Mapped[str] = mapped_column(Text)
    model_version: Mapped[int] = mapped_column(Integer)
    weights_sha256: Mapped[str] = mapped_column(Text)
    core_version: Mapped[str | None] = mapped_column(Text)
    processor_version: Mapped[str | None] = mapped_column(Text)
    decode_config: Mapped[str] = mapped_column(Text)
    rectified: Mapped[bool] = mapped_column(Boolean)
    input_width: Mapped[int] = mapped_column(Integer)
    input_height: Mapped[int] = mapped_column(Integer)
    render: Mapped[dict] = mapped_column(JSONB)
    predicted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class FishModel(Base):
    """A physical fish model or calibration target, reached by key (global)."""

    __tablename__ = "fish_models"

    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(Text, unique=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Fish(Base):
    """A real animal of a species, or a fish model -- never both; never deleted."""

    __tablename__ = "fish"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "fish_model_id"),
        CheckConstraint(
            "species_id IS NULL OR fish_model_id IS NULL",
            name="fish_species_or_model_check",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    species_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("species.id"))
    fish_model_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("fish_models.id")
    )
    created_at: Mapped[datetime] = _created_at()


class MissionFrameCluster(Base):
    __tablename__ = "mission_frame_clusters"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "mission_id"], ["missions.tenant_id", "missions.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "fish_id"], ["fish.tenant_id", "fish.id"]),
        CheckConstraint(
            "formed_by IN ('prediction', 'label_studio')",
            name="mission_frame_clusters_formed_by_check",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    mission_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    formed_by: Mapped[str | None] = mapped_column(Text)
    fish_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class MissionFrameClusterCapture(Base):
    """A capture's membership in a cluster; goes with the cluster."""

    __tablename__ = "mission_frame_cluster_captures"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "cluster_id"],
            ["mission_frame_clusters.tenant_id", "mission_frame_clusters.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "capture_id"], ["captures.tenant_id", "captures.id"]
        ),
        # A capture's clusters, for measurement_subjects (0028).
        Index(
            "mission_frame_cluster_captures_tenant_id_capture_id_idx",
            "tenant_id",
            "capture_id",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    cluster_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)


# --- Results: append-only, with their inputs -----------------------------------


class LaserDepth(Base):
    __tablename__ = "laser_depths"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "capture_id"], ["captures.tenant_id", "captures.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "laser_label_id"],
            ["laser_labels.tenant_id", "laser_labels.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "laser_calibration_id"],
            ["laser_calibrations.tenant_id", "laser_calibrations.id"],
        ),
        # A capture's depths, per capture (0029).
        Index("laser_depths_tenant_id_capture_id_idx", "tenant_id", "capture_id"),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    laser_label_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    laser_calibration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    depth_m: Mapped[float] = mapped_column(Double)
    range_m: Mapped[float | None] = mapped_column(Double)
    residual_m: Mapped[float | None] = mapped_column(Double)
    core_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Measurement(Base):
    """A length, with its inputs; current per §9.13 (``current_measurements``)."""

    __tablename__ = "measurements"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "capture_id"], ["captures.tenant_id", "captures.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "fish_id"], ["fish.tenant_id", "fish.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "laser_calibration_id"],
            ["laser_calibrations.tenant_id", "laser_calibrations.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "laser_depth_id"],
            ["laser_depths.tenant_id", "laser_depths.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "laser_label_id"],
            ["laser_labels.tenant_id", "laser_labels.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "head_tail_label_id"],
            ["head_tail_labels.tenant_id", "head_tail_labels.id"],
        ),
        CheckConstraint(
            "source IN ('server', 'device')", name="measurements_source_check"
        ),
        # A capture's measurements, per capture (0028).
        Index("measurements_tenant_id_capture_id_idx", "tenant_id", "capture_id"),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    capture_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    fish_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    source: Mapped[str] = mapped_column(Text)
    length_m: Mapped[float | None] = mapped_column(Double)
    laser_calibration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    laser_depth_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    laser_label_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    head_tail_label_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    algorithm: Mapped[str | None] = mapped_column(Text)
    algorithm_version: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    core_version: Mapped[str | None] = mapped_column(Text)
    model_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    #: When a device measured (0038); NULL for the server's. Orders current.
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LaserDepthRefusal(Base):
    """A laser label tried under a calibration that gave no measurable depth
    (migration 0025; `low_parallax` since 0071): "tried, made no progress"."""

    __tablename__ = "laser_depth_refusals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "capture_id"], ["captures.tenant_id", "captures.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "laser_label_id"],
            ["laser_labels.tenant_id", "laser_labels.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "laser_calibration_id"],
            ["laser_calibrations.tenant_id", "laser_calibrations.id"],
        ),
        CheckConstraint(
            "reason IN ('non_finite_depth', 'non_positive_depth', 'low_parallax')",
            name="laser_depth_refusals_reason_check",
        ),
        CheckConstraint(
            "depth_m <= 0 OR reason = 'low_parallax'",
            name="laser_depth_refusals_depth_m_check",
        ),
        Index(
            "laser_depth_refusals_tenant_id_capture_id_idx", "tenant_id", "capture_id"
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    laser_label_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    laser_x: Mapped[float] = mapped_column(Double)
    laser_y: Mapped[float] = mapped_column(Double)
    laser_calibration_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    reason: Mapped[str] = mapped_column(Text)
    depth_m: Mapped[float | None] = mapped_column(Double)
    core_version: Mapped[str] = mapped_column(Text)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created_at()


class MeasurementRefusal(Base):
    """A capture's measurement inputs that gave no usable length -- since 0071
    including a laser dot that is not measurable -- or a real fish no name
    could be read from (migration 0025)."""

    __tablename__ = "measurement_refusals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "capture_id"], ["captures.tenant_id", "captures.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "laser_calibration_id"],
            ["laser_calibrations.tenant_id", "laser_calibrations.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "species_label_id"],
            ["species_labels.tenant_id", "species_labels.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "laser_label_id"],
            ["laser_labels.tenant_id", "laser_labels.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "head_tail_label_id"],
            ["head_tail_labels.tenant_id", "head_tail_labels.id"],
        ),
        CheckConstraint(
            "reason IN ('non_finite_length', 'zero_length', 'unparseable_species',"
            " 'non_finite_depth', 'non_positive_depth', 'low_parallax')",
            name="measurement_refusals_reason_check",
        ),
        Index(
            "measurement_refusals_tenant_id_capture_id_idx", "tenant_id", "capture_id"
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    laser_calibration_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    reason: Mapped[str] = mapped_column(Text)
    species_label_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    content_of_image: Mapped[str | None] = mapped_column(Text)
    laser_label_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    laser_x: Mapped[float] = mapped_column(Double)
    laser_y: Mapped[float] = mapped_column(Double)
    head_tail_label_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    head_x: Mapped[float] = mapped_column(Double)
    head_y: Mapped[float] = mapped_column(Double)
    tail_x: Mapped[float] = mapped_column(Double)
    tail_y: Mapped[float] = mapped_column(Double)
    length_m: Mapped[float | None] = mapped_column(Double)
    depth_m: Mapped[float | None] = mapped_column(Double)
    algorithm: Mapped[str] = mapped_column(Text)
    algorithm_version: Mapped[str] = mapped_column(Text)
    core_version: Mapped[str] = mapped_column(Text)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created_at()


class LabelStudioProject(Base):
    """Which Label Studio project holds which dive's labels of which kind
    (migration 0020). v1 found them only by title."""

    __tablename__ = "label_studio_projects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "kind", "ls_project_id"),
        ForeignKeyConstraint(
            ["tenant_id", "mission_id"], ["missions.tenant_id", "missions.id"]
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    mission_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    kind: Mapped[str] = mapped_column(Text)
    ls_project_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


# -- automatic results (migration 0036; new in v2) ------------------------


def _producer() -> Mapped[str]:
    return mapped_column(Text, server_default=text("'automatic'::text"))


def _capture_seq_index(table: str) -> Index:
    return Index(
        f"{table}_tenant_id_capture_id_seq_idx", "tenant_id", "capture_id", "seq"
    )


def _fk(columns: tuple[str, str], table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(list(columns), [f"{table}.tenant_id", f"{table}.id"])


class AutomaticHeadTailPrediction(Base):
    """The detector's dot and the SAM 3.1 head/tail at it, with no human
    input; never a label, never a human-path prediction."""

    __tablename__ = "automatic_head_tail_predictions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk(("tenant_id", "capture_id"), "captures"),
        _capture_seq_index("automatic_head_tail_predictions"),
        _fk(("tenant_id", "jpeg_camera_calibration_id"), "camera_calibrations"),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    producer: Mapped[str] = _producer()
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(Text)
    laser_x: Mapped[float | None] = mapped_column(Double)
    laser_y: Mapped[float | None] = mapped_column(Double)
    laser_confidence: Mapped[float | None] = mapped_column(Double)
    laser_predictor_version: Mapped[int | None] = mapped_column(Integer)
    laser_checkpoint: Mapped[str | None] = mapped_column(Text)
    #: The dot's colour, ``red`` or ``green``, and how clearly (0072).
    laser_color: Mapped[str | None] = mapped_column(Text)
    laser_color_margin: Mapped[float | None] = mapped_column(Double)
    head_x: Mapped[float | None] = mapped_column(Double)
    head_y: Mapped[float | None] = mapped_column(Double)
    tail_x: Mapped[float | None] = mapped_column(Double)
    tail_y: Mapped[float | None] = mapped_column(Double)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    mask_area_px: Mapped[int | None] = mapped_column(Integer)
    silhouette_ratio: Mapped[float | None] = mapped_column(Double)
    crop_x: Mapped[int | None] = mapped_column(Integer)
    crop_y: Mapped[int | None] = mapped_column(Integer)
    mask_bbox: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    sam_score: Mapped[float | None] = mapped_column(Double)
    slate_probability: Mapped[float | None] = mapped_column(Double)
    predictor_version: Mapped[int] = mapped_column(Integer)
    checkpoint: Mapped[str | None] = mapped_column(Text)
    core_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    #: The render of the raw the dot and mask were found in (0049).
    jpeg_render_version: Mapped[int | None] = mapped_column(Integer)
    jpeg_camera_calibration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class AutomaticLaserCalibration(Base):
    """A dive's automatic calibration, accepted or refused: label-free size
    constancy, or the slate pose (0078)."""

    __tablename__ = "automatic_laser_calibrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk(("tenant_id", "mission_id"), "missions"),
        _fk(("tenant_id", "camera_calibration_id"), "camera_calibrations"),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    producer: Mapped[str] = _producer()
    mission_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    camera_calibration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    method: Mapped[str] = mapped_column(
        Text, server_default=text("'size_constancy'::text")
    )
    algorithm_version: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    refusal_reason: Mapped[str | None] = mapped_column(Text)
    laser_position: Mapped[list | None] = mapped_column(JSONB)
    laser_axis: Mapped[list | None] = mapped_column(JSONB)
    vanishing_px: Mapped[float | None] = mapped_column(Double)
    line_direction: Mapped[list | None] = mapped_column(JSONB)
    line_offset_px: Mapped[float | None] = mapped_column(Double)
    o_mag_m: Mapped[float | None] = mapped_column(Double)
    frames_used: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    candidate_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    pair_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    size_ratio: Mapped[float | None] = mapped_column(Double)
    se_px: Mapped[float | None] = mapped_column(Double)
    pair_residual_sd: Mapped[float | None] = mapped_column(Double)
    capture_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), server_default=text("'{}'::uuid[]")
    )
    core_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    #: The template a ``slate_pose`` calibration posed with (0078).
    slate_template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("slate_templates.id")
    )


class AutomaticSpeciesPrediction(Base):
    """BioCLIP's zero-shot species on an automatic mask's crop."""

    __tablename__ = "automatic_species_predictions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk(("tenant_id", "capture_id"), "captures"),
        _fk(
            ("tenant_id", "automatic_head_tail_prediction_id"),
            "automatic_head_tail_predictions",
        ),
        _capture_seq_index("automatic_species_predictions"),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    producer: Mapped[str] = _producer()
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    automatic_head_tail_prediction_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(Text)
    predictor_version: Mapped[int] = mapped_column(Integer)
    model_id: Mapped[str] = mapped_column(Text)
    predicted_choice: Mapped[str | None] = mapped_column(Text)
    top1_probability: Mapped[float | None] = mapped_column(Double)
    margin: Mapped[float | None] = mapped_column(Double)
    top5: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = _created_at()


class AutomaticMeasurement(Base):
    """An automatic length (or why not), naming its head/tail and calibration."""

    __tablename__ = "automatic_measurements"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), _fk(
        ("tenant_id", "capture_id"), "captures"
    ), _fk(
        ("tenant_id", "automatic_head_tail_prediction_id"),
        "automatic_head_tail_predictions",
    ), _fk(
        ("tenant_id", "automatic_laser_calibration_id"),
        "automatic_laser_calibrations",
    ), _fk(
        ("tenant_id", "laser_calibration_id"), "laser_calibrations"
    ), _fk(
        ("tenant_id", "camera_calibration_id"), "camera_calibrations"
    ), ForeignKeyConstraint(
        ["tenant_id", "automatic_keypoint_prediction_id"],
        [
            "automatic_keypoint_predictions.tenant_id",
            "automatic_keypoint_predictions.id",
        ],
        name="automatic_measurements_keypoints_fkey",
        ),  # fmt: skip
        _capture_seq_index("automatic_measurements"),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    producer: Mapped[str] = _producer()
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    automatic_head_tail_prediction_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    calibration_source: Mapped[str] = mapped_column(Text)
    automatic_laser_calibration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    laser_calibration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    camera_calibration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    length_m: Mapped[float | None] = mapped_column(Double)
    depth_m: Mapped[float | None] = mapped_column(Double)
    refusal: Mapped[str | None] = mapped_column(Text)
    algorithm: Mapped[str] = mapped_column(Text)
    algorithm_version: Mapped[str] = mapped_column(Text)
    core_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    #: The keypoints measured from (v5, 0074); NULL: the SAM rule's.
    automatic_keypoint_prediction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class AutomaticKeypointPrediction(Base):
    """SimCC's head and tail on an automatic head/tail prediction's SAM box
    (head/tail v5, 0074): ``predicted``, ``keypoints_disagree`` (to review) or
    ``keypoints_refused``; append-only."""

    __tablename__ = "automatic_keypoint_predictions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk(("tenant_id", "capture_id"), "captures"),
        _fk(
            ("tenant_id", "automatic_head_tail_prediction_id"),
            "automatic_head_tail_predictions",
        ),
        Index("automatic_keypoint_predictions_tenant_prediction_idx", "tenant_id",
              "automatic_head_tail_prediction_id", "seq"),  # fmt: skip
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    producer: Mapped[str] = _producer()
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    automatic_head_tail_prediction_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(Text)
    head_x: Mapped[float | None] = mapped_column(Double)
    head_y: Mapped[float | None] = mapped_column(Double)
    tail_x: Mapped[float | None] = mapped_column(Double)
    tail_y: Mapped[float | None] = mapped_column(Double)
    confidence: Mapped[float | None] = mapped_column(Double)
    crop: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    sam_rule_length_px: Mapped[float | None] = mapped_column(Double)
    simcc_length_px: Mapped[float | None] = mapped_column(Double)
    disagreement: Mapped[float | None] = mapped_column(Double)
    predictor_version: Mapped[int] = mapped_column(Integer)
    checkpoint: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class AutomaticLaserColourReading(Base):
    """An automatic dot's colour read off its render after the fact (0076):
    the backfill of dots predicted before contract v22; append-only, and
    never over the dot's own colour."""

    __tablename__ = "automatic_laser_colour_readings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk(("tenant_id", "capture_id"), "captures"),
        _fk(
            ("tenant_id", "automatic_head_tail_prediction_id"),
            "automatic_head_tail_predictions",
        ),
        Index("automatic_laser_colour_readings_tenant_prediction_idx", "tenant_id",
              "automatic_head_tail_prediction_id", "seq"),  # fmt: skip
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    automatic_head_tail_prediction_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    #: ``red``/``green``; NULL when too close to call or not readable.
    color: Mapped[str | None] = mapped_column(Text)
    color_margin: Mapped[float | None] = mapped_column(Double)
    #: ``render``: read off the prediction's render, not the raw.
    source: Mapped[str] = mapped_column(Text)
    reading_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


class CaptureRender(Base):
    """A capture's raw rectified to a JPEG in its tenant's bucket, or why not
    (0051); append-only, current = latest ``seq`` per capture."""

    __tablename__ = "capture_renders"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk(("tenant_id", "capture_id"), "captures"),
        _fk(("tenant_id", "camera_calibration_id"), "camera_calibrations"),
        _capture_seq_index("capture_renders"),
        Index(
            "capture_renders_rendered_once_per_bucket_idx",
            "tenant_id", "capture_id", "render_version", "camera_calibration_id",
            "bucket", unique=True, postgresql_where=text("status = 'rendered'"),
        ),  # fmt: skip
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(Text)
    render_version: Mapped[int] = mapped_column(Integer)
    camera_calibration_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    bucket: Mapped[str | None] = mapped_column(Text)
    key: Mapped[str | None] = mapped_column(Text)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    md5: Mapped[str | None] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


class CaptureRaw(Base):
    """A capture's raw copied into its tenant's bucket by the render stage,
    checked against the capture's checksum (0052); append-only."""

    __tablename__ = "capture_raws"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "capture_id", "bucket"),
        _fk(("tenant_id", "capture_id"), "captures"),
        CheckConstraint("bytes > 0", name="capture_raws_bytes_check"),
        CheckConstraint("md5 ~ '^[0-9a-f]{32}$'", name="capture_raws_md5_check"),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    bucket: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text)
    bytes: Mapped[int] = mapped_column(BigInteger)
    md5: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class MissionBeamMove(Base):
    """When the laser moved mid-dive (0073): the window between the last dot
    on the first aim and the first on the second, or no window (cleared);
    append-only, the latest per mission current."""

    __tablename__ = "mission_beam_moves"
    __table_args__ = (
        _fk(("tenant_id", "mission_id"), "missions"),
        Index("mission_beam_moves_tenant_mission_idx", "tenant_id", "mission_id"),
        CheckConstraint(
            "(moved_after IS NULL) = (moved_before IS NULL) "
            "AND (moved_after IS NULL OR moved_after < moved_before)",
            name="mission_beam_moves_window_check",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    mission_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    moved_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    moved_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detector_version: Mapped[str] = mapped_column(Text)
    gain: Mapped[int | None] = mapped_column(Integer)
    angle_deg: Mapped[float | None] = mapped_column(Double)
    apart_px: Mapped[float | None] = mapped_column(Double)
    dots_before: Mapped[int | None] = mapped_column(Integer)
    dots_after: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


class ShadowLaserCalibration(Base):
    """A shadow size-constancy fit or its refusal (0061); feeds nothing;
    append-only."""

    __tablename__ = "shadow_laser_calibrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk(("tenant_id", "mission_id"), "missions"),
        _fk(("tenant_id", "reference_laser_calibration_id"), "laser_calibrations"),
        Index(None, "tenant_id", "mission_id"),
        CheckConstraint("dot_source IN ('labels', 'detector')",
                        name="shadow_laser_calibrations_dot_source_check"),
        CheckConstraint("variant IN ('port', 'gated')",
                        name="shadow_laser_calibrations_variant_check"),
        CheckConstraint("outcome IN ('accepted', 'refused')",
                        name="shadow_laser_calibrations_outcome_check"),
        CheckConstraint("line_source IN ('mission_line', 'detector_line', 'dots')",
                        name="shadow_laser_calibrations_line_source_check"),
        CheckConstraint("frames_used >= 0",
                        name="shadow_laser_calibrations_frames_used_check"),
        CheckConstraint(
            "outcome <> 'accepted' OR (coalesce(cardinality(laser_position), 0) = 3 "
            "AND coalesce(cardinality(laser_axis), 0) = 3)",
            name="shadow_laser_calibrations_accepted_check",
        ),
        CheckConstraint("(outcome = 'refused') = (refusal_reason IS NOT NULL)",
                        name="shadow_laser_calibrations_refused_check"),
    )  # fmt: skip

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    mission_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    dot_source: Mapped[str] = mapped_column(Text)
    variant: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    refusal_reason: Mapped[str | None] = mapped_column(Text)
    refusals: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'")
    )
    laser_position: Mapped[list[float] | None] = mapped_column(ARRAY(Double))
    laser_axis: Mapped[list[float] | None] = mapped_column(ARRAY(Double))
    o_mag_m: Mapped[float] = mapped_column(Double)
    vanishing_px: Mapped[float | None] = mapped_column(Double)
    line_direction: Mapped[list[float] | None] = mapped_column(ARRAY(Double))
    line_offset_px: Mapped[float | None] = mapped_column(Double)
    line_source: Mapped[str | None] = mapped_column(Text)
    camera_calibration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    reference_laser_calibration_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    reference_borrowed: Mapped[bool | None] = mapped_column(Boolean)
    angle_diff_deg: Mapped[float | None] = mapped_column(Double)
    vp_offset_px: Mapped[float | None] = mapped_column(Double)
    vp_offset_deg_fx: Mapped[float | None] = mapped_column(Double)
    vp_offset_deg_f2850: Mapped[float | None] = mapped_column(Double)
    fit_residual: Mapped[float | None] = mapped_column(Double)
    se_px: Mapped[float | None] = mapped_column(Double)
    se_deg: Mapped[float | None] = mapped_column(Double)
    frames_used: Mapped[int] = mapped_column(Integer)
    frames_rejected: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    size_spread: Mapped[float | None] = mapped_column(Double)
    range_min_m: Mapped[float | None] = mapped_column(Double)
    range_max_m: Mapped[float | None] = mapped_column(Double)
    lever_m: Mapped[float | None] = mapped_column(Double)
    lever_m_production_offset: Mapped[float | None] = mapped_column(Double)
    size_cv_pct: Mapped[float | None] = mapped_column(Double)
    size_maxdev_pct: Mapped[float | None] = mapped_column(Double)
    sam_score_min: Mapped[float | None] = mapped_column(Double)
    sam_score_median: Mapped[float | None] = mapped_column(Double)
    no_mask_share: Mapped[float | None] = mapped_column(Double)
    sam_model: Mapped[str] = mapped_column(Text)
    sam_prompts: Mapped[list[str]] = mapped_column(ARRAY(Text))
    sam_threshold: Mapped[float] = mapped_column(Double)
    render_long_side: Mapped[int] = mapped_column(Integer)
    gate: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    algorithm_version: Mapped[str] = mapped_column(Text)
    code_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class ShadowCalibrationFrame(Base):
    """A slate frame a shadow fit considered (0061); append-only."""

    __tablename__ = "shadow_calibration_frames"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk(("tenant_id", "shadow_laser_calibration_id"), "shadow_laser_calibrations"),
        _fk(("tenant_id", "capture_id"), "captures"),
        Index("shadow_calibration_frames_tenant_id_calibration_idx",
              "tenant_id", "shadow_laser_calibration_id"),
        CheckConstraint("frame_source IN ('label', 'detector', 'both')",
                        name="shadow_calibration_frames_frame_source_check"),
        CheckConstraint("used = (rejected_reason IS NULL)",
                        name="shadow_calibration_frames_used_check"),
    )  # fmt: skip

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    shadow_laser_calibration_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    frame_source: Mapped[str] = mapped_column(Text)
    slate_probability: Mapped[float | None] = mapped_column(Double)
    dot_x: Mapped[float | None] = mapped_column(Double)
    dot_y: Mapped[float | None] = mapped_column(Double)
    dot_label_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    dot_label_source: Mapped[str | None] = mapped_column(Text)
    dot_prediction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    dot_confidence: Mapped[float | None] = mapped_column(Double)
    line_distance_px: Mapped[float | None] = mapped_column(Double)
    sam_prompt: Mapped[str | None] = mapped_column(Text)
    sam_score: Mapped[float | None] = mapped_column(Double)
    mask_area_px: Mapped[float | None] = mapped_column(Double)
    size: Mapped[float | None] = mapped_column(Double)
    t_px: Mapped[float | None] = mapped_column(Double)
    range_m: Mapped[float | None] = mapped_column(Double)
    used: Mapped[bool] = mapped_column(Boolean)
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    capture_number: Mapped[int | None] = mapped_column(Integer)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class PredictionMask(Base):
    """A SAM mask a prediction kept, stored in the tenant's bucket the way
    FishSense Mobile stores its mask, as the app's mask_png (0059); append-only."""

    __tablename__ = "prediction_masks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("bucket", "object_key"),
        _fk(("tenant_id", "capture_id"), "captures"),
        _fk(("tenant_id", "automatic_head_tail_prediction_id"),
            "automatic_head_tail_predictions"),
        _fk(("tenant_id", "head_tail_prediction_id"), "head_tail_predictions"),
        Index(None, "tenant_id", "capture_id"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'",
                        name="prediction_masks_sha256_check"),
        CheckConstraint("size_bytes > 0", name="prediction_masks_size_check"),
        CheckConstraint("width > 0", name="prediction_masks_width_check"),
        CheckConstraint("height > 0", name="prediction_masks_height_check"),
        CheckConstraint(
            "(automatic_head_tail_prediction_id IS NULL) "
            "<> (head_tail_prediction_id IS NULL)",
            name="prediction_masks_one_prediction_check",
        ),
    )  # fmt: skip

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    automatic_head_tail_prediction_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, unique=True
    )
    head_tail_prediction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True)
    bucket: Mapped[str] = mapped_column(Text)
    object_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(Text)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


class MaskRegeneration(Base):
    """A regeneration of a prediction's SAM mask by the mask backfill, stored
    or not, and what it got (0062); append-only."""

    __tablename__ = "mask_regenerations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk(("tenant_id", "capture_id"), "captures"),
        _fk(("tenant_id", "automatic_head_tail_prediction_id"),
            "automatic_head_tail_predictions"),
        _fk(("tenant_id", "head_tail_prediction_id"), "head_tail_predictions"),
        _fk(("tenant_id", "prediction_mask_id"), "prediction_masks"),
        Index(None, "tenant_id", "capture_id"),
        CheckConstraint(
            "outcome IN ('reproduced', 'mismatch', 'no_mask', 'image_unavailable', "
            "'decode_failed')",
            name="mask_regenerations_outcome_check",
        ),
        CheckConstraint(
            "(automatic_head_tail_prediction_id IS NULL) "
            "<> (head_tail_prediction_id IS NULL)",
            name="mask_regenerations_one_prediction_check",
        ),
    )  # fmt: skip

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    automatic_head_tail_prediction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    head_tail_prediction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    outcome: Mapped[str] = mapped_column(Text)
    mask_area_px: Mapped[int | None] = mapped_column(Integer)
    crop_x: Mapped[int | None] = mapped_column(Integer)
    crop_y: Mapped[int | None] = mapped_column(Integer)
    head_x: Mapped[float | None] = mapped_column(Double)
    head_y: Mapped[float | None] = mapped_column(Double)
    tail_x: Mapped[float | None] = mapped_column(Double)
    tail_y: Mapped[float | None] = mapped_column(Double)
    prediction_mask_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created_at()


class CaptureThumbnail(Base):
    """A render's thumbnail for /fish's cards, or that the render was not
    there to read (0053); append-only."""

    __tablename__ = "capture_thumbnails"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), _fk(
        ("tenant_id", "render_id"), "capture_renders"
    ), _fk(("tenant_id", "capture_id"), "captures"), Index(
        "capture_thumbnails_settled_once_idx",
        "tenant_id",
        "render_id",
        "target_width",
        unique=True,
        postgresql_where=text("status IN ('made', 'render_unavailable')"),
        ),  # fmt: skip
        Index(None, "tenant_id", "render_id"),
        Index(None, "tenant_id", "capture_id"),
        CheckConstraint(
            "status IN ('made', 'render_unavailable', 'failed')",
            name="capture_thumbnails_status_check",
        ),
        CheckConstraint(
            "target_width > 0", name="capture_thumbnails_target_width_check"
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    render_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(Text)
    target_width: Mapped[int] = mapped_column(Integer)
    bucket: Mapped[str | None] = mapped_column(Text)
    key: Mapped[str | None] = mapped_column(Text)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    md5: Mapped[str | None] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


class MobileCapture(Base):
    """The mobile CaptureExtension (0038): what the phone recorded with a
    capture, and the commit request as received. Append-only."""

    __tablename__ = "mobile_captures"
    __table_args__ = (
        UniqueConstraint("tenant_id", "fish_id"),
        ForeignKeyConstraint(
            ["tenant_id", "capture_id"], ["captures.tenant_id", "captures.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "fish_id"], ["fish.tenant_id", "fish.id"]),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    fish_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    device_model: Mapped[str | None] = mapped_column(Text)
    device_info: Mapped[str | None] = mapped_column(Text)
    app_version: Mapped[str | None] = mapped_column(Text)
    core_version: Mapped[str | None] = mapped_column(Text)
    model_version: Mapped[str | None] = mapped_column(Text)
    place_name: Mapped[str | None] = mapped_column(Text)
    orientation: Mapped[str | None] = mapped_column(Text)
    intrinsics: Mapped[list[float]] = mapped_column(ARRAY(Double))
    camera_pose: Mapped[list[float] | None] = mapped_column(ARRAY(Double))
    depth_width: Mapped[int] = mapped_column(Integer)
    depth_height: Mapped[int] = mapped_column(Integer)
    depth_row_bytes: Mapped[int] = mapped_column(Integer)
    confidence_width: Mapped[int] = mapped_column(Integer)
    confidence_height: Mapped[int] = mapped_column(Integer)
    mask_width: Mapped[int | None] = mapped_column(Integer)
    mask_height: Mapped[int | None] = mapped_column(Integer)
    manifest: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class CaptureObject(Base):
    """Every stored file of a capture, by role, with its verified SHA-256 and
    size (0038). Append-only."""

    __tablename__ = "capture_objects"
    __table_args__ = (
        UniqueConstraint("bucket", "object_key"),
        ForeignKeyConstraint(
            ["tenant_id", "capture_id"], ["captures.tenant_id", "captures.id"]
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    role: Mapped[str] = mapped_column(Text, primary_key=True)
    bucket: Mapped[str] = mapped_column(Text)
    object_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class DeviceMeasurementPoints(Base):
    """The snout/fork pixels behind a ``source = 'device'`` measurement, found
    by the phone or placed by the user (0038). Append-only."""

    __tablename__ = "device_measurement_points"
    __table_args__ = (
        UniqueConstraint("tenant_id", "client_measurement_uuid"),
        ForeignKeyConstraint(
            ["tenant_id", "measurement_id"],
            ["measurements.tenant_id", "measurements.id"],
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    measurement_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    client_measurement_uuid: Mapped[uuid.UUID] = mapped_column(Uuid)
    origin: Mapped[str] = mapped_column(Text)
    snout_x: Mapped[float] = mapped_column(Double)
    snout_y: Mapped[float] = mapped_column(Double)
    fork_x: Mapped[float] = mapped_column(Double)
    fork_y: Mapped[float] = mapped_column(Double)
    created_at: Mapped[datetime] = _created_at()


class AutomaticResultsExclusion(Base):
    """A dive the automatic-results backlog leaves alone, and why (0039).
    An operator's decision; the app role only reads it."""

    __tablename__ = "automatic_results_exclusions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "mission_id"], ["missions.tenant_id", "missions.id"]
        ),
        CheckConstraint(
            "btrim(reason) <> ''", name="automatic_results_exclusions_reason_check"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    mission_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class AutomaticFrameFailure(Base):
    """A frame the processor left out of an automatic run (0044). Never a
    dive strike; at `AUTOMATIC_FRAME_MAX_FAILURES` the store writes the
    frame's terminal ``frame_failed`` head/tail. Append-only."""

    __tablename__ = "automatic_frame_failures"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "capture_id"], ["captures.tenant_id", "captures.id"]
        ),
        Index(
            "automatic_frame_failures_tenant_id_capture_id_idx",
            "tenant_id",
            "capture_id",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    predictor_version: Mapped[int] = mapped_column(Integer)
    error: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class AutomaticResultsFailure(Base):
    """A failed automatic run of one of a dive's steps (0040). Append-only;
    an operator deletes a dive's rows to retry it."""

    __tablename__ = "automatic_results_failures"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "mission_id"], ["missions.tenant_id", "missions.id"]
        ),
        CheckConstraint(
            "step IN ('frames', 'calibration', 'lengths', 'species')",
            name="automatic_results_failures_step_check",
        ),
        Index(
            "automatic_results_failures_tenant_id_mission_id_idx",
            "tenant_id",
            "mission_id",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    mission_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    step: Mapped[str] = mapped_column(Text)
    error: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class CapturePlaceName(Base):
    """A place name derived from a capture's coordinates (0041). Append-only;
    the current name is the latest. NULL: no named place in range."""

    __tablename__ = "capture_place_names"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "capture_id"], ["captures.tenant_id", "captures.id"]
        ),
        Index(
            "capture_place_names_tenant_id_capture_id_derived_at_idx",
            "tenant_id",
            "capture_id",
            "derived_at",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    place_name: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    properties: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    derived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ReviewerSkill(Base):
    """A person's level in one kind of review (0063); append-only, the
    latest is current. Earned, or set by an admin; no row means Learning."""

    __tablename__ = "reviewer_skills"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index("reviewer_skills_user_idx", "tenant_id", "user_id", "kind", "seq"),
        CheckConstraint("kind IN ('laser', 'head_tail', 'species')",
                        name="reviewer_skills_kind_check"),
        CheckConstraint("level IN ('learning', 'trusted', 'expert')",
                        name="reviewer_skills_level_check"),
        CheckConstraint("source IN ('earned', 'admin')",
                        name="reviewer_skills_source_check"),
        CheckConstraint("(source = 'admin') = (set_by IS NOT NULL)",
                        name="reviewer_skills_check"),
        # A skill is a member's, and goes with the membership.
        ForeignKeyConstraint(["tenant_id", "user_id"],
                             ["memberships.tenant_id", "memberships.user_id"],
                             name="reviewer_skills_membership_fk", ondelete="CASCADE"),
    )  # fmt: skip

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    set_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Review(Base):
    """One reviewer's action on one kind of a frame's automatic result
    (0063): accept, fix, reject or confirm; append-only."""

    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk(("tenant_id", "capture_id"), "captures"),
        _fk(("tenant_id", "automatic_head_tail_prediction_id"),
            "automatic_head_tail_predictions"),
        _fk(("tenant_id", "render_id"), "capture_renders"),
        _fk(("tenant_id", "confirms_review_id"), "reviews"),
        Index("reviews_capture_idx", "tenant_id", "capture_id", "seq"),
        Index("reviews_confirms_idx", "tenant_id", "confirms_review_id"),
    )  # fmt: skip

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    capture_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    reviewer_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(Text)
    automatic_head_tail_prediction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    reason: Mapped[str | None] = mapped_column(Text)
    x: Mapped[float | None] = mapped_column(Double)
    y: Mapped[float | None] = mapped_column(Double)
    head_x: Mapped[float | None] = mapped_column(Double)
    head_y: Mapped[float | None] = mapped_column(Double)
    tail_x: Mapped[float | None] = mapped_column(Double)
    tail_y: Mapped[float | None] = mapped_column(Double)
    raw: Mapped[dict | None] = mapped_column(JSONB)
    render_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    scientific_name: Mapped[str | None] = mapped_column(Text)
    confirms_review_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = _created_at()

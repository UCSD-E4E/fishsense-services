"""Typed SQLAlchemy 2.0 models of the schema (PLAN.md §3: not SQLModel).

The migrations are the source of truth, because RLS policies and grants live
only there. These models mirror them for typed queries, and
``test_models_match_migrations`` fails the moment the two disagree.

The naming convention reproduces Postgres's own default constraint names, so
constraints the migrations create without explicit names still line up.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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

NAMING_CONVENTION = {
    "pk": "%(table_name)s_pkey",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "fk": "%(table_name)s_%(column_0_N_name)s_fkey",
    "ix": "%(table_name)s_%(column_0_N_name)s_idx",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


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


class User(Base):
    """The local record of an Authentik identity, keyed on its stable ``sub``."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _id()
    sub: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _created_at()


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


class Dive(Base):
    """A Lite offload session; ``priority`` is v1's commit/park flag."""

    __tablename__ = "dives"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "source_path"),
        ForeignKeyConstraint(
            ["tenant_id", "device_id"], ["devices.tenant_id", "devices.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "calibration_source_dive_id"],
            ["dives.tenant_id", "dives.id"],
        ),
        CheckConstraint(
            "priority IN ('low', 'high', 'none')", name="dives_priority_check"
        ),
        CheckConstraint(
            "calibration_source_dive_id <> id", name="dives_calibration_not_self_check"
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    name: Mapped[str | None] = mapped_column(Text)
    source_path: Mapped[str] = mapped_column(Text)
    dived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    priority: Mapped[str] = mapped_column(Text, server_default=text("'low'::text"))
    notes: Mapped[str | None] = mapped_column(Text)
    flip_dive_slate: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    slate_template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("slate_templates.id")
    )
    calibration_target_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("calibration_targets.id")
    )
    calibration_source_dive_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: When the species sync last wrote the slate template or calibration
    #: target: a calibration refusal older than this has expired (0022).
    calibration_links_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = _created_at()


class Capture(Base):
    """One frame; v1's ``image``. At most one canonical copy per tenant."""

    __tablename__ = "captures"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "source_path"),
        ForeignKeyConstraint(["tenant_id", "dive_id"], ["dives.tenant_id", "dives.id"]),
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
        Index("captures_tenant_id_dive_id_idx", "tenant_id", "dive_id"),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    dive_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
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
        ForeignKeyConstraint(["tenant_id", "dive_id"], ["dives.tenant_id", "dives.id"]),
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
    dive_id: Mapped[uuid.UUID] = mapped_column(Uuid)
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


class DiveLaserLine(Base):
    """The within-dive laser-dot line fit; append-only (latest ``seq`` per dive)."""

    __tablename__ = "dive_laser_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "dive_id"], ["dives.tenant_id", "dives.id"]),
        CheckConstraint(
            "inlier_count <= n_points",
            name="dive_laser_lines_inliers_within_points_check",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    dive_id: Mapped[uuid.UUID] = mapped_column(Uuid)
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

LABEL_SOURCES = "('human', 'auto_accept', 'pre_annotation', 'import')"


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


class LaserLabel(_LabelCore, Base):
    __tablename__ = "laser_labels"

    x: Mapped[float | None] = mapped_column(Double)
    y: Mapped[float | None] = mapped_column(Double)
    label: Mapped[str | None] = mapped_column(Text)
    #: Why it was superseded, as v1 records it (migration 0017); NULL unknown.
    superseded_reason: Mapped[str | None] = mapped_column(Text)


class HeadTailLabel(_LabelCore, Base):
    __tablename__ = "head_tail_labels"

    head_x: Mapped[float | None] = mapped_column(Double)
    head_y: Mapped[float | None] = mapped_column(Double)
    tail_x: Mapped[float | None] = mapped_column(Double)
    tail_y: Mapped[float | None] = mapped_column(Double)


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

    @classmethod
    def _extra_table_args(cls) -> tuple:
        return (
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


class DiveFrameCluster(Base):
    __tablename__ = "dive_frame_clusters"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "dive_id"], ["dives.tenant_id", "dives.id"]),
        ForeignKeyConstraint(["tenant_id", "fish_id"], ["fish.tenant_id", "fish.id"]),
        CheckConstraint(
            "formed_by IN ('prediction', 'label_studio')",
            name="dive_frame_clusters_formed_by_check",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
    number: Mapped[int] = _number()
    dive_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    formed_by: Mapped[str | None] = mapped_column(Text)
    fish_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class DiveFrameClusterCapture(Base):
    """A capture's membership in a cluster; goes with the cluster."""

    __tablename__ = "dive_frame_cluster_captures"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "cluster_id"],
            ["dive_frame_clusters.tenant_id", "dive_frame_clusters.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "capture_id"], ["captures.tenant_id", "captures.id"]
        ),
        # A capture's clusters, for measurement_subjects (0028).
        Index(
            "dive_frame_cluster_captures_tenant_id_capture_id_idx",
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


class LaserDepthRefusal(Base):
    """A laser label tried under a calibration that gave no depth in front of
    the camera (migration 0025): "tried, made no progress"."""

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
            "reason IN ('non_finite_depth', 'non_positive_depth')",
            name="laser_depth_refusals_reason_check",
        ),
        CheckConstraint("depth_m <= 0", name="laser_depth_refusals_depth_m_check"),
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
    """A capture's measurement inputs that gave no usable length, or a real
    fish no name could be read from (migration 0025)."""

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
            "reason IN ('non_finite_length', 'zero_length', 'unparseable_species')",
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
        ForeignKeyConstraint(["tenant_id", "dive_id"], ["dives.tenant_id", "dives.id"]),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    dive_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
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


class AutomaticLaserCalibration(Base):
    """A dive's label-free size-constancy calibration, accepted or refused."""

    __tablename__ = "automatic_laser_calibrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk(("tenant_id", "dive_id"), "dives"),
        _fk(("tenant_id", "camera_calibration_id"), "camera_calibrations"),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    producer: Mapped[str] = _producer()
    dive_id: Mapped[uuid.UUID] = mapped_column(Uuid)
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
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        _fk(("tenant_id", "capture_id"), "captures"),
        _fk(
            ("tenant_id", "automatic_head_tail_prediction_id"),
            "automatic_head_tail_predictions",
        ),
        _fk(
            ("tenant_id", "automatic_laser_calibration_id"),
            "automatic_laser_calibrations",
        ),
        _fk(("tenant_id", "laser_calibration_id"), "laser_calibrations"),
        _fk(("tenant_id", "camera_calibration_id"), "camera_calibrations"),
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

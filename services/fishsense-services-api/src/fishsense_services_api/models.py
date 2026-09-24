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
from sqlalchemy.dialects.postgresql import JSONB
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


def _v1_id() -> Mapped[int | None]:
    return mapped_column(BigInteger, unique=True, nullable=True)


class Species(Base):
    __tablename__ = "species"

    id: Mapped[uuid.UUID] = _id()
    scientific_name: Mapped[str | None] = mapped_column(Text, unique=True)
    common_name: Mapped[str | None] = mapped_column(Text)
    v1_id: Mapped[int | None] = _v1_id()
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


class FishModelReference(Base):
    """A physical fish model's known length; versioned by ``valid_from``."""

    __tablename__ = "fish_model_references"
    __table_args__ = (UniqueConstraint("name", "valid_from"),)

    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(Text)
    known_length_m: Mapped[float] = mapped_column(Double)
    is_provisional: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    notes: Mapped[str | None] = mapped_column(Text)
    valid_from: Mapped[datetime] = _created_at()
    v1_id: Mapped[int | None] = _v1_id()


class SlateTemplate(Base):
    __tablename__ = "slate_templates"

    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(Text, unique=True)
    dpi: Mapped[int | None] = mapped_column(Integer)
    source_path: Mapped[str | None] = mapped_column(Text)
    reference_points: Mapped[list] = mapped_column(JSONB)
    v1_id: Mapped[int | None] = _v1_id()
    created_at: Mapped[datetime] = _created_at()


# --- Tenant-scoped data -----------------------------------------------------


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "serial"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(
            "kind IN ('lite', 'lite_flatport', 'mobile', 'multilens', 'mono', 'scout')",
            name="devices_kind_check",
        ),
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    kind: Mapped[str] = mapped_column(Text)
    serial: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    v1_id: Mapped[int | None] = _v1_id()


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
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
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
    )

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
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
    line_confidence: Mapped[float] = mapped_column(Double)
    fitted_at: Mapped[datetime] = _created_at()


# --- Label Studio labels: one shared core, four kinds -------------------------

LABEL_SOURCES = "('human', 'auto_accept', 'pre_annotation', 'import')"


class _LabelCore:
    """Columns every label kind shares (migration 0010)."""

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
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
        )


class LaserLabel(_LabelCore, Base):
    __tablename__ = "laser_labels"

    x: Mapped[float | None] = mapped_column(Double)
    y: Mapped[float | None] = mapped_column(Double)
    label: Mapped[str | None] = mapped_column(Text)


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
    kind: Mapped[str] = mapped_column(Text)
    ls_project_id: Mapped[int] = mapped_column(Integer)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- Model predictions: append-only, latest ``seq`` per capture ----------------


class _PredictionCore:
    """Columns every prediction kind shares (migration 0011)."""

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    v1_id: Mapped[int | None] = _v1_id()
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

    @classmethod
    def _extra_table_args(cls) -> tuple:
        return (
            ForeignKeyConstraint(
                ["tenant_id", "laser_label_id"],
                ["laser_labels.tenant_id", "laser_labels.id"],
            ),
        )

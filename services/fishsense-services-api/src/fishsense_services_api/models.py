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
    DateTime,
    Double,
    ForeignKey,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "pk": "%(table_name)s_pkey",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
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
    __table_args__ = (UniqueConstraint("tenant_id", "serial"),)

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    kind: Mapped[str] = mapped_column(Text)
    serial: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()

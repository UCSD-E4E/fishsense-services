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
    DateTime,
    ForeignKey,
    MetaData,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
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


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("tenant_id", "serial"),)

    id: Mapped[uuid.UUID] = _id()
    tenant_id: Mapped[uuid.UUID] = _tenant_id()
    kind: Mapped[str] = mapped_column(Text)
    serial: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()

"""SQLAlchemy 2.0 ORM models for the yarn dyeing & winding ERP."""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class BatchStatus(str, enum.Enum):
    IN_PROCESS = "IN_PROCESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class Shift(str, enum.Enum):
    MORNING = "MORNING"
    NIGHT = "NIGHT"


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    PARTIAL = "PARTIAL"


class OrderStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    DISPATCHED = "DISPATCHED"


class RawInventory(Base):
    __tablename__ = "raw_inventory"
    __table_args__ = (
        CheckConstraint("initial_weight_kg > 0", name="ck_raw_initial_positive"),
        CheckConstraint("current_weight_kg >= 0", name="ck_raw_current_nonneg"),
        # FIFO lookup: oldest lot of a given count first
        Index("ix_raw_count_received", "yarn_count", "received_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    yarn_count: Mapped[str] = mapped_column(String(16), index=True)  # e.g. '2/30'
    mill_source: Mapped[str] = mapped_column(String(120))
    initial_weight_kg: Mapped[float] = mapped_column(Float)
    current_weight_kg: Mapped[float] = mapped_column(Float)
    received_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    batches: Mapped[List["DyeingBatch"]] = relationship(back_populates="raw_yarn")


class DyeingBatch(Base):
    __tablename__ = "dyeing_batches"
    __table_args__ = (
        CheckConstraint("spring_weight_kg > 0", name="ck_batch_spring_positive"),
        Index("ix_batch_status_started", "status", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_number: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    raw_yarn_id: Mapped[int] = mapped_column(ForeignKey("raw_inventory.id"), index=True)
    spring_weight_kg: Mapped[float] = mapped_column(Float)
    dye_color: Mapped[str] = mapped_column(String(60))
    status: Mapped[BatchStatus] = mapped_column(
        SAEnum(BatchStatus, name="batch_status"), default=BatchStatus.IN_PROCESS
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    raw_yarn: Mapped[RawInventory] = relationship(back_populates="batches", lazy="joined")
    winding_outputs: Mapped[List["WindingOutput"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )

    @property
    def yarn_count(self) -> str:
        return self.raw_yarn.yarn_count


class WindingOutput(Base):
    __tablename__ = "winding_outputs"
    __table_args__ = (
        CheckConstraint("finished_cones_count >= 0", name="ck_wind_cones_nonneg"),
        CheckConstraint("finished_weight_kg >= 0", name="ck_wind_finished_nonneg"),
        CheckConstraint("winding_waste_kg >= 0", name="ck_wind_waste_nonneg"),
        Index("ix_wind_logged_shift", "logged_at", "shift"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("dyeing_batches.id"), index=True)
    shift: Mapped[Shift] = mapped_column(SAEnum(Shift, name="shift"))
    finished_cones_count: Mapped[int] = mapped_column(Integer)
    finished_weight_kg: Mapped[float] = mapped_column(Float)
    winding_waste_kg: Mapped[float] = mapped_column(Float)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    batch: Mapped[DyeingBatch] = relationship(back_populates="winding_outputs")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_name: Mapped[str] = mapped_column(String(200), index=True)
    industry_type: Mapped[str] = mapped_column(String(100))  # 'Bag Manufacturing', ...
    phone: Mapped[str] = mapped_column(String(30))
    address: Mapped[str] = mapped_column(Text)

    orders: Mapped[List["SalesOrder"]] = relationship(back_populates="customer")


class SalesOrder(Base):
    __tablename__ = "sales_orders"
    __table_args__ = (Index("ix_orders_customer_created", "customer_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    payment_status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus, name="payment_status"), default=PaymentStatus.PENDING
    )
    order_status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, name="order_status"), default=OrderStatus.DRAFT, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    customer: Mapped[Customer] = relationship(back_populates="orders", lazy="joined")
    items: Mapped[List["SalesItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class SalesItem(Base):
    __tablename__ = "sales_items"
    __table_args__ = (
        CheckConstraint("total_cones >= 0", name="ck_item_cones_nonneg"),
        CheckConstraint("total_weight_kg > 0", name="ck_item_weight_positive"),
        CheckConstraint("price_per_kg >= 0", name="ck_item_price_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id"), index=True)
    yarn_count: Mapped[str] = mapped_column(String(16), index=True)
    total_cones: Mapped[int] = mapped_column(Integer)
    total_weight_kg: Mapped[float] = mapped_column(Float)
    price_per_kg: Mapped[float] = mapped_column(Float)

    order: Mapped[SalesOrder] = relationship(back_populates="items")

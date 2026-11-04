"""Pydantic v2 request / response schemas."""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, List, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from models import BatchStatus, OrderStatus, PaymentStatus, Shift
from services import normalize_yarn_count

YarnCount = Annotated[str, AfterValidator(normalize_yarn_count)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------ inventory
class InwardCreate(BaseModel):
    yarn_count: YarnCount = Field(examples=["2/30"])
    mill_source: str = Field(min_length=1, max_length=120)
    weight_kg: float = Field(gt=0, le=1_000_000)
    received_date: Optional[datetime] = None


class RawInventoryOut(ORMModel):
    id: int
    yarn_count: str
    mill_source: str
    initial_weight_kg: float
    current_weight_kg: float
    received_date: datetime


# ------------------------------------------------------------------ batches
class BatchStart(BaseModel):
    batch_number: str = Field(min_length=1, max_length=50)
    spring_weight_kg: float = Field(gt=0, le=1_000_000)
    dye_color: str = Field(default="UNSPECIFIED", min_length=1, max_length=60)
    raw_yarn_id: Optional[int] = Field(default=None, description="Specific lot; omit to auto-pick FIFO by yarn_count")
    yarn_count: Optional[YarnCount] = None

    @model_validator(mode="after")
    def _need_source(self):
        if self.raw_yarn_id is None and not self.yarn_count:
            raise ValueError("Provide raw_yarn_id or yarn_count")
        return self


class BatchOut(ORMModel):
    id: int
    batch_number: str
    raw_yarn_id: int
    yarn_count: str
    spring_weight_kg: float
    dye_color: str
    status: BatchStatus
    started_at: datetime
    completed_at: Optional[datetime] = None


class WindingComplete(BaseModel):
    batch_number: str = Field(min_length=1)
    shift: Shift
    finished_cones_count: int = Field(ge=0)
    finished_weight_kg: float = Field(ge=0)
    winding_waste_kg: float = Field(default=0, ge=0)
    mark_batch_completed: bool = Field(
        default=True, description="Set false when more winding will be logged for this batch (e.g. next shift)"
    )


class WindingOut(ORMModel):
    id: int
    batch_id: int
    shift: Shift
    finished_cones_count: int
    finished_weight_kg: float
    winding_waste_kg: float
    logged_at: datetime


class WindingResult(BaseModel):
    output: WindingOut
    batch_number: str
    yarn_count: str
    spring_weight_kg: float
    cumulative_finished_kg: float
    cumulative_waste_kg: float
    entry_yield_pct: float = Field(description="finished_weight_kg / spring_weight_kg * 100 for this entry")
    batch_yield_pct: float = Field(description="cumulative finished / spring weight * 100")
    batch_status: BatchStatus


# ------------------------------------------------------------------ sales
class CustomerCreate(BaseModel):
    company_name: str = Field(min_length=1, max_length=200)
    industry_type: str = Field(min_length=1, max_length=100, examples=["Bag Manufacturing"])
    phone: str = Field(min_length=5, max_length=30)
    address: str = ""


class CustomerOut(ORMModel):
    id: int
    company_name: str
    industry_type: str
    phone: str
    address: str


class DispatchItem(BaseModel):
    yarn_count: YarnCount
    total_cones: int = Field(ge=0)
    total_weight_kg: float = Field(gt=0)
    price_per_kg: float = Field(ge=0)


class DispatchCreate(BaseModel):
    customer_id: int
    items: List[DispatchItem] = Field(min_length=1)
    payment_status: PaymentStatus = PaymentStatus.PENDING


class SalesItemOut(ORMModel):
    id: int
    yarn_count: str
    total_cones: int
    total_weight_kg: float
    price_per_kg: float


class SalesOrderOut(ORMModel):
    id: int
    order_number: str
    customer: CustomerOut
    items: List[SalesItemOut]
    total_amount: float
    payment_status: PaymentStatus
    order_status: OrderStatus
    created_at: datetime


# ------------------------------------------------------------------ reports / stock
class DailyLossRow(BaseModel):
    yarn_count: str
    shift: Shift
    batches: int
    finished_cones: int
    finished_weight_kg: float
    waste_kg: float
    waste_pct: float = Field(description="waste / (finished + waste) * 100")


class DailyLossReport(BaseModel):
    date: date
    rows: List[DailyLossRow]
    total_finished_kg: float
    total_waste_kg: float
    overall_waste_pct: float


class RawStockRow(BaseModel):
    yarn_count: str
    weight_kg: float
    lots: int


class InProcessRow(BaseModel):
    yarn_count: str
    spring_weight_kg: float
    batches: int


class FinishedStockRow(BaseModel):
    yarn_count: str
    weight_kg: float
    cones: int


class StockSummary(BaseModel):
    raw_stock: List[RawStockRow]
    in_process: List[InProcessRow]
    finished_stock: List[FinishedStockRow]

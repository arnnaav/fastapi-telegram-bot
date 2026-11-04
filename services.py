"""Business logic shared by the FastAPI app and the Telegram bot.

Functions here never commit; the caller owns the transaction
(FastAPI endpoint -> db.commit(), bot -> session_scope()).
"""
from __future__ import annotations

import os
import re
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from models import (
    BatchStatus,
    Customer,
    DyeingBatch,
    OrderStatus,
    PaymentStatus,
    RawInventory,
    SalesItem,
    SalesOrder,
    Shift,
    WindingOutput,
    utcnow,
)

PLANT_TZ = ZoneInfo(os.getenv("PLANT_TZ", "Asia/Kolkata"))
WINDING_TOLERANCE = float(os.getenv("WINDING_TOLERANCE", "0.05"))
EPS = 1e-6
DISPATCH_LOCK_KEY = 874_201  # pg advisory lock: serialises stock-checking dispatches


# --------------------------------------------------------------------------- errors
class ServiceError(Exception):
    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFoundError(ServiceError):
    status_code = 404


class ConflictError(ServiceError):
    status_code = 409


class ValidationFailed(ServiceError):
    status_code = 422


# --------------------------------------------------------------------------- helpers
_COUNT_RE = re.compile(r"^\s*(\d{1,2})\s*[/xX\-]\s*(\d{1,3})\s*$")


def normalize_yarn_count(value: str) -> str:
    m = _COUNT_RE.match(value or "")
    if not m:
        raise ValidationFailed(f"Invalid yarn count '{value}'. Expected a format like 2/30.")
    return f"{int(m.group(1))}/{int(m.group(2))}"


def parse_shift(value: str | Shift) -> Shift:
    if isinstance(value, Shift):
        return value
    v = (value or "").strip().upper()
    if v in {"MORNING", "DAY", "M", "SUBAH", "SUBH", "MORN"}:
        return Shift.MORNING
    if v in {"NIGHT", "N", "RAAT", "RAT"}:
        return Shift.NIGHT
    raise ValidationFailed(f"Unknown shift '{value}'. Use MORNING or NIGHT.")


def r3(x: float) -> float:
    return round(float(x), 3)


def _positive(name: str, value: float) -> float:
    if value is None or value <= 0:
        raise ValidationFailed(f"{name} must be greater than 0.")
    return r3(value)


def _nonneg(name: str, value: float) -> float:
    if value is None or value < 0:
        raise ValidationFailed(f"{name} cannot be negative.")
    return r3(value)


def yield_pct(finished_kg: float, spring_kg: float) -> float:
    """Yield % = finished_weight_kg / spring_weight_kg * 100"""
    return round(finished_kg / spring_kg * 100, 2) if spring_kg else 0.0


def day_bounds_utc(day: Optional[date] = None) -> tuple[datetime, datetime, date]:
    """[start, end) of a plant-local calendar day, expressed in UTC."""
    day = day or datetime.now(PLANT_TZ).date()
    start = datetime.combine(day, time.min, tzinfo=PLANT_TZ)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc), day


# --------------------------------------------------------------------------- inventory
def inward_yarn(
    db: Session,
    *,
    yarn_count: str,
    weight_kg: float,
    mill_source: str,
    received_date: Optional[datetime] = None,
) -> RawInventory:
    w = _positive("weight_kg", weight_kg)
    mill = (mill_source or "").strip()
    if not mill:
        raise ValidationFailed("mill_source is required.")
    lot = RawInventory(
        yarn_count=normalize_yarn_count(yarn_count),
        mill_source=mill,
        initial_weight_kg=w,
        current_weight_kg=w,
        received_date=received_date or utcnow(),
    )
    db.add(lot)
    db.flush()
    return lot


def list_raw_inventory(db: Session, only_available: bool = True) -> list[RawInventory]:
    stmt = select(RawInventory).order_by(RawInventory.received_date, RawInventory.id)
    if only_available:
        stmt = stmt.where(RawInventory.current_weight_kg > EPS)
    return list(db.scalars(stmt))


# --------------------------------------------------------------------------- batches
def get_batch(db: Session, batch_number: str, for_update: bool = False) -> DyeingBatch:
    stmt = select(DyeingBatch).where(DyeingBatch.batch_number == batch_number.strip())
    if for_update:
        stmt = stmt.with_for_update(of=DyeingBatch)
    batch = db.scalars(stmt).unique().first()
    if not batch:
        raise NotFoundError(f"Batch '{batch_number}' not found.")
    return batch


def start_batch(
    db: Session,
    *,
    batch_number: str,
    spring_weight_kg: float,
    dye_color: str = "UNSPECIFIED",
    raw_yarn_id: Optional[int] = None,
    yarn_count: Optional[str] = None,
) -> DyeingBatch:
    """Deduct yarn from a raw lot and open a dyeing batch.

    A batch is tied to ONE raw lot (raw_yarn_id FK). If only yarn_count is given,
    the oldest lot (FIFO) that can cover the whole spring weight is used.
    """
    number = (batch_number or "").strip()
    if not number:
        raise ValidationFailed("batch_number is required.")
    w = _positive("spring_weight_kg", spring_weight_kg)
    if raw_yarn_id is None and not yarn_count:
        raise ValidationFailed("Provide raw_yarn_id or yarn_count.")
    if db.scalar(select(DyeingBatch.id).where(DyeingBatch.batch_number == number)):
        raise ConflictError(f"Batch number '{number}' already exists.")

    yc = normalize_yarn_count(yarn_count) if yarn_count else None

    if raw_yarn_id is not None:
        lot = db.get(RawInventory, raw_yarn_id, with_for_update=True)
        if not lot:
            raise NotFoundError(f"Raw yarn lot {raw_yarn_id} not found.")
        if yc and lot.yarn_count != yc:
            raise ValidationFailed(f"Lot {raw_yarn_id} is {lot.yarn_count}, not {yc}.")
        if lot.current_weight_kg + EPS < w:
            raise ConflictError(
                f"Lot {raw_yarn_id} has only {lot.current_weight_kg:.2f} kg; need {w:.2f} kg."
            )
    else:
        lot = db.scalars(
            select(RawInventory)
            .where(RawInventory.yarn_count == yc, RawInventory.current_weight_kg >= w - EPS)
            .order_by(RawInventory.received_date, RawInventory.id)
            .limit(1)
            .with_for_update()
        ).first()
        if not lot:
            total, biggest = db.execute(
                select(
                    func.coalesce(func.sum(RawInventory.current_weight_kg), 0.0),
                    func.coalesce(func.max(RawInventory.current_weight_kg), 0.0),
                ).where(RawInventory.yarn_count == yc)
            ).one()
            raise ConflictError(
                f"No single {yc} lot has {w:.2f} kg. Total {yc} in stock: {total:.2f} kg "
                f"(largest lot {biggest:.2f} kg). Split the batch or inward more yarn."
            )

    lot.current_weight_kg = max(0.0, r3(lot.current_weight_kg - w))
    batch = DyeingBatch(
        batch_number=number,
        raw_yarn_id=lot.id,
        spring_weight_kg=w,
        dye_color=(dye_color or "UNSPECIFIED").strip() or "UNSPECIFIED",
        status=BatchStatus.IN_PROCESS,
        started_at=utcnow(),
    )
    batch.raw_yarn = lot
    db.add(batch)
    db.flush()
    return batch


def complete_winding(
    db: Session,
    *,
    batch_number: str,
    shift: str | Shift,
    finished_cones_count: int,
    finished_weight_kg: float,
    winding_waste_kg: float = 0.0,
    mark_batch_completed: bool = True,
) -> dict:
    """Record a winding output and compute yield.

    A batch may be wound over several shifts: pass mark_batch_completed=False for
    partial entries. Yield is reported per entry and cumulatively for the batch.
    """
    sh = parse_shift(shift)
    if finished_cones_count is None or finished_cones_count < 0:
        raise ValidationFailed("finished_cones_count cannot be negative.")
    fin = _nonneg("finished_weight_kg", finished_weight_kg)
    waste = _nonneg("winding_waste_kg", winding_waste_kg)

    batch = get_batch(db, batch_number, for_update=True)
    if batch.status != BatchStatus.IN_PROCESS:
        raise ConflictError(f"Batch '{batch.batch_number}' is {batch.status.value}; cannot add winding.")

    prev_fin, prev_waste = db.execute(
        select(
            func.coalesce(func.sum(WindingOutput.finished_weight_kg), 0.0),
            func.coalesce(func.sum(WindingOutput.winding_waste_kg), 0.0),
        ).where(WindingOutput.batch_id == batch.id)
    ).one()
    cum_fin, cum_waste = r3(prev_fin + fin), r3(prev_waste + waste)
    ceiling = batch.spring_weight_kg * (1 + WINDING_TOLERANCE)
    if cum_fin + cum_waste > ceiling + EPS:
        raise ValidationFailed(
            f"Finished {cum_fin:.2f} + waste {cum_waste:.2f} kg exceeds spring weight "
            f"{batch.spring_weight_kg:.2f} kg (+{WINDING_TOLERANCE:.0%} tolerance). Check the numbers."
        )

    out = WindingOutput(
        batch_id=batch.id,
        shift=sh,
        finished_cones_count=finished_cones_count,
        finished_weight_kg=fin,
        winding_waste_kg=waste,
        logged_at=utcnow(),
    )
    db.add(out)
    if mark_batch_completed:
        batch.status = BatchStatus.COMPLETED
        batch.completed_at = utcnow()
    db.flush()

    return {
        "output": out,
        "batch_number": batch.batch_number,
        "yarn_count": batch.yarn_count,
        "spring_weight_kg": batch.spring_weight_kg,
        "cumulative_finished_kg": cum_fin,
        "cumulative_waste_kg": cum_waste,
        "entry_yield_pct": yield_pct(fin, batch.spring_weight_kg),
        "batch_yield_pct": yield_pct(cum_fin, batch.spring_weight_kg),
        "batch_status": batch.status,
    }


def cancel_batch(db: Session, batch_number: str) -> DyeingBatch:
    batch = get_batch(db, batch_number, for_update=True)
    if batch.status != BatchStatus.IN_PROCESS:
        raise ConflictError(f"Batch is {batch.status.value}; only IN_PROCESS batches can be cancelled.")
    if db.scalar(select(func.count(WindingOutput.id)).where(WindingOutput.batch_id == batch.id)):
        raise ConflictError("Batch already has winding output; it cannot be cancelled.")
    lot = db.get(RawInventory, batch.raw_yarn_id, with_for_update=True)
    lot.current_weight_kg = r3(lot.current_weight_kg + batch.spring_weight_kg)
    batch.status = BatchStatus.CANCELLED
    batch.completed_at = utcnow()
    db.flush()
    return batch


# --------------------------------------------------------------------------- stock
def finished_stock_by_count(db: Session) -> dict[str, dict]:
    """Finished goods = total wound output minus dispatched sales items, per count."""
    produced = db.execute(
        select(
            RawInventory.yarn_count,
            func.coalesce(func.sum(WindingOutput.finished_weight_kg), 0.0),
            func.coalesce(func.sum(WindingOutput.finished_cones_count), 0),
        )
        .select_from(WindingOutput)
        .join(DyeingBatch, WindingOutput.batch_id == DyeingBatch.id)
        .join(RawInventory, DyeingBatch.raw_yarn_id == RawInventory.id)
        .group_by(RawInventory.yarn_count)
    ).all()
    sold = {
        c: (w, n)
        for c, w, n in db.execute(
            select(
                SalesItem.yarn_count,
                func.coalesce(func.sum(SalesItem.total_weight_kg), 0.0),
                func.coalesce(func.sum(SalesItem.total_cones), 0),
            )
            .join(SalesOrder, SalesItem.order_id == SalesOrder.id)
            .where(SalesOrder.order_status == OrderStatus.DISPATCHED)
            .group_by(SalesItem.yarn_count)
        ).all()
    }
    stock: dict[str, dict] = {}
    for count, w, n in produced:
        sw, sn = sold.get(count, (0.0, 0))
        stock[count] = {"weight_kg": r3(w - sw), "cones": int(n - sn)}
    return stock


def get_stock_summary(db: Session) -> dict:
    raw = db.execute(
        select(
            RawInventory.yarn_count,
            func.sum(RawInventory.current_weight_kg),
            func.count(RawInventory.id),
        )
        .where(RawInventory.current_weight_kg > EPS)
        .group_by(RawInventory.yarn_count)
        .order_by(RawInventory.yarn_count)
    ).all()
    wip = db.execute(
        select(
            RawInventory.yarn_count,
            func.sum(DyeingBatch.spring_weight_kg),
            func.count(DyeingBatch.id),
        )
        .join(RawInventory, DyeingBatch.raw_yarn_id == RawInventory.id)
        .where(DyeingBatch.status == BatchStatus.IN_PROCESS)
        .group_by(RawInventory.yarn_count)
        .order_by(RawInventory.yarn_count)
    ).all()
    fin = finished_stock_by_count(db)
    return {
        "raw_stock": [{"yarn_count": c, "weight_kg": r3(w), "lots": n} for c, w, n in raw],
        "in_process": [{"yarn_count": c, "spring_weight_kg": r3(w), "batches": n} for c, w, n in wip],
        "finished_stock": [
            {"yarn_count": c, **v} for c, v in sorted(fin.items()) if v["weight_kg"] > EPS or v["cones"] > 0
        ],
    }


# --------------------------------------------------------------------------- customers / sales
def create_customer(db: Session, *, company_name: str, industry_type: str, phone: str, address: str) -> Customer:
    c = Customer(company_name=company_name.strip(), industry_type=industry_type.strip(),
                 phone=phone.strip(), address=address.strip())
    db.add(c)
    db.flush()
    return c


def dispatch_order(
    db: Session,
    *,
    customer_id: int,
    items: list[dict],
    payment_status: PaymentStatus = PaymentStatus.PENDING,
) -> SalesOrder:
    """Create a dispatched invoice, validating against finished stock."""
    if not items:
        raise ValidationFailed("At least one item is required.")
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": DISPATCH_LOCK_KEY})

    customer = db.get(Customer, customer_id)
    if not customer:
        raise NotFoundError(f"Customer {customer_id} not found.")

    clean: list[dict] = []
    need: dict[str, list] = defaultdict(lambda: [0.0, 0])
    for it in items:
        yc = normalize_yarn_count(it["yarn_count"])
        wt = _positive("total_weight_kg", it["total_weight_kg"])
        cones = int(it["total_cones"])
        if cones < 0:
            raise ValidationFailed("total_cones cannot be negative.")
        price = _nonneg("price_per_kg", it["price_per_kg"])
        clean.append({"yarn_count": yc, "total_weight_kg": wt, "total_cones": cones, "price_per_kg": price})
        need[yc][0] += wt
        need[yc][1] += cones

    stock = finished_stock_by_count(db)
    for yc, (wt, cones) in need.items():
        have = stock.get(yc, {"weight_kg": 0.0, "cones": 0})
        if wt > have["weight_kg"] + EPS or cones > have["cones"]:
            raise ConflictError(
                f"Insufficient finished stock for {yc}: requested {wt:.2f} kg / {cones} cones, "
                f"available {have['weight_kg']:.2f} kg / {have['cones']} cones."
            )

    now = utcnow()
    order = SalesOrder(
        order_number=f"INV-{now.astimezone(PLANT_TZ):%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
        customer_id=customer.id,
        payment_status=payment_status,
        order_status=OrderStatus.DISPATCHED,
        created_at=now,
        total_amount=0.0,
    )
    order.customer = customer
    for it in clean:
        order.items.append(SalesItem(**it))
    order.total_amount = round(sum(i["total_weight_kg"] * i["price_per_kg"] for i in clean), 2)
    db.add(order)
    db.flush()
    return order


# --------------------------------------------------------------------------- reports
def daily_loss_report(db: Session, day: Optional[date] = None, yarn_count: Optional[str] = None) -> dict:
    start, end, day = day_bounds_utc(day)
    stmt = (
        select(
            RawInventory.yarn_count,
            WindingOutput.shift,
            func.sum(WindingOutput.finished_weight_kg),
            func.sum(WindingOutput.winding_waste_kg),
            func.sum(WindingOutput.finished_cones_count),
            func.count(func.distinct(WindingOutput.batch_id)),
        )
        .select_from(WindingOutput)
        .join(DyeingBatch, WindingOutput.batch_id == DyeingBatch.id)
        .join(RawInventory, DyeingBatch.raw_yarn_id == RawInventory.id)
        .where(WindingOutput.logged_at >= start, WindingOutput.logged_at < end)
        .group_by(RawInventory.yarn_count, WindingOutput.shift)
        .order_by(RawInventory.yarn_count, WindingOutput.shift)
    )
    if yarn_count:
        stmt = stmt.where(RawInventory.yarn_count == normalize_yarn_count(yarn_count))

    rows, tot_f, tot_w = [], 0.0, 0.0
    for count, shift, fin, waste, cones, nb in db.execute(stmt).all():
        fin, waste = float(fin or 0), float(waste or 0)
        tot_f, tot_w = tot_f + fin, tot_w + waste
        rows.append({
            "yarn_count": count,
            "shift": shift,
            "batches": nb,
            "finished_cones": int(cones or 0),
            "finished_weight_kg": r3(fin),
            "waste_kg": r3(waste),
            "waste_pct": round(waste / (fin + waste) * 100, 2) if fin + waste else 0.0,
        })
    return {
        "date": day,
        "rows": rows,
        "total_finished_kg": r3(tot_f),
        "total_waste_kg": r3(tot_w),
        "overall_waste_pct": round(tot_w / (tot_f + tot_w) * 100, 2) if tot_f + tot_w else 0.0,
    }

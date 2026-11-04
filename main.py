"""FastAPI app: inventory, dyeing batches, winding/yield, B2B dispatch, loss reports, and Telegram bot webhook.

Run:  uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
import google.generativeai as genai
import telegram

import services
from database import engine, get_db
from models import Base
from schemas import (
    BatchOut, BatchStart, CustomerCreate, CustomerOut, DailyLossReport, DispatchCreate,
    InwardCreate, RawInventoryOut, SalesOrderOut, StockSummary, WindingComplete, WindingResult,
)

log = logging.getLogger("yarn_erp")
API_KEY = os.getenv("API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

telegram_bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.getenv("AUTO_CREATE_TABLES", "1") == "1":
        Base.metadata.create_all(engine)  # use Alembic migrations in real production
    yield


app = FastAPI(title="Yarn Dyeing & Winding ERP & Telegram Bot", version="1.0.0", lifespan=lifespan)


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if API_KEY and not (x_api_key and hmac.compare_digest(x_api_key, API_KEY)):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


@app.exception_handler(services.ServiceError)
async def service_error_handler(_: Request, exc: services.ServiceError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(IntegrityError)
async def integrity_error_handler(_: Request, exc: IntegrityError):
    log.warning("Integrity error: %s", exc.orig)
    return JSONResponse(status_code=409, content={"detail": "Duplicate or conflicting record."})


@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------------------------ Telegram Webhook
@app.post("/webhook")
async def telegram_webhook(request: Request):
    if not telegram_bot:
        return {"status": "bot token not configured"}
    
    try:
        data = await request.json()
        update = telegram.Update.de_json(data, telegram_bot)
        
        if update and update.message and update.message.text:
            chat_id = update.message.chat_id
            user_text = update.message.text
            
            # Generate intelligent response using Gemini AI
            response_text = "Hello! I am your ERP assistant."
            if GEMINI_API_KEY:
                try:
                    # Using gemini-2.5-flash or standard generative model
                    model = genai.GenerativeModel("gemini-1.5-flash")
                    ai_response = model.generate_content(user_text)
                    if ai_response and ai_response.text:
                        response_text = ai_response.text
                except Exception as e:
                    log.error("Gemini AI error: %s", e)
                    response_text = f"Received your message, but encountered an AI error: {e}"
            
            await telegram_bot.send_message(chat_id=chat_id, text=response_text)
            
        return {"status": "ok"}
    except Exception as e:
        log.error("Webhook error: %s", e)
        return {"status": "error", "detail": str(e)}


api = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])


# ------------------------------------------------------------------ customers
@api.post("/customers", response_model=CustomerOut, status_code=201)
def create_customer(body: CustomerCreate, db: Session = Depends(get_db)):
    c = services.create_customer(db, **body.model_dump())
    db.commit()
    return c


# ------------------------------------------------------------------ inventory
@api.post("/inventory/inward", response_model=RawInventoryOut, status_code=201)
def inward(body: InwardCreate, db: Session = Depends(get_db)):
    lot = services.inward_yarn(
        db, yarn_count=body.yarn_count, weight_kg=body.weight_kg,
        mill_source=body.mill_source, received_date=body.received_date,
    )
    db.commit()
    return lot


@api.get("/inventory", response_model=List[RawInventoryOut])
def list_inventory(only_available: bool = True, db: Session = Depends(get_db)):
    return services.list_raw_inventory(db, only_available)


@api.get("/stock/summary", response_model=StockSummary)
def stock_summary(db: Session = Depends(get_db)):
    return services.get_stock_summary(db)


# ------------------------------------------------------------------ batches
@api.post("/batches/start", response_model=BatchOut, status_code=201)
def start_batch(body: BatchStart, db: Session = Depends(get_db)):
    batch = services.start_batch(
        db, batch_number=body.batch_number, spring_weight_kg=body.spring_weight_kg,
        dye_color=body.dye_color, raw_yarn_id=body.raw_yarn_id, yarn_count=body.yarn_count,
    )
    db.commit()
    return batch


@api.post("/batches/complete-winding", response_model=WindingResult, status_code=201)
def complete_winding(body: WindingComplete, db: Session = Depends(get_db)):
    result = services.complete_winding(
        db, batch_number=body.batch_number, shift=body.shift,
        finished_cones_count=body.finished_cones_count,
        finished_weight_kg=body.finished_weight_kg,
        winding_waste_kg=body.winding_waste_kg,
        mark_batch_completed=body.mark_batch_completed,
    )
    db.commit()
    return result


@api.post("/batches/{batch_number}/cancel", response_model=BatchOut)
def cancel_batch(batch_number: str, db: Session = Depends(get_db)):
    batch = services.cancel_batch(db, batch_number)
    db.commit()
    return batch


@api.get("/batches/{batch_number}", response_model=BatchOut)
def get_batch(batch_number: str, db: Session = Depends(get_db)):
    return services.get_batch(db, batch_number)


# ------------------------------------------------------------------ sales
@api.post("/sales/dispatch", response_model=SalesOrderOut, status_code=201)
def dispatch(body: DispatchCreate, db: Session = Depends(get_db)):
    order = services.dispatch_order(
        db, customer_id=body.customer_id,
        items=[i.model_dump() for i in body.items],
        payment_status=body.payment_status,
    )
    db.commit()
    return order


# ------------------------------------------------------------------ reports
@api.get("/reports/daily-loss", response_model=DailyLossReport)
def daily_loss(
    report_date: Optional[date] = Query(default=None, alias="date", description="Plant-local day; default today"),
    yarn_count: Optional[str] = None,
    db: Session = Depends(get_db),
):
    return services.daily_loss_report(db, report_date, yarn_count)


app.include_router(api)
@app.post("/webhook")
async def telegram_webhook(request: Request):
    if not telegram_bot:
        return {"status": "bot token not configured"}
    
    try:
        data = await request.json()
        log.info("Received webhook data: %s", data)
        
        # Extract message safely from Telegram update dict
        message = data.get("message") or data.get("edited_message")
        if not message:
            return {"status": "no message found"}
            
        chat_id = message["chat"]["id"]
        user_text = message.get("text", "")
        
        response_text = "Hello! I received your message."
        if user_text and GEMINI_API_KEY:
            try:
                model = genai.GenerativeModel("gemini-1.5-flash")
                ai_response = model.generate_content(user_text)
                if ai_response and ai_response.text:
                    response_text = ai_response.text
            except Exception as ai_err:
                log.error("Gemini AI generation error: %s", ai_err)
                response_text = "Sorry, I encountered an error generating an AI response."
        
        # Send reply back via Telegram API
        await telegram_bot.send_message(chat_id=chat_id, text=response_text)
        return {"status": "ok"}
        
    except Exception as e:
        log.error("Webhook processing error: %s", e)
        return {"status": "error", "detail": str(e)}
import requests

@app.post("/webhook")
async def telegram_webhook(request: Request):
    if not TELEGRAM_BOT_TOKEN:
        return {"status": "bot token not configured"}
    
    try:
        data = await request.json()
        log.info("Received webhook data: %s", data)
        
        # Extract message safely from incoming JSON
        message = data.get("message") or data.get("edited_message")
        if not message:
            return {"status": "no message found"}
            
        chat_id = message["chat"]["id"]
        user_text = message.get("text", "")
        
        response_text = "Hello! I am your ERP & AI assistant."
        
        if user_text and GEMINI_API_KEY:
            try:
                model = genai.GenerativeModel("gemini-1.5-flash")
                ai_response = model.generate_content(user_text)
                if ai_response and ai_response.text:
                    response_text = ai_response.text
            except Exception as ai_err:
                log.error("Gemini AI generation error: %s", ai_err)
                response_text = f"AI Error: {ai_err}"
        
        # Send response directly via Telegram HTTP API
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": response_text}
        r = requests.post(url, json=payload)
        log.info("Telegram send response: %s", r.text)
        
        return {"status": "ok"}
        
    except Exception as e:
        log.error("Webhook processing error: %s", e)
        return {"status": "error", "detail": str(e)}
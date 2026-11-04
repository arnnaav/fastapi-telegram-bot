"""Telegram shop-floor assistant: English / Hindi / Hinglish -> Claude tool use -> DB.

Run:  python telegram_bot.py
Env:  TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, ALLOWED_TELEGRAM_IDS, DATABASE_URL (see .env.example)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict, deque
from datetime import datetime

import anthropic
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from sqlalchemy import func, select
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

try:  # decorator is exported at top level in recent SDKs
    from anthropic import beta_async_tool
except ImportError:  # pragma: no cover
    from anthropic.lib.tools import beta_async_tool

import services
from database import session_scope
from models import RawInventory

load_dotenv()
logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("yarn_bot")

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
ALLOWED_IDS = {int(x) for x in os.getenv("ALLOWED_TELEGRAM_IDS", "").replace(" ", "").split(",") if x}
MORNING_START = int(os.getenv("MORNING_SHIFT_START", "6"))
NIGHT_START = int(os.getenv("NIGHT_SHIFT_START", "18"))
HISTORY_TURNS = 4  # user+assistant pairs remembered per chat, for short follow-ups

claude = AsyncAnthropic()  # reads ANTHROPIC_API_KEY

BASE_PROMPT = (
    "You are the smart factory operational assistant for a yarn dyeing & winding facility. "
    "Factory workers on the shop floor will text or send natural audio transcriptions in English, "
    "Hindi, or Hinglish (e.g., 'Aaj 150kg 2/30 yarn receive hua Bhilwara mill se'). Extract structured "
    "parameters, invoke the correct DB tool, and return a crisp 2-line confirmation message with emojis."
)

RULES = """
Operating rules:
- Reply in the worker's language/script (Hinglish in -> Hinglish out). Exactly 2 short lines, with emojis. No markdown.
- Yarn counts look like 2/30 or 2/50 etc. Weights are in kg. Convert spoken numbers ("dedh sau" = 150, "dhai sau" = 250).
- Shift: subah/morning/day = MORNING, raat/night = NIGHT. If the worker does not say it, use the shift implied by the
  current time below and mention it in line 2.
- Winding: if the worker says more winding is still pending for that batch ("baaki hai", "partial"), pass
  is_final_entry=false; otherwise true. Waste defaults to 0 only if the worker clearly says there was none; if waste
  is not mentioned at all, ask for it instead of guessing.
- NEVER invent a batch number, weight, count or mill. If a required value is missing or ambiguous, ask ONE short
  question and do not call a tool.
- If a tool returns an error, tell the worker what went wrong in plain words (2 lines max) and what to fix. Nothing was saved.
- Stock questions ("kitna stock hai?") -> get_stock_summary.
- For winding confirmations include the batch yield % returned by the tool.
- Only handle factory logging/stock. Politely decline anything else in one line.
"""


def system_prompt() -> str:
    now = datetime.now(services.PLANT_TZ)
    shift = "MORNING" if MORNING_START <= now.hour < NIGHT_START else "NIGHT"
    return f"{BASE_PROMPT}\n{RULES}\nCurrent plant time: {now:%Y-%m-%d %H:%M} ({services.PLANT_TZ.key}); current shift: {shift}."


# --------------------------------------------------------------------------- tool plumbing
async def _run_db(fn) -> str:
    """Run a sync DB function in a worker thread inside one transaction; return JSON for Claude."""

    def _inner():
        with session_scope() as db:
            return fn(db)

    try:
        return json.dumps(await asyncio.to_thread(_inner), default=str)
    except services.ServiceError as e:
        return json.dumps({"error": e.message, "saved": False})
    except Exception:  # noqa: BLE001
        log.exception("tool execution failed")
        return json.dumps({"error": "Internal database error. Nothing was saved.", "saved": False})


@beta_async_tool
async def log_inward_yarn(yarn_count: str, weight_kg: float, mill: str) -> str:
    """Log a delivery of raw greige yarn received from a mill into raw inventory.

    Args:
        yarn_count: Yarn count such as '2/30' or '2/50'.
        weight_kg: Received net weight in kilograms.
        mill: Name of the supplying mill, e.g. 'Bhilwara'.
    """

    def op(db):
        lot = services.inward_yarn(db, yarn_count=yarn_count, weight_kg=weight_kg, mill_source=mill)
        total = db.scalar(
            select(func.sum(RawInventory.current_weight_kg)).where(RawInventory.yarn_count == lot.yarn_count)
        )
        return {"saved": True, "lot_id": lot.id, "yarn_count": lot.yarn_count,
                "weight_kg": lot.initial_weight_kg, "mill": lot.mill_source,
                "total_raw_stock_of_this_count_kg": services.r3(total or 0)}

    return await _run_db(op)


@beta_async_tool
async def start_dyeing(batch_number: str, yarn_count: str, weight_kg: float, dye_color: str = "UNSPECIFIED") -> str:
    """Start a dyeing batch: rolls raw yarn onto springs and deducts it from raw inventory (oldest lot first).

    Args:
        batch_number: Unique batch identifier exactly as spoken/typed by the worker, e.g. 'B-1042'.
        yarn_count: Yarn count of the raw yarn used, e.g. '2/30'.
        weight_kg: Total spring weight loaded for dyeing, in kilograms.
        dye_color: Dye colour/shade if mentioned (e.g. 'Navy Blue'); otherwise leave as 'UNSPECIFIED'.
    """

    def op(db):
        b = services.start_batch(db, batch_number=batch_number, spring_weight_kg=weight_kg,
                                 dye_color=dye_color, yarn_count=yarn_count)
        return {"saved": True, "batch_number": b.batch_number, "yarn_count": b.yarn_count,
                "spring_weight_kg": b.spring_weight_kg, "dye_color": b.dye_color,
                "lot_remaining_kg": b.raw_yarn.current_weight_kg}

    return await _run_db(op)


@beta_async_tool
async def log_winding_output(
    batch_number: str, shift: str, cones: int, weight_kg: float, waste_kg: float, is_final_entry: bool = True
) -> str:
    """Record finished cones from winding for a dyeing batch and compute batch yield %.

    Args:
        batch_number: The dyeing batch this output belongs to.
        shift: 'MORNING' or 'NIGHT'.
        cones: Number of finished cones produced.
        weight_kg: Finished net weight in kilograms.
        waste_kg: Winding waste in kilograms.
        is_final_entry: True if winding of this batch is now finished; False if more output will be logged later.
    """

    def op(db):
        r = services.complete_winding(
            db, batch_number=batch_number, shift=shift, finished_cones_count=cones,
            finished_weight_kg=weight_kg, winding_waste_kg=waste_kg, mark_batch_completed=is_final_entry,
        )
        return {"saved": True, "batch_number": r["batch_number"], "yarn_count": r["yarn_count"],
                "shift": r["output"].shift.value, "cones": r["output"].finished_cones_count,
                "finished_kg": r["output"].finished_weight_kg, "waste_kg": r["output"].winding_waste_kg,
                "entry_yield_pct": r["entry_yield_pct"], "batch_yield_pct": r["batch_yield_pct"],
                "batch_status": r["batch_status"].value}

    return await _run_db(op)


@beta_async_tool
async def get_stock_summary() -> str:
    """Get current stock: raw yarn by count, batches in dyeing, and finished cones/kg ready to sell, by count."""
    return await _run_db(lambda db: services.get_stock_summary(db))


TOOLS = [log_inward_yarn, start_dyeing, log_winding_output, get_stock_summary]


# --------------------------------------------------------------------------- Telegram handlers
histories: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_TURNS * 2))
chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def authorised(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id in ALLOWED_IDS


async def deny(update: Update) -> None:
    uid = update.effective_user.id if update.effective_user else "unknown"
    await update.effective_message.reply_text(
        f"⛔ Not authorised. Ask the admin to add your Telegram ID: {uid}"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorised(update):
        return await deny(update)
    await update.message.reply_text(
        "🏭 Factory assistant ready.\n"
        "Likho jaise: 'Aaj 150kg 2/30 yarn Bhilwara mill se aaya' ya 'Batch B-12 winding: 80 cones, 96kg, waste 1.2kg raat'.\n"
        "/stock = current stock"
    )


async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorised(update):
        return await deny(update)
    data = json.loads(await _run_db(lambda db: services.get_stock_summary(db)))
    if "error" in data:
        return await update.message.reply_text(f"⚠️ {data['error']}")
    raw = ", ".join(f"{r['yarn_count']}: {r['weight_kg']:.0f}kg" for r in data["raw_stock"]) or "—"
    wip = ", ".join(f"{r['yarn_count']}: {r['spring_weight_kg']:.0f}kg" for r in data["in_process"]) or "—"
    fin = ", ".join(f"{r['yarn_count']}: {r['weight_kg']:.0f}kg/{r['cones']} cones" for r in data["finished_stock"]) or "—"
    await update.message.reply_text(f"📦 Raw: {raw}\n🧪 Dyeing: {wip}\n🧵 Finished: {fin}")


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorised(update):
        return await deny(update)
    await update.message.reply_text(
        "🎤 Voice note abhi process nahi hota. Telegram ke transcribe option se text bhejein ya type karein. ✍️"
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not authorised(update):
        return await deny(update)

    chat_id = update.effective_chat.id
    user_text = update.message.text.strip()
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

    async with chat_locks[chat_id]:  # one message at a time per chat -> ordered writes & history
        messages = list(histories[chat_id]) + [{"role": "user", "content": user_text}]
        try:
            runner = claude.beta.messages.tool_runner(  # async client: returns an async runner
                model=MODEL,
                max_tokens=1024,
                system=system_prompt(),
                tools=TOOLS,
                messages=messages,
            )
            final = await runner.until_done()
            reply = "".join(b.text for b in final.content if b.type == "text").strip()
        except anthropic.APIError:
            log.exception("Anthropic API error")
            return await update.message.reply_text("⚠️ AI service busy hai, 1 minute baad dobara bhejein.")
        except Exception:  # noqa: BLE001
            log.exception("Unexpected bot failure")
            return await update.message.reply_text("⚠️ Kuch gadbad hui. Data check karke dobara try karein.")

        reply = reply or "✅ Done."
        histories[chat_id].extend(
            [{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}]
        )
    await update.message.reply_text(reply)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled telegram error", exc_info=context.error)


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    if not ALLOWED_IDS:
        log.warning("ALLOWED_TELEGRAM_IDS is empty: every user will be refused (their ID is shown in the reply).")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    log.info("Bot started (model=%s)", MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

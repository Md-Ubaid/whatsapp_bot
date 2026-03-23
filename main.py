# main.py
from fastapi import FastAPI, Request, Query, HTTPException
from database import database
from config import VERIFY_TOKEN
from db_helpers import is_user_registered, get_session
import logging
from bot_logic import (
    send_main_menu,
    handle_button_click,
    handle_list_selection,
    handle_text_message,
    send_not_registered,
)

app = FastAPI()
logger = logging.getLogger(__name__)


@app.on_event("startup")
async def startup():
    await database.connect()
    print("✅ Connected to Neon database")


@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()


@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


# ── Deduplication helpers ─────────────────────────────────────────────────────

async def is_already_processed(message_id: str) -> bool:
    """Returns True if we've seen this message ID before."""
    row = await database.fetch_one(
        "SELECT message_id FROM processed_messages WHERE message_id = :mid",
        {"mid": message_id}
    )
    return row is not None


async def mark_as_processed(message_id: str):
    """Record this message ID so retries are ignored."""
    await database.execute(
        """INSERT INTO processed_messages (message_id)
           VALUES (:mid)
           ON CONFLICT (message_id) DO NOTHING""",
        {"mid": message_id}
    )


async def cleanup_old_messages():
    """Delete processed message records older than 24 hours."""
    await database.execute(
        """DELETE FROM processed_messages
           WHERE processed_at < NOW() - INTERVAL '24 hours'"""
    )


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def receive_message(request: Request):
    body = await request.json()
    logger.info(f"Incoming payload: {body}")

    try:
        entry   = body["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]

        # Ignore delivery receipts and status updates
        if "messages" not in value:
            return {"status": "ok"}

        message  = value["messages"][0]
        phone    = message["from"]
        msg_type = message["type"]

        # ── DEDUPLICATION CHECK ──────────────────────────────────────────────
        # Every WhatsApp message has a unique ID. If we've seen it, skip it.
        message_id = message.get("id", "")
        if message_id:
            if await is_already_processed(message_id):
                logger.info(f"Duplicate message ignored: {message_id}")
                return {"status": "ok"}
            await mark_as_processed(message_id)

        # Clean up old records occasionally (1 in 20 chance per request)
        import random
        if random.randint(1, 20) == 1:
            await cleanup_old_messages()

        # ── REGISTRATION CHECK ───────────────────────────────────────────────
        registered = await is_user_registered(phone)
        if not registered:
            await send_not_registered(phone)
            return {"status": "ok"}

        # ── SESSION CHECK ────────────────────────────────────────────────────
        session = await get_session(phone)

        # ── ROUTE MESSAGE ────────────────────────────────────────────────────
        if msg_type == "interactive":
            interactive_type = message["interactive"]["type"]

            if interactive_type == "button_reply":
                button_id = message["interactive"]["button_reply"]["id"]
                await handle_button_click(phone, button_id, session)

            elif interactive_type == "list_reply":
                row_id = message["interactive"]["list_reply"]["id"]
                await handle_list_selection(phone, row_id, session)

        elif msg_type == "text":
            text = message["text"]["body"]
            await handle_text_message(phone, text, session)

    except (KeyError, IndexError) as e:
        logger.error(f"Payload parsing error: {e}")

    return {"status": "ok"}
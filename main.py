import random
import logging
import hmac
import hashlib

from fastapi import FastAPI, Request, Query, HTTPException, BackgroundTasks
from database import database
from config import VERIFY_TOKEN, WHATSAPP_APP_SECRET
from db_helpers import is_user_registered, get_session
from bot_logic import (
    send_main_menu,
    handle_button_click,
    handle_list_selection,
    handle_text_message,
    send_not_registered,
)

app = FastAPI()
logger = logging.getLogger(__name__)


# ── Startup / Shutdown ────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    await database.connect()
    logger.info("✅ Connected to database")


@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()
    logger.info("Database disconnected")


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    return {"status": "ok"}

# ── Ping endpoint ──────────────────────────────────────────────────────────────

@app.get("/ping")
async def ping():
    return {"pong": True}

# ── Webhook verification ──────────────────────────────────────────────────────

@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


# ── Signature validation ──────────────────────────────────────────────────────

def _verify_signature(body: bytes, signature: str) -> bool:
    """
    Validates X-Hub-Signature-256 sent by WhatsApp on every webhook POST.
    Skips validation if WHATSAPP_APP_SECRET is not configured (dev mode).
    """
    if not WHATSAPP_APP_SECRET:
        logger.debug("WHATSAPP_APP_SECRET not set — skipping signature validation (dev mode)")
        return True
    expected = "sha256=" + hmac.new(
        WHATSAPP_APP_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


# ── Deduplication ─────────────────────────────────────────────────────────────

async def _try_mark_processed(message_id: str) -> bool:
    """
    Atomically inserts message_id into processed_messages.
    Returns True  → this is a NEW message, go ahead and process it.
    Returns False → duplicate, skip it.

    Uses INSERT ... ON CONFLICT DO NOTHING to avoid the SELECT + INSERT
    race condition where two concurrent requests could both pass a
    SELECT check before either commits the INSERT.
    """
    try:
        result = await database.execute(
            """INSERT INTO processed_messages (message_id)
               VALUES (:mid)
               ON CONFLICT (message_id) DO NOTHING""",
            {"mid": message_id}
        )
        # databases library returns rowcount; 0 means conflict = duplicate
        return result != 0
    except Exception as e:
        logger.error(f"Dedup insert error: {e}")
        # On error, allow processing — better to send a duplicate than silently drop
        return True


async def _cleanup_old_messages():
    """Deletes processed_message records older than 24 hours. Safe to fail."""
    try:
        await database.execute(
            """DELETE FROM processed_messages
               WHERE processed_at < NOW() - INTERVAL '24 hours'"""
        )
    except Exception as e:
        # Non-critical — log and continue. Must NOT propagate or WhatsApp will
        # retry the webhook, causing duplicate message processing.
        logger.warning(f"Cleanup failed (non-critical): {e}")


# ── Core message processor (runs as background task) ─────────────────────────

async def _process_message(body: dict):
    """
    Processes a single incoming WhatsApp webhook payload.
    Runs as a FastAPI BackgroundTask so HTTP 200 is returned to WhatsApp
    BEFORE any DB work begins — this prevents WhatsApp from retrying due
    to slow DB responses, which was the root cause of the duplicate
    main-menu bug.
    """
    try:
        entry   = body["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]

        # Ignore delivery receipts, read receipts, and other status updates
        if "messages" not in value:
            return

        message    = value["messages"][0]
        phone      = message["from"]
        msg_type   = message["type"]
        message_id = message.get("id", "")

        # ── Dedup check (atomic) ─────────────────────────────────────────────
        if message_id and not await _try_mark_processed(message_id):
            logger.info(f"Duplicate message skipped: {message_id}")
            return

        # ── Occasional cleanup (1 in 20 requests, safely wrapped) ───────────
        if random.randint(1, 20) == 1:
            await _cleanup_old_messages()

        # ── Registration check ───────────────────────────────────────────────
        if not await is_user_registered(phone):
            await send_not_registered(phone)
            return

        # ── Session load ─────────────────────────────────────────────────────
        session = await get_session(phone)

        # ── Route by message type ────────────────────────────────────────────
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
        logger.warning(f"Payload parsing error (likely unsupported message type): {e}")
    except Exception as e:
        logger.exception(f"Unhandled error in message processing: {e}")


# ── Webhook receiver ──────────────────────────────────────────────────────────

@app.post("/webhook")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    """
    Receives incoming WhatsApp webhook POST.

    Returns HTTP 200 to WhatsApp IMMEDIATELY before any processing begins.
    This is critical — WhatsApp retries if it doesn't receive 200 within
    ~5 seconds, and slow DB cold-starts on Neon can exceed that threshold,
    causing duplicate messages to be sent to users.

    Actual processing is offloaded to a BackgroundTask.
    """
    raw_body = await request.body()

    # Validate WhatsApp signature to reject forged webhook calls
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(raw_body, signature):
        logger.warning(f"Invalid webhook signature rejected")
        raise HTTPException(status_code=403, detail="Invalid signature")

    body = await request.json()
    background_tasks.add_task(_process_message, body)

    # 200 returned here — before _process_message runs
    return {"status": "ok"}
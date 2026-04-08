# Finance Tracker by Shyara — WhatsApp Cloud API Helper Functions
# ─────────────────────────────────────────────────────────────────────────────
# All outgoing WhatsApp messages go through this file.
# bot_logic.py never calls the API directly — always uses these helpers.
# ─────────────────────────────────────────────────────────────────────────────

import httpx
import logging
import re
import unicodedata

from app.core.config import WHATSAPP_TOKEN, WHATSAPP_API_URL

logger = logging.getLogger(__name__)

HEADERS = {
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json",
}

# WhatsApp API hard limits
MAX_BUTTONS      = 3    # max reply buttons per interactive message
MAX_BUTTON_TITLE = 20   # max chars in button title
MAX_LIST_ROWS    = 10   # max rows across all sections in a list message
MAX_ROW_TITLE    = 24   # max chars in list row title
MAX_ROW_DESC     = 72   # max chars in list row description
MAX_BUTTON_LABEL = 20   # max chars on the list opener button


# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def _post(payload: dict) -> dict:
    """
    Internal POST to WhatsApp Cloud API with error handling.
    All public functions call this instead of httpx directly.

    On success: returns the API response dict.
    On failure: logs the error and returns {} so the bot keeps running.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                WHATSAPP_API_URL,
                headers=HEADERS,
                json=payload
            )
            if response.status_code != 200:
                logger.error(
                    f"WhatsApp API error {response.status_code}: "
                    f"{response.text[:300]}"
                )
                return {}
            return response.json()

    except httpx.TimeoutException:
        logger.error("WhatsApp API request timed out after 10s")
        return {}
    except httpx.RequestError as e:
        logger.error(f"WhatsApp API request failed: {e}")
        return {}
    except Exception as e:
        logger.error(f"Unexpected error sending WhatsApp message: {e}")
        return {}


def _clean_menu_label(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    cleaned = []
    started = False
    for char in text:
        category = unicodedata.category(char)
        if not started and (category.startswith("S") or category.startswith("P") or char.isspace()):
            continue
        started = True
        cleaned.append(char)

    normalized = "".join(cleaned).strip() or text
    normalized = re.sub(r"\s{2,}", " ", normalized)
    return normalized


# ══════════════════════════════════════════════════════════════════════════════
# SEND TEXT MESSAGE
# ══════════════════════════════════════════════════════════════════════════════

async def send_text_message(to: str, text: str) -> dict:
    """
    Send a plain text message.

    to   — recipient WhatsApp number (e.g. "919876543210")
    text — message body, supports WhatsApp markdown:
           *bold*  _italic_  ~strikethrough~  ```monospace```
    """
    if not text or not text.strip():
        logger.warning("send_text_message called with empty text — skipping")
        return {}

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to,
        "type":              "text",
        "text":              {"body": text[:4096]}   # WhatsApp max 4096 chars
    }
    logger.debug(f"Sending text to {to}: {text[:60]}...")
    return await _post(payload)


# ══════════════════════════════════════════════════════════════════════════════
# SEND INTERACTIVE BUTTONS
# ══════════════════════════════════════════════════════════════════════════════

async def send_interactive_buttons(
    to: str,
    body_text: str,
    buttons: list[dict]
) -> dict:
    """
    Send a message with up to 3 tappable reply buttons.

    to        — recipient WhatsApp number
    body_text — message shown above the buttons (max 1024 chars)
    buttons   — list of button dicts, max 3:
                [{"id": "btn_id", "title": "Button Label"}, ...]

    Button rules:
    - id:    your internal identifier, never shown to user (max 256 chars)
    - title: text displayed on the button (max 20 chars)
    - Max 3 buttons per message — use send_list_message for 4+ options
    """
    if not buttons:
        logger.warning("send_interactive_buttons called with no buttons — sending text instead")
        return await send_text_message(to, body_text)

    # Enforce max 3 buttons
    if len(buttons) > MAX_BUTTONS:
        logger.warning(f"Too many buttons ({len(buttons)}) — truncating to {MAX_BUTTONS}")
        buttons = buttons[:MAX_BUTTONS]

    # Truncate titles that exceed WhatsApp limit
    safe_buttons = []
    for btn in buttons:
        safe_buttons.append({
            "type":  "reply",
            "reply": {
                "id":    str(btn["id"])[:256],
                "title": _clean_menu_label(btn["title"])[:MAX_BUTTON_TITLE]
            }
        })

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to,
        "type":              "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text[:1024]},
            "action": {
                "buttons": safe_buttons
            }
        }
    }
    logger.debug(f"Sending buttons to {to}: {[b['reply']['title'] for b in safe_buttons]}")
    return await _post(payload)


# ══════════════════════════════════════════════════════════════════════════════
# SEND LIST MESSAGE
# ══════════════════════════════════════════════════════════════════════════════

async def send_list_message(
    to: str,
    body_text: str,
    button_label: str,
    sections: list[dict]
) -> dict:
    """
    Send a scrollable list message (item picker).
    Use this when you have 4–10 options (buttons only support max 3).

    to           — recipient WhatsApp number
    body_text    — message shown above the list button (max 1024 chars)
    button_label — text on the button that opens the picker (max 20 chars)
    sections     — list of section dicts:
                   [
                       {
                           "title": "Section Name",
                           "rows": [
                               {
                                   "id":          "unique_row_id",   # max 256 chars
                                   "title":       "Row Title",       # max 24 chars
                                   "description": "Details here"     # optional, max 72 chars
                               }
                           ]
                       }
                   ]

    Limits:
    - Max 10 rows total across all sections
    - Row title: max 24 chars
    - Row description: max 72 chars
    - Button label: max 20 chars
    """
    if not sections:
        logger.warning("send_list_message called with no sections — sending text instead")
        return await send_text_message(to, body_text)

    # Enforce row limits and truncate field lengths
    total_rows  = 0
    safe_sections = []

    for section in sections:
        if total_rows >= MAX_LIST_ROWS:
            break

        safe_rows = []
        for row in section.get("rows", []):
            if total_rows >= MAX_LIST_ROWS:
                break

            safe_row = {
                "id":    str(row["id"])[:256],
                "title": _clean_menu_label(row["title"])[:MAX_ROW_TITLE],
            }
            if row.get("description"):
                safe_row["description"] = _clean_menu_label(row["description"])[:MAX_ROW_DESC]

            safe_rows.append(safe_row)
            total_rows += 1

        if safe_rows:
            safe_sections.append({
                "title": _clean_menu_label(section.get("title", "Options"))[:24],
                "rows":  safe_rows
            })

    if not safe_sections:
        return await send_text_message(to, body_text)

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to,
        "type":              "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text[:1024]},
            "action": {
                "button":   _clean_menu_label(button_label)[:MAX_BUTTON_LABEL],
                "sections": safe_sections
            }
        }
    }
    logger.debug(f"Sending list to {to}: {total_rows} rows across {len(safe_sections)} sections")
    return await _post(payload)


# ══════════════════════════════════════════════════════════════════════════════
# SEND IMAGE MESSAGE
# ══════════════════════════════════════════════════════════════════════════════

async def send_image_message(
    to: str,
    image_url: str,
    caption: str = ""
) -> dict:
    """
    Send an image message via a public HTTPS URL.

    to        — recipient WhatsApp number
    image_url — publicly accessible HTTPS URL (max 5 MB, JPEG/PNG/WEBP)
    caption   — optional text shown below the image (max 1024 chars)

    Note: WhatsApp fetches the image from the URL at send time.
    The URL must be publicly accessible — localhost URLs won't work.
    """
    if not image_url or not image_url.startswith("https://"):
        logger.warning(f"send_image_message: invalid URL '{image_url}' — must be HTTPS")
        return await send_text_message(to, caption or "Image unavailable.")

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to,
        "type":              "image",
        "image": {
            "link":    image_url,
            "caption": caption[:1024] if caption else ""
        }
    }
    logger.debug(f"Sending image to {to}: {image_url[:80]}")
    return await _post(payload)


# ══════════════════════════════════════════════════════════════════════════════
# SEND DOCUMENT MESSAGE
# ══════════════════════════════════════════════════════════════════════════════

async def send_document_message(
    to: str,
    document_url: str,
    filename: str = "document.pdf",
    caption: str = ""
) -> dict:
    """
    Send a document/PDF via a public HTTPS URL.

    to           — recipient WhatsApp number
    document_url — publicly accessible HTTPS URL
    filename     — filename shown in WhatsApp (e.g. "report_march_2026.pdf")
    caption      — optional text shown below the document
    """
    if not document_url or not document_url.startswith("https://"):
        logger.warning(f"send_document_message: invalid URL '{document_url}'")
        return {}

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to,
        "type":              "document",
        "document": {
            "link":     document_url,
            "filename": filename,
            "caption":  caption[:1024] if caption else ""
        }
    }
    logger.debug(f"Sending document to {to}: {filename}")
    return await _post(payload)


# ══════════════════════════════════════════════════════════════════════════════
# SEND TEMPLATE MESSAGE
# ══════════════════════════════════════════════════════════════════════════════

async def send_template_message(
    to: str,
    template_name: str,
    language_code: str = "en",
    components: list = None
) -> dict:
    """
    Send a WhatsApp-approved template message.

    to            — recipient WhatsApp number
    template_name — name of the approved template (e.g. "welcome_new_user")
    language_code — language code of the template (e.g. "en", "en_US")
    components    — optional list of template component dicts for variable substitution:
                    [{"type": "body", "parameters": [{"type": "text", "text": "John"}]}]

    Note: Templates must be pre-approved by WhatsApp/Meta before use.
    Use send_text_message for dev/testing instead.
    """
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type":    "individual",
        "to":                to,
        "type":              "template",
        "template": {
            "name":     template_name,
            "language": {"code": language_code},
        }
    }
    if components:
        payload["template"]["components"] = components

    logger.debug(f"Sending template '{template_name}' to {to}")
    return await _post(payload)

from dotenv import load_dotenv
import os

load_dotenv()


def _require(name: str) -> str:
    """Raises a clear error at startup if a required env var is missing."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"❌ Missing required environment variable: {name}")
    return value


# ── Required ──────────────────────────────────────────────────────────────────
WHATSAPP_TOKEN  = _require("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = _require("PHONE_NUMBER_ID")
VERIFY_TOKEN    = _require("VERIFY_TOKEN")
DATABASE_URL    = _require("DATABASE_URL")

# ── Optional (gracefully degraded) ───────────────────────────────────────────

# Used for webhook signature validation.
# If not set, validation is skipped with a warning (acceptable in dev, not prod).
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")

# Used in send_not_registered() message to direct users to sign up.
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://your-app.com")

# ── Derived ───────────────────────────────────────────────────────────────────
WHATSAPP_API_URL = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
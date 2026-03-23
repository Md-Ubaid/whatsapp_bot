from dotenv import load_dotenv
import os

load_dotenv()

def require_env(name: str):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"❌ Missing environment variable: {name}")
    return value


WHATSAPP_TOKEN = require_env("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = require_env("PHONE_NUMBER_ID")
VERIFY_TOKEN = require_env("VERIFY_TOKEN")
DATABASE_URL = require_env("DATABASE_URL")

WHATSAPP_API_URL = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
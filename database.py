import os
from databases import Database
from dotenv import load_dotenv

load_dotenv()

_raw_url = os.getenv("DATABASE_URL", "")

# Normalize postgres:// → postgresql:// (required by asyncpg)
if _raw_url.startswith("postgres://"):
    _raw_url = _raw_url.replace("postgres://", "postgresql://", 1)

# Enforce SSL for Neon (and most managed Postgres providers)
if _raw_url and "sslmode" not in _raw_url:
    _raw_url += "?sslmode=require"

DATABASE_URL = _raw_url

# Connection pool limits prevent exhausting Neon's free-tier connection cap
# (typically 5–10 connections). Without limits, under load the pool grows
# unboundedly until the DB refuses new connections.
database = Database(DATABASE_URL, min_size=1, max_size=5)
"""Central configuration. Every value can be overridden with an env var."""
from __future__ import annotations

import os
from pathlib import Path

# --- Paths ------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("OSRS_GE_DATA_DIR", PROJECT_ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv("OSRS_GE_DB_PATH", DATA_DIR / "osrs_ge.duckdb"))
# Trades live in their OWN DuckDB file so the API can write them without fighting the
# collector for the prices-DB lock (DuckDB allows only one read-write process per file).
TRADES_DB_PATH = Path(os.getenv("OSRS_GE_TRADES_DB_PATH", DATA_DIR / "trades.duckdb"))
# Signal log lives in its OWN file too, written only by the collector (hourly), so it
# never contends with the prices-DB writer or the trades-DB writer.
LOG_DB_PATH = Path(os.getenv("OSRS_GE_LOG_DB_PATH", DATA_DIR / "signals_log.duckdb"))
DEMO_MARKER = DATA_DIR / ".demo_mode"  # present when the DB holds synthetic demo data

# --- OSRS Wiki Real-time Prices API -----------------------------------------
# Docs: https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices
API_BASE = "https://prices.runescape.wiki/api/v1/osrs"

# The wiki REQUIRES a descriptive User-Agent with a contact, so they can reach
# you if the script misbehaves. Change the contact via the env var if you'd
# rather use a Discord handle than an email.
USER_AGENT = os.getenv(
    "OSRS_GE_USER_AGENT",
    "osrs-ge-terminal/0.1 (personal flipping research; contact: tristan@swp360.com)",
)
HTTP_TIMEOUT = float(os.getenv("OSRS_GE_HTTP_TIMEOUT", "30"))
HTTP_RETRIES = int(os.getenv("OSRS_GE_HTTP_RETRIES", "4"))

# --- Collector --------------------------------------------------------------
POLL_INTERVAL_SECONDS = int(os.getenv("OSRS_GE_POLL_INTERVAL", "300"))  # 5 minutes

# --- Grand Exchange tax (verified June 2026 mechanics) ----------------------
TAX_RATE = 0.02          # 2% on the sell side (raised from 1% on 29 May 2025)
TAX_CAP = 5_000_000      # gp per item; binds at a sale price >= 250,000,000
TAX_MIN_PRICE = 50       # items selling for < 50 gp are untaxed (2% rounds to 0)

# --- Flip-finder default thresholds (tunable from the UI later) -------------
DEFAULT_MIN_VOLUME = 100         # min units traded per 5m side to trust a price
DEFAULT_MAX_PRICE_AGE = 3600     # seconds; ignore prices staler than this
DEFAULT_MIN_MARGIN = 1           # gp net margin floor
DEFAULT_BANKROLL = 250_000_000   # your trading capital, for position sizing

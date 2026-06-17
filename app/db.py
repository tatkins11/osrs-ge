"""DuckDB storage layer.

Concurrency model: connections are short-lived. The collector opens read-write
for a sub-second write every 5 minutes; the API server opens read-only per
request. ``connect`` retries briefly to smooth over the rare lock collision.

All timestamps are stored as naive UTC.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import duckdb
import pandas as pd

from .config import DB_PATH, TRADES_DB_PATH
from .tax import is_exempt

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Current time as a naive UTC datetime (what we store everywhere)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def connect(read_only: bool = False, retries: int = 12, retry_wait: float = 0.5):
    """Open the DuckDB database, retrying briefly if another process holds the lock."""
    last_err: Exception | None = None
    for _ in range(retries):
        try:
            return duckdb.connect(str(DB_PATH), read_only=read_only)
        except duckdb.Error as e:  # IOException on lock contention / missing file
            last_err = e
            time.sleep(retry_wait)
    raise RuntimeError(f"could not open DuckDB at {DB_PATH}: {last_err}") from last_err


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id    INTEGER PRIMARY KEY,
    name       VARCHAR,
    members    BOOLEAN,
    value      BIGINT,
    lowalch    BIGINT,
    highalch   BIGINT,
    buy_limit  BIGINT,
    icon       VARCHAR,
    exempt     BOOLEAN,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS snapshots (
    ts         TIMESTAMP,
    item_id    INTEGER,
    instabuy   BIGINT,   -- latest 'high' (price you PAY to buy now)
    instasell  BIGINT,   -- latest 'low'  (price you GET to sell now)
    high_time  TIMESTAMP,
    low_time   TIMESTAMP,
    avg_high   BIGINT,   -- 5m average insta-buy price
    avg_low    BIGINT,   -- 5m average insta-sell price
    high_vol   BIGINT,   -- 5m insta-buy volume
    low_vol    BIGINT,   -- 5m insta-sell volume
    PRIMARY KEY (ts, item_id)
);

CREATE TABLE IF NOT EXISTS history (
    item_id   INTEGER,
    timestep  VARCHAR,   -- '5m' | '1h' | '6h' | '24h'
    ts        TIMESTAMP,
    avg_high  BIGINT,
    avg_low   BIGINT,
    high_vol  BIGINT,
    low_vol   BIGINT,
    PRIMARY KEY (item_id, timestep, ts)
);

CREATE SEQUENCE IF NOT EXISTS trades_id_seq;
CREATE TABLE IF NOT EXISTS trades (
    id       BIGINT PRIMARY KEY DEFAULT nextval('trades_id_seq'),
    ts       TIMESTAMP,
    item_id  INTEGER,
    side     VARCHAR,    -- 'buy' | 'sell'
    qty      BIGINT,
    price    BIGINT,     -- gp per unit (what you actually paid / received)
    note     VARCHAR
);
"""

_INT_SNAPSHOT_COLS = ["item_id", "instabuy", "instasell", "avg_high", "avg_low", "high_vol", "low_vol"]
_INT_HISTORY_COLS = ["item_id", "avg_high", "avg_low", "high_vol", "low_vol"]


def _coerce_int(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Cast columns to nullable Int64 so missing values become SQL NULL cleanly."""
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    return df


def init_schema(con=None) -> None:
    own = con is None
    con = con or connect()
    try:
        con.execute(SCHEMA)
    finally:
        if own:
            con.close()


def ensure_db() -> None:
    """Create the database file and tables if they do not yet exist."""
    init_schema()


# --- writes -----------------------------------------------------------------
def upsert_items(mapping: list[dict], con=None) -> int:
    own = con is None
    con = con or connect()
    try:
        now = utcnow()
        rows = [
            {
                "item_id": m["id"],
                "name": m.get("name"),
                "members": m.get("members"),
                "value": m.get("value"),
                "lowalch": m.get("lowalch"),
                "highalch": m.get("highalch"),
                "buy_limit": m.get("limit"),
                "icon": m.get("icon"),
                "exempt": is_exempt(m.get("name")),
                "updated_at": now,
            }
            for m in mapping
        ]
        df = pd.DataFrame(rows)
        con.register("map_df", df)
        con.execute(
            """
            INSERT INTO items
                (item_id, name, members, value, lowalch, highalch, buy_limit, icon, exempt, updated_at)
            SELECT item_id, name, members, value, lowalch, highalch, buy_limit, icon, exempt, updated_at
            FROM map_df
            ON CONFLICT (item_id) DO UPDATE SET
                name=excluded.name, members=excluded.members, value=excluded.value,
                lowalch=excluded.lowalch, highalch=excluded.highalch,
                buy_limit=excluded.buy_limit, icon=excluded.icon,
                exempt=excluded.exempt, updated_at=excluded.updated_at
            """
        )
        con.unregister("map_df")
        return len(df)
    finally:
        if own:
            con.close()


def insert_snapshots(df: pd.DataFrame, con=None) -> int:
    if df is None or df.empty:
        return 0
    df = _coerce_int(df.copy(), _INT_SNAPSHOT_COLS)
    own = con is None
    con = con or connect()
    try:
        con.register("snap_df", df)
        con.execute(
            """
            INSERT INTO snapshots
                (ts, item_id, instabuy, instasell, high_time, low_time,
                 avg_high, avg_low, high_vol, low_vol)
            SELECT ts, item_id, instabuy, instasell, high_time, low_time,
                   avg_high, avg_low, high_vol, low_vol
            FROM snap_df
            ON CONFLICT (ts, item_id) DO NOTHING
            """
        )
        con.unregister("snap_df")
        return len(df)
    finally:
        if own:
            con.close()


def insert_history(df: pd.DataFrame, con=None) -> int:
    if df is None or df.empty:
        return 0
    df = _coerce_int(df.copy(), _INT_HISTORY_COLS)
    own = con is None
    con = con or connect()
    try:
        con.register("hist_df", df)
        con.execute(
            """
            INSERT INTO history (item_id, timestep, ts, avg_high, avg_low, high_vol, low_vol)
            SELECT item_id, timestep, ts, avg_high, avg_low, high_vol, low_vol
            FROM hist_df
            ON CONFLICT (item_id, timestep, ts) DO NOTHING
            """
        )
        con.unregister("hist_df")
        return len(df)
    finally:
        if own:
            con.close()


# --- reads ------------------------------------------------------------------
def get_items_df(con=None) -> pd.DataFrame:
    own = con is None
    con = con or connect(read_only=True)
    try:
        return con.execute("SELECT * FROM items ORDER BY item_id").df()
    finally:
        if own:
            con.close()


def latest_snapshot_df(con=None) -> pd.DataFrame:
    """The most recent snapshot row for every item."""
    own = con is None
    con = con or connect(read_only=True)
    try:
        return con.execute(
            """
            SELECT s.*
            FROM snapshots s
            JOIN (SELECT item_id, max(ts) AS mts FROM snapshots GROUP BY item_id) m
              ON s.item_id = m.item_id AND s.ts = m.mts
            """
        ).df()
    finally:
        if own:
            con.close()


def item_history_df(item_id: int, timestep: str | None = None, con=None) -> pd.DataFrame:
    own = con is None
    con = con or connect(read_only=True)
    try:
        if timestep:
            return con.execute(
                "SELECT * FROM history WHERE item_id=? AND timestep=? ORDER BY ts",
                [item_id, timestep],
            ).df()
        return con.execute(
            "SELECT * FROM history WHERE item_id=? ORDER BY timestep, ts", [item_id]
        ).df()
    finally:
        if own:
            con.close()


def item_snapshots_df(item_id: int, limit: int | None = None, con=None) -> pd.DataFrame:
    """Collected live snapshots for one item, oldest first."""
    own = con is None
    con = con or connect(read_only=True)
    try:
        q = "SELECT * FROM snapshots WHERE item_id=? ORDER BY ts"
        if limit:
            q = f"SELECT * FROM ({q} DESC LIMIT {int(limit)}) ORDER BY ts"
        return con.execute(q, [item_id]).df()
    finally:
        if own:
            con.close()


def stats(con=None) -> dict:
    """Quick health/coverage numbers for the dashboard + diagnostics."""
    own = con is None
    con = con or connect(read_only=True)
    try:
        n_items = con.execute("SELECT count(*) FROM items").fetchone()[0]
        n_snaps = con.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        n_hist = con.execute("SELECT count(*) FROM history").fetchone()[0]
        rng = con.execute("SELECT min(ts), max(ts) FROM snapshots").fetchone()
        return {
            "items": n_items,
            "snapshot_rows": n_snaps,
            "history_rows": n_hist,
            "snapshot_first": rng[0],
            "snapshot_last": rng[1],
        }
    finally:
        if own:
            con.close()


# --- personal trade log (separate DB file; API-owned, no lock fight with the collector) ---
_TRADES_SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS trades_id_seq;
CREATE TABLE IF NOT EXISTS trades (
    id       BIGINT PRIMARY KEY DEFAULT nextval('trades_id_seq'),
    ts       TIMESTAMP,
    item_id  INTEGER,
    side     VARCHAR,
    qty      BIGINT,
    price    BIGINT,
    note     VARCHAR
);
"""
_TRADE_COLS = ["id", "ts", "item_id", "side", "qty", "price", "note"]


def connect_trades(read_only: bool = False, retries: int = 12, retry_wait: float = 0.5):
    """Open the trades DB (separate file from the collector's prices DB)."""
    last_err: Exception | None = None
    for _ in range(retries):
        try:
            return duckdb.connect(str(TRADES_DB_PATH), read_only=read_only)
        except duckdb.Error as e:
            last_err = e
            time.sleep(retry_wait)
    raise RuntimeError(f"could not open trades DB at {TRADES_DB_PATH}: {last_err}") from last_err


def ensure_trades_db() -> None:
    """Create the trades file + schema if missing (call once at API startup)."""
    con = connect_trades()
    try:
        con.execute(_TRADES_SCHEMA)
    finally:
        con.close()


def insert_trade(item_id: int, side: str, qty: int, price: int, note: str = "", ts=None) -> None:
    con = connect_trades()
    try:
        con.execute(_TRADES_SCHEMA)  # idempotent; safe if startup ensure was skipped
        con.execute(
            "INSERT INTO trades (ts, item_id, side, qty, price, note) VALUES (?, ?, ?, ?, ?, ?)",
            [ts or utcnow(), int(item_id), side, int(qty), int(price), note or ""],
        )
    finally:
        con.close()


def get_trades_df() -> pd.DataFrame:
    try:
        con = connect_trades(read_only=True)
    except RuntimeError:
        return pd.DataFrame(columns=_TRADE_COLS)  # file not created yet -> no trades
    try:
        return con.execute("SELECT * FROM trades ORDER BY ts, id").df()
    except duckdb.Error:
        return pd.DataFrame(columns=_TRADE_COLS)
    finally:
        con.close()


def delete_trade(trade_id: int) -> None:
    con = connect_trades()
    try:
        con.execute("DELETE FROM trades WHERE id = ?", [int(trade_id)])
    finally:
        con.close()

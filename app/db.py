"""DuckDB storage layer.

Concurrency model: connections are short-lived. The collector opens read-write
for a sub-second write every 5 minutes; the API server opens read-only per
request. ``connect`` retries briefly to smooth over the rare lock collision.

All timestamps are stored as naive UTC.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import duckdb
import pandas as pd

from .config import DB_PATH, LOG_DB_PATH, TRADES_DB_PATH
from .tax import is_exempt, net_sell

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

CREATE TABLE IF NOT EXISTS updates (   -- OSRS game updates / blog posts (for chart event markers)
    ts        TIMESTAMP,
    title     VARCHAR,
    category  VARCHAR,
    url       VARCHAR PRIMARY KEY
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


# --- game updates (collector-written into the prices DB, API reads read-only) ---
def upsert_updates(rows: list[dict], con=None) -> int:
    if not rows:
        return 0
    own = con is None
    con = con or connect()
    try:
        df = pd.DataFrame(rows)
        con.register("upd_df", df)
        con.execute(
            """INSERT INTO updates (ts, title, category, url)
               SELECT ts, title, category, url FROM upd_df
               ON CONFLICT (url) DO UPDATE SET ts=excluded.ts, title=excluded.title, category=excluded.category"""
        )
        con.unregister("upd_df")
        return len(df)
    finally:
        if own:
            con.close()


def get_updates_df(con=None) -> pd.DataFrame:
    own = con is None
    con = con or connect(read_only=True)
    try:
        return con.execute("SELECT ts, title, category, url FROM updates ORDER BY ts DESC").df()
    except duckdb.Error:
        return pd.DataFrame(columns=["ts", "title", "category", "url"])
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
CREATE TABLE IF NOT EXISTS orders (   -- live GE offers streamed from the RuneLite plugin
    order_id     VARCHAR PRIMARY KEY, -- plugin-generated, stable per offer
    login        VARCHAR,
    slot         INTEGER,
    item_id      INTEGER,
    side         VARCHAR,             -- buy | sell
    price        BIGINT,              -- offer price per item
    total_qty    BIGINT,
    filled_qty   BIGINT,
    spent        BIGINT,              -- gp transacted so far
    state        VARCHAR,             -- BUYING|BOUGHT|SELLING|SOLD|CANCELLED_BUY|CANCELLED_SELL|EMPTY
    opened_ts    TIMESTAMP,
    updated_ts   TIMESTAMP,
    completed_ts TIMESTAMP,
    trade_id     BIGINT,              -- linked trade once a fill is finalized (NULL until then)
    cash_done    BIGINT DEFAULT 0     -- cash impact already applied to free_gp for this order (idempotent delta)
);
CREATE TABLE IF NOT EXISTS settings (   -- small key/value store (free_gp baseline, etc.)
    key   VARCHAR PRIMARY KEY,
    value DOUBLE
);
CREATE TABLE IF NOT EXISTS net_worth_log (  -- one row per day: snapshot of total worth for the growth curve
    day             DATE PRIMARY KEY,
    ts              TIMESTAMP,
    net_worth       BIGINT,
    bankroll        BIGINT,          -- liquid cash component (the user's bankroll filter at snapshot time)
    holdings_value  BIGINT,          -- open positions at live value, net of tax
    realized_total  BIGINT,
    unrealized_total BIGINT,
    invested        BIGINT
);
CREATE SEQUENCE IF NOT EXISTS plan_log_id_seq;
CREATE TABLE IF NOT EXISTS plan_log (   -- ~hourly snapshot of what the 8-Slot Plan recommended (for calibration)
    id        BIGINT PRIMARY KEY DEFAULT nextval('plan_log_id_seq'),
    ts        TIMESTAMP,
    action    VARCHAR,     -- BUY | SELL | CUT | HOLD
    item_id   INTEGER,
    name      VARCHAR,
    price     BIGINT,      -- recommended price (buy-at for buys, list-at for sells/holds)
    qty       BIGINT,      -- units (buys) or qty held (sells/holds)
    margin    BIGINT,      -- per-unit competitive margin (buys)
    gp_day    BIGINT,      -- modeled gp/day (buys)
    exp_net   BIGINT,      -- realizable P&L (sells/cuts)
    recovery  INTEGER,     -- recovery score 0-100 (holds/cuts)
    target    BIGINT,      -- sell target (buys) / fair value (holds)
    cur_price BIGINT       -- market price at snapshot (baseline for outcome eval)
);
"""
_TRADE_COLS = ["id", "ts", "item_id", "side", "qty", "price", "note"]
_NW_COLS = ["day", "ts", "net_worth", "bankroll", "holdings_value", "realized_total", "unrealized_total", "invested"]
_PLAN_LOG_COLS = ["id", "ts", "action", "item_id", "name", "price", "qty", "margin", "gp_day", "exp_net", "recovery", "target", "cur_price"]


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


def _ensure_schema(con) -> None:
    """Create the trades schema + run lightweight migrations (idempotent)."""
    con.execute(_TRADES_SCHEMA)
    try:  # add cash_done to orders tables created before the free-gp tracker
        con.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS cash_done BIGINT DEFAULT 0")
    except duckdb.Error:
        pass


def ensure_trades_db() -> None:
    """Create the trades file + schema if missing (call once at API startup)."""
    con = connect_trades()
    try:
        _ensure_schema(con)
    finally:
        con.close()


# --- free-gp tracking -------------------------------------------------------
# free_gp = your spendable coin pouch. Placing a buy reserves gp (out of the pouch); a sell
# returns proceeds as it fills; cancelling a buy returns the unfilled reserve. Each order stores
# the cash impact already applied (cash_done) so any state change re-applies only the delta.
def _get_setting(con, key, default=None):
    r = con.execute("SELECT value FROM settings WHERE key=?", [key]).fetchone()
    return r[0] if (r and r[0] is not None) else default


def _set_setting(con, key, value) -> None:
    con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", [key, float(value)])


def _target_cash(side, price, total, filled, state) -> int:
    """Net cash impact an order SHOULD have on free_gp given its current state."""
    price, total, filled = int(price or 0), int(total or 0), int(filled or 0)
    if side == "sell":
        return net_sell(price, False) * filled               # proceeds for the filled qty (~net of 2% tax)
    cancelled = str(state or "").upper().startswith("CANCELLED")
    return -price * (filled if cancelled else total)         # reserve full while live/bought; only filled if cancelled


def _reconcile_order_cash(con, oid: str) -> None:
    """Apply the delta between an order's target cash impact and what's already been applied."""
    r = con.execute("SELECT side, price, total_qty, filled_qty, state, cash_done FROM orders WHERE order_id=?", [oid]).fetchone()
    if not r:
        return
    side, price, total, filled, state, cash_done = r
    target = _target_cash(side, price, total, filled, state)
    delta = target - int(cash_done or 0)
    if delta:
        cur = _get_setting(con, "free_gp", None)
        if cur is not None:                                  # only move free_gp once a baseline is set
            _set_setting(con, "free_gp", max(0.0, float(cur) + delta))  # free gp can't go negative
        con.execute("UPDATE orders SET cash_done=? WHERE order_id=?", [int(target), oid])


def get_free_gp():
    """The persisted free-gp baseline, or None if never set (callers fall back to the filter)."""
    try:
        con = connect_trades(read_only=True)
    except RuntimeError:
        return None
    try:
        r = con.execute("SELECT value FROM settings WHERE key='free_gp'").fetchone()
        return float(max(0.0, r[0])) if (r and r[0] is not None) else None  # never report negative
    except duckdb.Error:
        return None
    finally:
        con.close()


def set_free_gp(value: float) -> None:
    """Set the free-gp baseline and re-anchor every order as already accounted at its current state,
    so the value is taken as-is (today's orders are baked in; only future changes adjust it)."""
    con = connect_trades()
    try:
        _ensure_schema(con)
        _set_setting(con, "free_gp", max(0.0, float(value)))
        for o in con.execute("SELECT order_id, side, price, total_qty, filled_qty, state FROM orders").fetchall():
            tgt = _target_cash(o[1], o[2], o[3], o[4], o[5])
            con.execute("UPDATE orders SET cash_done=? WHERE order_id=?", [int(tgt), o[0]])
    finally:
        con.close()


def insert_trade(item_id: int, side: str, qty: int, price: int, note: str = "", ts=None) -> int | None:
    con = connect_trades()
    try:
        con.execute(_TRADES_SCHEMA)  # idempotent; safe if startup ensure was skipped
        row = con.execute(
            "INSERT INTO trades (ts, item_id, side, qty, price, note) VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            [ts or utcnow(), int(item_id), side, int(qty), int(price), note or ""],
        ).fetchone()
        return int(row[0]) if row else None
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


def update_trade(trade_id: int, qty=None, price=None, note=None, side=None) -> None:
    """Patch a logged trade in place (e.g. bump qty as a buy order fills). Only the
    provided fields change."""
    sets, params = [], []
    if qty is not None:
        sets.append("qty = ?"); params.append(int(qty))
    if price is not None:
        sets.append("price = ?"); params.append(int(price))
    if note is not None:
        sets.append("note = ?"); params.append(note)
    if side is not None:
        sets.append("side = ?"); params.append(side)
    if not sets:
        return
    params.append(int(trade_id))
    con = connect_trades()
    try:
        con.execute(f"UPDATE trades SET {', '.join(sets)} WHERE id = ?", params)
    finally:
        con.close()


_ORDER_COLS = ["order_id", "login", "slot", "item_id", "side", "price", "total_qty",
               "filled_qty", "spent", "state", "opened_ts", "updated_ts", "completed_ts", "trade_id"]
_TERMINAL_STATES = {"BOUGHT", "SOLD", "CANCELLED_BUY", "CANCELLED_SELL"}


def _to_naive_utc(v):
    if not v:
        return utcnow()
    try:
        return pd.to_datetime(v, utc=True).to_pydatetime().replace(tzinfo=None)
    except Exception:
        return utcnow()


def ingest_offers(events: list[dict]) -> dict:
    """Upsert live GE offers from the RuneLite plugin. When an offer reaches a terminal
    state with a real fill, finalize it into a trade exactly once (so the portfolio /
    round-trip P&L update automatically). Buys record the real average fill price
    (spent/filled); sells record the gross offer price (the tax engine takes the 2%)."""
    if not events:
        return {"orders": 0, "trades_created": 0}
    con = connect_trades()
    try:
        _ensure_schema(con)
        n = made = 0
        for e in events:
            oid = str(e.get("order_id") or "").strip()
            if not oid:
                continue
            state = str(e.get("state") or "").upper()
            side = e.get("side") or ("sell" if ("SELL" in state or state == "SOLD") else "buy")
            item_id = int(e.get("item_id") or 0)
            price = int(e.get("price") or 0)
            total = int(e.get("total_qty") or 0)
            filled = int(e.get("filled_qty") or 0)
            spent = int(e.get("spent") or 0)
            slot = int(e.get("slot")) if e.get("slot") is not None else -1
            ts = _to_naive_utc(e.get("ts"))
            terminal = state in _TERMINAL_STATES
            existing = con.execute("SELECT trade_id FROM orders WHERE order_id = ?", [oid]).fetchone()
            # De-dup the same offer re-reported under a NEW id (plugin restart lost its slot->id memory).
            if existing is None:
                if state in ("BUYING", "SELLING"):
                    # an open offer: match a still-OPEN order for the same item/price/qty/side. Match an
                    # exact slot OR a slot-less manual order (entered on phone) so the plugin adopts the
                    # manual order instead of duplicating it (which would double-reserve the cash).
                    m = con.execute(
                        """SELECT order_id, trade_id FROM orders
                           WHERE state IN ('BUYING','SELLING') AND trade_id IS NULL
                             AND item_id=? AND price=? AND total_qty=? AND side=?
                             AND (slot=? OR slot IS NULL OR slot < 0)
                           ORDER BY (slot=?) DESC, updated_ts DESC LIMIT 1""",
                        [item_id, price, total, side, slot, slot],
                    ).fetchone()
                else:
                    # a terminal replay (e.g. a BOUGHT offer re-sent, or a manual order the plugin now
                    # reports filled): match the same offer (open OR finalized) seen recently — exact slot
                    # OR a slot-less manual order — so its trade isn't logged twice and cash isn't doubled.
                    m = con.execute(
                        """SELECT order_id, trade_id FROM orders
                           WHERE item_id=? AND price=? AND total_qty=? AND side=?
                             AND updated_ts >= ?
                             AND (slot=? OR slot IS NULL OR slot < 0)
                           ORDER BY (slot=?) DESC, updated_ts DESC LIMIT 1""",
                        [item_id, price, total, side, ts - timedelta(hours=18), slot, slot],
                    ).fetchone()
                if m:
                    oid, existing = m[0], (m[1],)
            if existing is None:
                # One offer per slot: a genuinely new order in this slot means any order we still
                # show as OPEN there has ended (we missed its terminal event) -> finalize it, logging
                # a trade for whatever it had filled (BOUGHT/SOLD if complete, else CANCELLED).
                if slot >= 0:
                    for so in con.execute(
                        """SELECT order_id, item_id, side, filled_qty, spent, price, total_qty
                           FROM orders WHERE slot=? AND state IN ('BUYING','SELLING') AND trade_id IS NULL""",
                        [slot],
                    ).fetchall():
                        s_oid, s_item, s_side, s_fill, s_spent, s_price, s_total = so
                        s_state = (("BOUGHT" if s_side == "buy" else "SOLD") if (s_fill or 0) >= (s_total or 0)
                                   else ("CANCELLED_BUY" if s_side == "buy" else "CANCELLED_SELL"))
                        con.execute("UPDATE orders SET state=?, completed_ts=? WHERE order_id=?", [s_state, ts, s_oid])
                        if s_fill and s_fill > 0:
                            avg = int(round(s_spent / s_fill)) if (s_side == "buy" and (s_spent or 0) > 0) else int(s_price or 0)
                            r2 = con.execute(
                                "INSERT INTO trades (ts,item_id,side,qty,price,note) VALUES (?,?,?,?,?,?) RETURNING id",
                                [ts, int(s_item), s_side, int(s_fill), int(avg), "GE auto · slot reused"],
                            ).fetchone()
                            con.execute("UPDATE orders SET trade_id=? WHERE order_id=?", [int(r2[0]) if r2 else None, s_oid])
                            made += 1
                        _reconcile_order_cash(con, s_oid)   # displaced order finalized -> settle its cash
                con.execute(
                    """INSERT INTO orders (order_id, login, slot, item_id, side, price, total_qty,
                           filled_qty, spent, state, opened_ts, updated_ts, completed_ts, trade_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                    [oid, e.get("login"), slot,
                     item_id, side, price, total, filled, spent, state, ts, ts, (ts if terminal else None)],
                )
                trade_id = None
            else:
                trade_id = existing[0]
                con.execute(
                    """UPDATE orders SET item_id=?, side=?, price=?, total_qty=?, filled_qty=?, spent=?,
                           state=?, slot=COALESCE(NULLIF(?, -1), slot), updated_ts=?,
                           completed_ts=COALESCE(completed_ts, ?) WHERE order_id=?""",
                    [item_id, side, price, total, filled, spent, state, slot, ts, (ts if terminal else None), oid],
                )
            n += 1
            if terminal and filled > 0 and trade_id is None:
                avg_price = int(round(spent / filled)) if (side == "buy" and spent > 0) else price
                # same connection (nesting connect_trades() would deadlock the single writer)
                row = con.execute(
                    "INSERT INTO trades (ts, item_id, side, qty, price, note) VALUES (?,?,?,?,?,?) RETURNING id",
                    [ts, item_id, side, filled, avg_price, f"GE auto · slot {e.get('slot')}"],
                ).fetchone()
                con.execute("UPDATE orders SET trade_id=? WHERE order_id=?", [int(row[0]) if row else None, oid])
                made += 1
            _reconcile_order_cash(con, oid)   # apply this offer's cash delta to free_gp (reserve / proceeds / return)
        return {"orders": n, "trades_created": made}
    finally:
        con.close()


def get_orders_df() -> pd.DataFrame:
    try:
        con = connect_trades(read_only=True)
    except RuntimeError:
        return pd.DataFrame(columns=_ORDER_COLS)
    try:
        return con.execute("SELECT * FROM orders ORDER BY updated_ts DESC").df()
    except duckdb.Error:
        return pd.DataFrame(columns=_ORDER_COLS)
    finally:
        con.close()


def delete_order(order_id: str) -> None:
    con = connect_trades()
    try:
        _ensure_schema(con)
        r = con.execute("SELECT cash_done, state FROM orders WHERE order_id=?", [str(order_id)]).fetchone()
        if r and r[0] and str(r[1] or "").upper() == "BUYING":  # removing a live buy returns its reserved gp
            cur = _get_setting(con, "free_gp", None)
            if cur is not None:
                _set_setting(con, "free_gp", max(0.0, float(cur) - int(r[0])))
        con.execute("DELETE FROM orders WHERE order_id = ?", [str(order_id)])
    finally:
        con.close()


def add_order(item_id: int, side: str, price: int, total_qty: int, filled_qty: int = 0,
              slot: int | None = None, login: str = "manual") -> str:
    """Create an order manually (for phone play without the RuneLite plugin). Returns the order_id."""
    oid = "m-" + uuid.uuid4().hex[:14]
    now = utcnow()
    state = "BUYING" if side == "buy" else "SELLING"
    con = connect_trades()
    try:
        _ensure_schema(con)
        con.execute(
            "INSERT INTO orders (order_id, login, slot, item_id, side, price, total_qty, filled_qty, spent, state, opened_ts, updated_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [oid, login, (int(slot) if slot is not None else None), int(item_id), side, int(price),
             int(total_qty), int(filled_qty), int(filled_qty) * int(price), state, now, now],
        )
        _reconcile_order_cash(con, oid)   # placing a buy reserves gp out of free_gp; a pre-filled sell credits proceeds
        return oid
    finally:
        con.close()


def update_order_fields(order_id: str, price=None, total_qty=None, filled_qty=None,
                        slot=None, state=None) -> None:
    """Manually edit an order (e.g. bump filled qty as it fills, or reprice) from the UI."""
    sets, params = [], []
    if price is not None:
        sets.append("price = ?"); params.append(int(price))
    if total_qty is not None:
        sets.append("total_qty = ?"); params.append(int(total_qty))
    if filled_qty is not None:
        sets.append("filled_qty = ?"); params.append(int(filled_qty))
    if slot is not None:
        sets.append("slot = ?"); params.append(int(slot))
    if state is not None:
        sets.append("state = ?"); params.append(str(state))
    if not sets:
        return
    sets.append("updated_ts = ?"); params.append(utcnow())
    params.append(str(order_id))
    con = connect_trades()
    try:
        _ensure_schema(con)
        con.execute(f"UPDATE orders SET {', '.join(sets)} WHERE order_id = ?", params)
        con.execute("UPDATE orders SET spent = filled_qty * price WHERE order_id = ?", [str(order_id)])
        _reconcile_order_cash(con, str(order_id))   # reflect repriced/filled changes in free_gp
    finally:
        con.close()


def purge_terminal_orders() -> int:
    """Remove finished orders (bought/sold/cancelled) from the tracker. Trades + P&L live in a
    separate table and are untouched; terminal orders' cash is already baked into free_gp, so
    deleting them doesn't move it. Returns how many rows were removed."""
    con = connect_trades()
    try:
        _ensure_schema(con)
        n = con.execute("SELECT count(*) FROM orders WHERE state NOT IN ('BUYING','SELLING')").fetchone()
        con.execute("DELETE FROM orders WHERE state NOT IN ('BUYING','SELLING')")
        return int(n[0]) if n else 0
    finally:
        con.close()


def record_net_worth(net_worth, bankroll, holdings_value, realized_total, unrealized_total, invested, ts=None) -> bool:
    """Snapshot today's net worth for the growth curve (at most one row per day). Best-effort:
    returns True if a new row was written, False if today's already exists or the write failed."""
    ts = ts or utcnow()
    day = ts.date() if hasattr(ts, "date") else ts
    try:
        con = connect_trades()
    except RuntimeError:
        return False
    try:
        con.execute(_TRADES_SCHEMA)
        if con.execute("SELECT 1 FROM net_worth_log WHERE day = ?", [day]).fetchone():
            return False
        con.execute(
            "INSERT INTO net_worth_log (day, ts, net_worth, bankroll, holdings_value, realized_total, unrealized_total, invested) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [day, ts, int(net_worth), int(bankroll), int(holdings_value), int(realized_total), int(unrealized_total), int(invested)],
        )
        return True
    except duckdb.Error:
        return False
    finally:
        con.close()


def get_net_worth_log_df() -> pd.DataFrame:
    try:
        con = connect_trades(read_only=True)
    except RuntimeError:
        return pd.DataFrame(columns=_NW_COLS)
    try:
        return con.execute("SELECT * FROM net_worth_log ORDER BY day").df()
    except duckdb.Error:
        return pd.DataFrame(columns=_NW_COLS)
    finally:
        con.close()


def insert_plan_log(plan: dict, ts=None) -> int:
    """Snapshot what the 8-Slot Plan recommended (best-effort, at most ~one per hour) so a later
    calibration can measure whether the buys filled/profited and the cut/hold calls were right."""
    recs = (plan.get("slots") or []) + (plan.get("holding") or [])
    if not recs:
        return 0
    ts = ts or utcnow()
    try:
        con = connect_trades()
    except RuntimeError:
        return 0
    try:
        _ensure_schema(con)
        last = con.execute("SELECT max(ts) FROM plan_log").fetchone()
        if last and last[0] is not None and (ts - last[0]).total_seconds() < 3300:  # ~55 min dedup
            return 0
        rows = [[
            ts, str(s.get("action") or ""), int(s.get("item_id") or 0), str(s.get("name") or ""),
            int(s.get("price") or 0), int(s.get("units") or s.get("qty") or 0),
            int(s.get("margin") or 0), int(s.get("gp_day") or 0), int(s.get("expected_net") or 0),
            int(s.get("recovery_score") or 0), int(s.get("target") or s.get("sell_target") or 0),
            int(s.get("cur_price") or s.get("price") or 0),
        ] for s in recs]
        con.executemany(
            "INSERT INTO plan_log (ts, action, item_id, name, price, qty, margin, gp_day, exp_net, recovery, target, cur_price) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)
    except duckdb.Error:
        return 0
    finally:
        con.close()


def get_plan_log_df() -> pd.DataFrame:
    try:
        con = connect_trades(read_only=True)
    except RuntimeError:
        return pd.DataFrame(columns=_PLAN_LOG_COLS)
    try:
        return con.execute("SELECT * FROM plan_log ORDER BY ts, id").df()
    except duckdb.Error:
        return pd.DataFrame(columns=_PLAN_LOG_COLS)
    finally:
        con.close()


# --- signal log (separate DB file; collector-owned, hourly snapshots) -------
_LOG_SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS signal_log_id_seq;
CREATE TABLE IF NOT EXISTS signal_log (
    id          BIGINT PRIMARY KEY DEFAULT nextval('signal_log_id_seq'),
    ts          TIMESTAMP,
    kind        VARCHAR,     -- flip | crash | value | overnight
    item_id     INTEGER,
    name        VARCHAR,
    rank        INTEGER,     -- position within that kind at snapshot time
    score       DOUBLE,      -- kind-specific headline (roi / confidence / fill_prob)
    entry       DOUBLE,      -- recommended buy price
    target      DOUBLE,      -- recommended sell / fair value
    exp_roi     DOUBLE,
    exp_margin  DOUBLE,
    horizon     VARCHAR,
    mid         DOUBLE,      -- market mid at snapshot, for later evaluation
    established DOUBLE
);
"""
_LOG_COLS = ["id", "ts", "kind", "item_id", "name", "rank", "score", "entry", "target",
             "exp_roi", "exp_margin", "horizon", "mid", "established"]


def connect_log(read_only: bool = False, retries: int = 12, retry_wait: float = 0.5):
    """Open the signal-log DB (its own file)."""
    last_err: Exception | None = None
    for _ in range(retries):
        try:
            return duckdb.connect(str(LOG_DB_PATH), read_only=read_only)
        except duckdb.Error as e:
            last_err = e
            time.sleep(retry_wait)
    raise RuntimeError(f"could not open log DB at {LOG_DB_PATH}: {last_err}") from last_err


def ensure_log_db() -> None:
    con = connect_log()
    try:
        con.execute(_LOG_SCHEMA)
    finally:
        con.close()


def insert_signal_log(df: pd.DataFrame, con=None) -> int:
    if df is None or df.empty:
        return 0
    own = con is None
    con = con or connect_log()
    try:
        con.execute(_LOG_SCHEMA)  # idempotent
        cols = [c for c in _LOG_COLS if c != "id" and c in df.columns]
        con.register("sl_df", df[cols])
        con.execute(f"INSERT INTO signal_log ({', '.join(cols)}) SELECT {', '.join(cols)} FROM sl_df")
        con.unregister("sl_df")
        return len(df)
    finally:
        if own:
            con.close()


def get_signal_log_df() -> pd.DataFrame:
    try:
        con = connect_log(read_only=True)
    except RuntimeError:
        return pd.DataFrame(columns=_LOG_COLS)  # file not created yet
    try:
        return con.execute("SELECT * FROM signal_log ORDER BY ts, id").df()
    except duckdb.Error:
        return pd.DataFrame(columns=_LOG_COLS)
    finally:
        con.close()

"""Synthetic data generator for offline development & demo.

The live OSRS API is firewall-blocked on this machine, so this seeds DuckDB
with realistic price history -- intraday + weekly seasonality, mean-reverting
noise, sensible bid/ask spreads and activity-driven volume -- for a basket of
representative items. It lets the full analytics / signal / UI stack run and be
validated with NO network access. Run the real collector on an open network to
replace this with live data.

Run:
    python -m app.mockdata                 # seed ~60 days of hourly history
    python -m app.mockdata --days 90       # longer history
    python -m app.mockdata --no-reset      # append instead of wiping first
"""
from __future__ import annotations

import argparse
import logging

import numpy as np
import pandas as pd

from .config import DEMO_MARKER
from .db import (
    connect,
    ensure_db,
    insert_history,
    insert_snapshots,
    upsert_items,
    utcnow,
)

log = logging.getLogger("mockdata")

# (id, name, base_price, buy_limit, members, daily_amp, spread_frac, base_vol)
#   daily_amp   = fractional size of the intraday swing (0.05 == +/-5%)
#   spread_frac = instabuy/instasell gap as a fraction of mid price
#   base_vol    = rough units traded per hour at average activity
DEMO_ITEMS = [
    (4151, "Abyssal whip", 1_500_000, 70, True, 0.05, 0.030, 1_500),
    (11802, "Armadyl godsword", 16_000_000, 8, True, 0.040, 0.020, 120),
    (20997, "Twisted bow", 1_250_000_000, 8, True, 0.030, 0.012, 40),
    (11832, "Bandos chestplate", 25_000_000, 70, True, 0.035, 0.020, 200),
    (21555, "Ancestral hat", 180_000_000, 8, True, 0.030, 0.015, 60),
    (6571, "Uncut onyx", 2_600_000, 100, True, 0.040, 0.030, 400),
    (4587, "Dragon scimitar", 60_000, 70, True, 0.060, 0.050, 3_000),
    (11235, "Dark bow", 70_000, 70, True, 0.060, 0.050, 1_500),
    (1127, "Rune platebody", 38_000, 125, False, 0.050, 0.040, 4_000),
    (4153, "Granite maul", 35_000, 70, True, 0.050, 0.040, 2_000),
    (2434, "Prayer potion(4)", 9_000, 8_000, True, 0.040, 0.030, 60_000),
    (12695, "Super combat potion(4)", 12_000, 8_000, True, 0.040, 0.030, 40_000),
    (6685, "Saradomin brew(4)", 11_000, 8_000, True, 0.040, 0.030, 45_000),
    (3024, "Super restore(4)", 11_000, 8_000, True, 0.040, 0.030, 50_000),
    (13441, "Anglerfish", 1_300, 13_000, True, 0.050, 0.050, 120_000),
    (385, "Shark", 800, 13_000, False, 0.050, 0.050, 200_000),
    (1513, "Magic logs", 1_000, 25_000, True, 0.070, 0.060, 90_000),
    (1515, "Yew logs", 230, 25_000, False, 0.080, 0.080, 250_000),
    (453, "Coal", 140, 25_000, False, 0.060, 0.050, 400_000),
    (2357, "Gold bar", 110, 13_000, False, 0.060, 0.050, 300_000),
    (561, "Nature rune", 95, 25_000, False, 0.050, 0.040, 800_000),
    (565, "Blood rune", 200, 25_000, True, 0.050, 0.040, 500_000),
    (12934, "Zulrah's scales", 130, 30_000, True, 0.050, 0.040, 700_000),
    (11212, "Dragon arrow", 1_300, 11_000, True, 0.050, 0.040, 200_000),
    (892, "Rune arrow", 70, 11_000, True, 0.060, 0.050, 500_000),
    (5295, "Ranarr seed", 30_000, 200, True, 0.060, 0.030, 3_000),
    (5304, "Torstol seed", 55_000, 200, True, 0.060, 0.030, 2_000),
    (1631, "Uncut dragonstone", 11_000, 10_000, True, 0.050, 0.040, 30_000),
    (9245, "Onyx bolts (e)", 12_000, 11_000, True, 0.050, 0.040, 30_000),
    (314, "Feather", 3, 30_000, False, 0.070, 0.100, 2_000_000),
]

PEAK_HOUR_UTC = 20  # player activity tends to peak in the evening (EU/US overlap)


def _gen_history(item: tuple, ts: pd.DatetimeIndex, rng: np.random.Generator) -> pd.DataFrame:
    iid, name, base, limit, members, daily_amp, spread_frac, base_vol = item
    n = len(ts)
    hour = ts.hour.to_numpy()
    dow = ts.dayofweek.to_numpy()

    # Smooth intraday cycle peaking at PEAK_HOUR_UTC, plus a weekend lift.
    daily = -daily_amp * np.cos(2 * np.pi * (hour - PEAK_HOUR_UTC) / 24.0)
    weekend = np.where(dow >= 5, daily_amp * 0.4, 0.0)

    # Ornstein-Uhlenbeck mean-reverting drift so z-score / Bollinger have signal.
    ou = np.zeros(n)
    theta, sigma = 0.04, daily_amp * 0.45
    steps = rng.normal(0.0, sigma, n)
    for i in range(1, n):
        ou[i] = ou[i - 1] * (1 - theta) + steps[i]

    mid = base * (1.0 + daily + weekend + ou)
    mid = np.maximum(mid, base * 0.4)  # floor to avoid silly values
    half = mid * spread_frac / 2.0
    avg_high = np.round(mid + half).astype("int64")  # instabuy side
    avg_low = np.round(mid - half).astype("int64")   # instasell side

    activity = 0.5 + 0.5 * (-np.cos(2 * np.pi * (hour - PEAK_HOUR_UTC) / 24.0))
    vol = base_vol * (0.5 + activity) * rng.uniform(0.7, 1.3, n)
    high_vol = np.round(vol * rng.uniform(0.4, 0.6, n)).astype("int64")
    low_vol = np.round(vol * rng.uniform(0.4, 0.6, n)).astype("int64")

    return pd.DataFrame(
        {
            "item_id": iid,
            "timestep": "1h",
            "ts": ts,
            "avg_high": avg_high,
            "avg_low": avg_low,
            "high_vol": high_vol,
            "low_vol": low_vol,
        }
    )


def _gen_snapshots(hist: pd.DataFrame, recent_days: int = 7) -> pd.DataFrame:
    """Build recent live-style snapshots at the collector's real 5-MINUTE cadence by
    upsampling the hourly history (linear mid interpolation + jitter, per-window Poisson
    volumes so quiet windows are genuinely empty). The 5-min grid matters: fill-uptime,
    clearing-VWAP and liquidity-clock all measure 5-min windows — hourly-only snapshots
    made every demo item look dead (uptime ~8%) and starved the planner's gates.
    Ends with one fresh row at 'now' so the flip-finder sees a current price."""
    rng = np.random.default_rng(int(hist["item_id"].iloc[0]) + 1)
    tail = hist.tail(recent_days * 24 + 1).copy()
    hours = tail["ts"].to_numpy().astype("datetime64[s]").astype("int64")   # epoch seconds ([us]-safe)
    grid = np.arange(hours[0], hours[-1] + 1, 300)                   # 5-min steps
    mid_h = ((tail["avg_high"] + tail["avg_low"]) / 2.0).to_numpy(dtype="float64")
    spread_h = (tail["avg_high"] - tail["avg_low"]).to_numpy(dtype="float64")
    mid = np.interp(grid, hours, mid_h) * (1.0 + rng.normal(0.0, 0.002, len(grid)))
    spread = np.maximum(1.0, np.interp(grid, hours, spread_h))
    hvol_h = np.interp(grid, hours, tail["high_vol"].to_numpy(dtype="float64")) / 12.0
    lvol_h = np.interp(grid, hours, tail["low_vol"].to_numpy(dtype="float64")) / 12.0
    high_vol = rng.poisson(np.maximum(hvol_h, 0.05))                 # zero-vol windows = no prints
    low_vol = rng.poisson(np.maximum(lvol_h, 0.05))
    ts = pd.to_datetime(grid, unit="s")
    snap = pd.DataFrame(
        {
            "ts": ts,
            "item_id": int(tail["item_id"].iloc[0]),
            "instabuy": np.round(mid + spread / 2.0).astype("int64"),
            "instasell": np.round(mid - spread / 2.0).astype("int64"),
            "high_time": ts,
            "low_time": ts,
            "avg_high": np.round(mid + spread / 2.0).astype("int64"),
            "avg_low": np.round(mid - spread / 2.0).astype("int64"),
            "high_vol": high_vol.astype("int64"),
            "low_vol": low_vol.astype("int64"),
        }
    )
    last = snap.iloc[-1]
    now = pd.Timestamp(utcnow())
    fresh = last.to_dict()
    fresh.update({"ts": now, "high_time": now, "low_time": now})
    return pd.concat([snap, pd.DataFrame([fresh])], ignore_index=True)


def clear(con) -> None:
    for t in ("snapshots", "history", "items"):
        con.execute(f"DELETE FROM {t}")


DEMO_FREE_GP = 152_000_000
_BASE = {iid: base for (iid, _n, base, *_r) in DEMO_ITEMS}
_NAME = {iid: n for (iid, n, *_r) in DEMO_ITEMS}


def seed_activity(end: pd.Timestamp) -> None:
    """Populate the trades + log DBs with a coherent DEMO story so every page renders with
    active information: ~3 weeks of closed round-trips, open positions in every state the
    planner grades (profitable / stale / underwater), live + terminal GE orders, a compounding
    net-worth history, and graded signal outcomes (an overnight roster with repeat winners).
    WIPES those tables first — this is the offline dev tool, never run on the live box."""
    from . import db as dbm

    rng = np.random.default_rng(7)
    dbm.ensure_trades_db()
    dbm.ensure_log_db()

    tcon = dbm.connect_trades()
    try:
        for t in ("trades", "orders", "net_worth_log", "plan_log", "settings"):
            tcon.execute(f"DELETE FROM {t}")

        def trade(ts, iid, side, qty, price, note="demo"):
            tcon.execute(
                "INSERT INTO trades (ts, item_id, side, qty, price, note) VALUES (?,?,?,?,?,?)",
                [ts.to_pydatetime(), int(iid), side, int(qty), int(round(price)), note],
            )

        # ---- ~3 weeks of closed round-trips on the liquid flip staples -------------------
        flippers = [4151, 2434, 6685, 3024, 11235, 4587, 9245, 1631, 11832]
        for d in range(20, 0, -1):
            day0 = end - pd.Timedelta(days=d)
            for _ in range(int(rng.integers(1, 4))):
                iid = int(rng.choice(flippers))
                base = _BASE[iid]
                qty = max(1, int(min(25_000_000 / base, 4000) * rng.uniform(0.4, 1.0)))
                buy = base * rng.uniform(0.96, 1.03)
                m = float(np.clip(rng.normal(0.023, 0.016), -0.035, 0.06))   # net margin, losers included
                sell = buy * (1 + m) / 0.98                                   # gross so net-of-tax hits m
                t0 = day0 + pd.Timedelta(hours=float(rng.uniform(8, 20)))
                t1 = t0 + pd.Timedelta(hours=float(np.clip(rng.lognormal(1.2, 0.8), 0.4, 30)))
                trade(t0, iid, "buy", qty, buy)
                trade(t1, iid, "sell", qty, sell)

        # ---- open positions in every state the planner grades ----------------------------
        trade(end - pd.Timedelta(days=9), 11802, "buy", 3, _BASE[11802] * 0.95)     # AGS: in profit -> SELL/HOLD
        trade(end - pd.Timedelta(days=4, hours=8), 21555, "buy", 1, _BASE[21555] * 1.033)  # Ancestral: flat/underwater -> stale
        trade(end - pd.Timedelta(days=2, hours=3), 6571, "buy", 25, _BASE[6571] * 1.046)   # Onyx: underwater -> recovery read
        trade(end - pd.Timedelta(days=1, hours=5), 3024, "buy", 2500, _BASE[3024] * 0.982) # restores: feeding the live sell

        # ---- GE orders: live (buying / selling / fresh) + recent terminal ----------------
        def order(oid, slot, iid, side, price, total, filled, state, opened_h, done_h=None):
            opened = (end - pd.Timedelta(hours=opened_h)).to_pydatetime()
            done = (end - pd.Timedelta(hours=done_h)).to_pydatetime() if done_h is not None else None
            tcon.execute(
                "INSERT INTO orders (order_id, login, slot, item_id, side, price, total_qty, filled_qty,"
                " spent, state, opened_ts, updated_ts, completed_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [oid, "demo", slot, int(iid), side, int(round(price)), int(total), int(filled),
                 int(round(price)) * int(filled), state, opened, done or opened, done],
            )

        order("demo-b1", 0, 6685, "buy", _BASE[6685] * 0.985, 3000, 1200, "BUYING", 2.2)
        order("demo-b2", 1, 1513, "buy", _BASE[1513] * 0.97, 20000, 0, "BUYING", 0.6)
        order("demo-s1", 2, 3024, "sell", _BASE[3024] * 1.025, 2500, 900, "SELLING", 3.1)
        order("demo-t1", 3, 11212, "buy", _BASE[11212] * 0.99, 8000, 8000, "BOUGHT", 6.0, 5.0)
        order("demo-t2", 4, 13441, "sell", _BASE[13441] * 1.02, 10000, 10000, "SOLD", 27.0, 26.0)
        order("demo-t3", 5, 1515, "buy", _BASE[1515] * 0.96, 20000, 6000, "CANCELLED_BUY", 31.0, 30.0)

        # ---- net-worth history: ~3 weeks compounding toward the next target --------------
        nw = 385_000_000.0
        realized = 0.0
        for d in range(21, 0, -1):
            day = (end - pd.Timedelta(days=d)).date()
            growth = rng.normal(0.011, 0.012)
            nw *= 1 + growth
            realized += max(0.0, nw * growth * 0.7)
            bank = nw * rng.uniform(0.25, 0.45)
            tcon.execute(
                "INSERT INTO net_worth_log (day, ts, net_worth, bankroll, holdings_value,"
                " realized_total, unrealized_total, invested) VALUES (?,?,?,?,?,?,?,?)",
                [day, pd.Timestamp(day).to_pydatetime(), int(nw), int(bank), int(nw - bank),
                 int(realized), int(rng.normal(0, 4e6)), int(nw - bank)],
            )
    finally:
        tcon.close()

    # free gp: set AFTER orders exist — set_free_gp re-anchors every order's cash as already
    # accounted, so the demo bankroll lands exactly at DEMO_FREE_GP (and sets the manual anchor).
    dbm.set_free_gp(DEMO_FREE_GP)

    # ---- graded signal outcomes: the Proven tab + the 2-touch roster ----------------------
    lcon = dbm.connect_log()
    try:
        lcon.execute("DELETE FROM signal_outcomes")
        horizon = {"overnight": 1, "flip": 1, "crash": 7, "value": 3}

        def outcomes(kind, iid, n, win_p, ret_mu, ret_sd):
            for k in range(n):
                sig = end - pd.Timedelta(days=float(rng.uniform(1, 20)), hours=float(k))
                win = bool(rng.random() < win_p)
                ret = abs(rng.normal(ret_mu, ret_sd)) * (1 if win else -0.6)
                lcon.execute(
                    "INSERT OR IGNORE INTO signal_outcomes (kind, item_id, sig_ts, name, score,"
                    " horizon_d, reached, win, ret_net, graded_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [kind, int(iid), sig.to_pydatetime(), _NAME[iid], float(rng.uniform(20, 95)),
                     horizon[kind], win and bool(rng.random() < 0.9), win, float(ret),
                     (sig + pd.Timedelta(days=horizon[kind])).to_pydatetime()],
                )

        # overnight: the proven roster — repeat winners with real edge
        for iid, n, wp, mu in [(2434, 12, 0.92, 0.105), (6685, 10, 0.9, 0.085), (3024, 9, 0.89, 0.09),
                               (4151, 9, 0.78, 0.06), (11212, 8, 0.85, 0.07), (9245, 7, 0.7, 0.05)]:
            outcomes("overnight", iid, n, wp, mu, 0.03)
        for iid in (4151, 4587, 11235, 1127, 1631, 12934):     # flip: mediocre, honest
            outcomes("flip", iid, 10, 0.42, 0.012, 0.02)
        for iid in (11802, 11832, 21555, 6571):                # crash: modest edge
            outcomes("crash", iid, 6, 0.58, 0.03, 0.025)
        for iid in (5295, 5304, 1513, 561, 565):               # value: coin-flip
            outcomes("value", iid, 8, 0.47, 0.015, 0.02)
    finally:
        lcon.close()

    log.info("demo activity seeded: trades, orders, net-worth history, signal outcomes, free_gp=%s", f"{DEMO_FREE_GP:,}")


def seed(days: int = 60, reset: bool = True) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ensure_db()

    end = pd.Timestamp(utcnow()).floor("h")
    start = end - pd.Timedelta(days=days)
    ts = pd.date_range(start, end, freq="h")

    if reset:
        con = connect()
        try:
            clear(con)
        finally:
            con.close()

    mapping = [
        {
            "id": iid, "name": name, "members": members, "value": base,
            "lowalch": int(base * 0.4), "highalch": int(base * 0.6),
            "limit": limit, "icon": "",
        }
        for (iid, name, base, limit, members, *_rest) in DEMO_ITEMS
    ]
    upsert_items(mapping)

    hist_frames, snap_frames = [], []
    for item in DEMO_ITEMS:
        rng = np.random.default_rng(item[0])  # reproducible per item id
        h = _gen_history(item, ts, rng)
        hist_frames.append(h)
        snap_frames.append(_gen_snapshots(h))

    hist = pd.concat(hist_frames, ignore_index=True)
    snaps = pd.concat(snap_frames, ignore_index=True)
    insert_history(hist)
    insert_snapshots(snaps)
    seed_activity(end)

    log.info(
        "SYNTHETIC DEMO DATA seeded: %d items, %d history rows (%s -> %s), %d snapshot rows",
        len(DEMO_ITEMS), len(hist), start.date(), end.date(), len(snaps),
    )
    log.warning("This is FAKE data for offline demo. Delete the DB and run the collector for real prices.")
    DEMO_MARKER.write_text(str(utcnow()))


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed DuckDB with synthetic OSRS price data for offline demo.")
    ap.add_argument("--days", type=int, default=60, help="days of hourly history to generate")
    ap.add_argument("--no-reset", dest="reset", action="store_false", help="append instead of wiping tables first")
    args = ap.parse_args()
    seed(days=args.days, reset=args.reset)


if __name__ == "__main__":
    main()

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


def _gen_snapshots(hist: pd.DataFrame, recent_hours: int = 48) -> pd.DataFrame:
    """Build recent live-style snapshots from the tail of an item's history,
    plus one fresh row at 'now' so the flip-finder sees a current price."""
    tail = hist.tail(recent_hours).copy()
    snap = pd.DataFrame(
        {
            "ts": tail["ts"],
            "item_id": tail["item_id"],
            "instabuy": tail["avg_high"],
            "instasell": tail["avg_low"],
            "high_time": tail["ts"],
            "low_time": tail["ts"],
            "avg_high": tail["avg_high"],
            "avg_low": tail["avg_low"],
            "high_vol": tail["high_vol"],
            "low_vol": tail["low_vol"],
        }
    )
    last = tail.iloc[-1]
    now = pd.Timestamp(utcnow())
    jitter_h = 1.0 + np.random.default_rng(int(last["item_id"])).uniform(-0.004, 0.004)
    fresh = pd.DataFrame(
        [
            {
                "ts": now,
                "item_id": int(last["item_id"]),
                "instabuy": int(round(last["avg_high"] * jitter_h)),
                "instasell": int(round(last["avg_low"] * jitter_h)),
                "high_time": now,
                "low_time": now,
                "avg_high": int(last["avg_high"]),
                "avg_low": int(last["avg_low"]),
                "high_vol": int(last["high_vol"]),
                "low_vol": int(last["low_vol"]),
            }
        ]
    )
    return pd.concat([snap, fresh], ignore_index=True)


def clear(con) -> None:
    for t in ("snapshots", "history", "items"):
        con.execute(f"DELETE FROM {t}")


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

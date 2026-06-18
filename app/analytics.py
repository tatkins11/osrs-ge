"""Statistical analytics over collected price data.

Two layers:
  * market_table()  -- one fast pass (SQL aggregates + vectorised math) over
    every item, producing the enriched table the dashboard/flip-finder use.
  * analyze_item()  -- a deep dive for a single item: full indicator series
    (moving average, Bollinger bands, RSI, z-score) plus hour-of-day and
    day-of-week seasonal profiles, for the per-item terminal screen.

All prices are gp; all timestamps are naive UTC.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import tax as taxmod
from .config import DEFAULT_BANKROLL
from .db import (
    connect,
    get_items_df,
    item_history_df,
    item_snapshots_df,
    latest_snapshot_df,
    utcnow,
)

HISTORY_TIMESTEP = "1h"

# --- whole-market table -----------------------------------------------------
_MARKET_STATS_SQL = """
WITH base AS (
    SELECT item_id, ts,
           (avg_high + avg_low) / 2.0 AS mid,
           (COALESCE(high_vol, 0) + COALESCE(low_vol, 0)) AS vol,
           (avg_high * 0.98 - avg_low) AS flip_margin   -- approx net flip margin (2% tax)
    FROM history
    WHERE timestep = '1h' AND avg_high IS NOT NULL AND avg_low IS NOT NULL
),
base6 AS (   -- 6h history spans ~90 days; the 1h table only ~2 weeks, so the 30d window must use 6h
    SELECT item_id, ts, (avg_high + avg_low) / 2.0 AS mid
    FROM history
    WHERE timestep = '6h' AND avg_high IS NOT NULL AND avg_low IS NOT NULL
),
mx  AS (SELECT max(ts) AS t FROM base),
mx6 AS (SELECT max(ts) AS t FROM base6),
agg7 AS (
    SELECT b.item_id,
        avg(b.mid)         FILTER (WHERE b.ts >= mx.t - INTERVAL 7 DAY)  AS mean_7d,
        median(b.mid)      FILTER (WHERE b.ts >= mx.t - INTERVAL 7 DAY)  AS median_7d,  -- robust "established level"
        stddev_samp(b.mid) FILTER (WHERE b.ts >= mx.t - INTERVAL 7 DAY)  AS sd_7d,
        avg(b.vol)         FILTER (WHERE b.ts >= mx.t - INTERVAL 7 DAY)  AS vol_hourly_7d,
        sum(b.vol)         FILTER (WHERE b.ts >= mx.t - INTERVAL 24 HOUR) AS vol_24h,
        arg_max(b.mid, b.ts) FILTER (WHERE b.ts <= mx.t - INTERVAL 24 HOUR) AS mid_1d_ago,
        -- margin persistence: fraction of the past week the flip was profitable, and its typical size
        avg(CASE WHEN b.flip_margin > 0 THEN 1.0 ELSE 0.0 END) FILTER (WHERE b.ts >= mx.t - INTERVAL 7 DAY) AS margin_uptime,
        median(b.flip_margin) FILTER (WHERE b.ts >= mx.t - INTERVAL 7 DAY) AS margin_median_7d,
        count(*)           FILTER (WHERE b.ts >= mx.t - INTERVAL 7 DAY)  AS n_7d
    FROM base b CROSS JOIN mx
    GROUP BY b.item_id
),
agg30 AS (   -- real 30-day range/mean from 6h bars
    SELECT b.item_id,
        min(b.mid) FILTER (WHERE b.ts >= mx6.t - INTERVAL 30 DAY) AS min_30d,
        max(b.mid) FILTER (WHERE b.ts >= mx6.t - INTERVAL 30 DAY) AS max_30d,
        avg(b.mid) FILTER (WHERE b.ts >= mx6.t - INTERVAL 30 DAY) AS mean_30d
    FROM base6 b CROSS JOIN mx6
    GROUP BY b.item_id
)
SELECT a.item_id, a.mean_7d, a.median_7d, a.sd_7d,
       a30.min_30d, a30.max_30d, a30.mean_30d,
       a.vol_hourly_7d, a.vol_24h, a.mid_1d_ago, a.margin_uptime, a.margin_median_7d, a.n_7d
FROM agg7 a
LEFT JOIN agg30 a30 USING (item_id)
"""


def _market_history_stats(con) -> pd.DataFrame:
    return con.execute(_MARKET_STATS_SQL).df()


def market_table(con=None, bankroll: int = DEFAULT_BANKROLL) -> pd.DataFrame:
    """Build the enriched per-item market table (one row per tradeable item)."""
    own = con is None
    con = con or connect(read_only=True)
    try:
        items = get_items_df(con)
        latest = latest_snapshot_df(con)
        hist_stats = _market_history_stats(con)
    finally:
        if own:
            con.close()

    if latest.empty:
        return pd.DataFrame()

    df = items.merge(latest, on="item_id", how="inner").merge(hist_stats, on="item_id", how="left")

    buy = df["instasell"].astype("float64")   # you buy by joining the low side
    sell = df["instabuy"].astype("float64")   # you sell by joining the high side
    df["buy_price"] = buy
    df["sell_price"] = sell
    df["mid"] = (buy + sell) / 2.0

    df["tax"] = taxmod.sell_tax_array(sell, df["exempt"].to_numpy())
    df["gross_margin"] = sell - buy
    df["net_margin"] = (sell - df["tax"]) - buy
    df["roi"] = np.where(buy > 0, df["net_margin"] / buy, np.nan)

    limit = df["buy_limit"].astype("float64")
    df["profit_per_cycle"] = df["net_margin"] * limit            # full buy-limit fill
    df["capital_per_cycle"] = buy * limit

    # Statistical position relative to recent history.
    sd = df["sd_7d"].astype("float64")
    df["z_7d"] = np.where(sd > 0, (df["mid"] - df["mean_7d"]) / sd, np.nan)
    rng = (df["max_30d"] - df["min_30d"]).astype("float64")
    df["pct_30d"] = np.where(rng > 0, (df["mid"] - df["min_30d"]) / rng, np.nan)
    df["volatility_7d"] = np.where(df["mean_7d"] > 0, sd / df["mean_7d"], np.nan)
    df["vol_hourly_7d"] = df["vol_hourly_7d"].astype("float64")
    df["vol_daily_7d"] = df["vol_hourly_7d"] * 24.0

    # unusual-volume: last 24h traded vs a typical day; + the 24h price change for direction
    df["vol_24h"] = df["vol_24h"].astype("float64")
    df["vol_ratio"] = np.where(df["vol_daily_7d"] > 0, df["vol_24h"] / df["vol_daily_7d"], np.nan)
    md1 = df["mid_1d_ago"].astype("float64")
    df["chg_24h"] = np.where(md1 > 0, (df["mid"] - md1) / md1, np.nan)

    now = pd.Timestamp(utcnow())
    last_trade = df[["high_time", "low_time"]].max(axis=1)
    df["price_age_min"] = (now - last_trade).dt.total_seconds() / 60.0

    return df


# --- single-item deep dive --------------------------------------------------
def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def add_indicators(series: pd.DataFrame, window: int = 168, rsi_period: int = 14) -> pd.DataFrame:
    """Add mid, moving average, Bollinger bands, z-score and RSI. ``window`` in rows
    (168 hourly rows == 7 days)."""
    s = series.sort_values("ts").reset_index(drop=True).copy()
    s["mid"] = (s["avg_high"].astype("float64") + s["avg_low"].astype("float64")) / 2.0
    minp = max(2, window // 4)
    s["ma"] = s["mid"].rolling(window, min_periods=minp).mean()
    s["sd"] = s["mid"].rolling(window, min_periods=minp).std()
    s["upper"] = s["ma"] + 2 * s["sd"]
    s["lower"] = s["ma"] - 2 * s["sd"]
    s["z"] = np.where(s["sd"] > 0, (s["mid"] - s["ma"]) / s["sd"], np.nan)
    s["rsi"] = _rsi(s["mid"], rsi_period)
    return s


def hour_profile(series: pd.DataFrame) -> pd.DataFrame:
    """Average % deviation from each day's mean, grouped by UTC hour.
    Negative == historically cheap hour (good to buy); positive == expensive."""
    s = series.copy()
    s["mid"] = (s["avg_high"].astype("float64") + s["avg_low"].astype("float64")) / 2.0
    s["day"] = s["ts"].dt.normalize()
    daily_mean = s.groupby("day")["mid"].transform("mean")
    s["dev"] = np.where(daily_mean > 0, s["mid"] / daily_mean - 1.0, np.nan)
    s["hour"] = s["ts"].dt.hour
    g = s.groupby("hour")["dev"].agg(["mean", "std", "count"]).reset_index()
    return g.rename(columns={"mean": "avg_dev"})


def dow_profile(series: pd.DataFrame) -> pd.DataFrame:
    """Average % deviation from a trailing weekly mean, grouped by day of week (0=Mon)."""
    s = series.sort_values("ts").copy()
    s["mid"] = (s["avg_high"].astype("float64") + s["avg_low"].astype("float64")) / 2.0
    weekly_mean = s["mid"].rolling(168, min_periods=24).mean()
    s["dev"] = np.where(weekly_mean > 0, s["mid"] / weekly_mean - 1.0, np.nan)
    s["dow"] = s["ts"].dt.dayofweek
    g = s.groupby("dow")["dev"].agg(["mean", "std", "count"]).reset_index()
    return g.rename(columns={"mean": "avg_dev"})


def _epoch_seconds(ts: pd.Series) -> pd.Series:
    """Unix seconds from naive-UTC datetimes. Resolution-robust: DuckDB/pandas may
    return datetime64[us] (not [ns]), so a fixed //1e9 would be 1000x off."""
    return ts.astype("datetime64[s]").astype("int64")


def _clean(v):
    """Coerce a pandas/numpy scalar to a JSON-serialisable Python value."""
    if v is None or v is pd.NaT or v is pd.NA:
        return None
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.isoformat()
    return v


def analyze_item(item_id: int, con=None, max_points: int = 2000) -> dict | None:
    own = con is None
    con = con or connect(read_only=True)
    try:
        items = get_items_df(con)
        row = items[items["item_id"] == item_id]
        if row.empty:
            return None
        hist = item_history_df(item_id, HISTORY_TIMESTEP, con)
        latest = latest_snapshot_df(con)
    finally:
        if own:
            con.close()

    item = row.iloc[0].to_dict()
    cur = latest[latest["item_id"] == item_id]

    out: dict = {
        "item": {
            "item_id": int(item["item_id"]),
            "name": item.get("name"),
            "members": bool(item.get("members")) if item.get("members") is not None else None,
            "buy_limit": _clean(item.get("buy_limit")),
            "exempt": bool(item.get("exempt")),
            "high_alch": _clean(item.get("highalch")),
        },
        "current": {},
        "stats": {},
        "series": [],
        "hour_profile": [],
        "dow_profile": [],
    }

    exempt = bool(item.get("exempt"))
    if not cur.empty:
        c = cur.iloc[0]
        buy = float(c["instasell"]) if pd.notna(c["instasell"]) else None
        sell = float(c["instabuy"]) if pd.notna(c["instabuy"]) else None
        tax = int(taxmod.sell_tax(int(sell), exempt)) if sell else 0
        net = (sell - tax - buy) if (buy and sell) else None
        out["current"] = {
            "instabuy": _clean(sell),
            "instasell": _clean(buy),
            "mid": _clean((buy + sell) / 2.0) if (buy and sell) else None,
            "tax": tax,
            "gross_margin": _clean(sell - buy) if (buy and sell) else None,
            "net_margin": _clean(net),
            "roi": _clean(net / buy) if (net is not None and buy) else None,
            "high_vol": _clean(c.get("high_vol")),
            "low_vol": _clean(c.get("low_vol")),
        }

    if not hist.empty:
        ind = add_indicators(hist)
        if len(ind) > max_points:
            ind = ind.iloc[-max_points:]
        ind = ind.assign(time=_epoch_seconds(ind["ts"]))
        cols = ["time", "avg_high", "avg_low", "mid", "ma", "upper", "lower", "z", "rsi", "high_vol", "low_vol"]
        out["series"] = [
            {k: _clean(v) for k, v in rec.items()}
            for rec in ind[cols].to_dict("records")
        ]

        last = ind.iloc[-1]
        valid_mid = ind["mid"].dropna()                       # illiquid items can have a NA trailing mid
        mid_now = float(valid_mid.iloc[-1]) if not valid_mid.empty else None
        mean_7d = float(last["ma"]) if pd.notna(last["ma"]) else None
        sd_7d = float(last["sd"]) if pd.notna(last["sd"]) else None
        out["stats"] = {
            "mean_7d": _clean(mean_7d),
            "sd_7d": _clean(sd_7d),
            "z_7d": _clean((mid_now - mean_7d) / sd_7d) if (mid_now is not None and mean_7d and sd_7d) else None,
            "rsi": _clean(float(last["rsi"])) if pd.notna(last["rsi"]) else None,
            "min_30d": _clean(float(ind["mid"].tail(720).min())),
            "max_30d": _clean(float(ind["mid"].tail(720).max())),
            "volatility_7d": _clean(sd_7d / mean_7d) if (mean_7d and sd_7d) else None,
        }

        hp = hour_profile(hist)
        out["hour_profile"] = [
            {"hour": int(r["hour"]), "avg_dev": _clean(float(r["avg_dev"])), "count": int(r["count"])}
            for _, r in hp.iterrows()
        ]
        dp = dow_profile(hist)
        out["dow_profile"] = [
            {"dow": int(r["dow"]), "avg_dev": _clean(float(r["avg_dev"])), "count": int(r["count"])}
            for _, r in dp.iterrows()
        ]

    return out


# Multi-horizon % change windows. Each picks the coarsest history timestep that
# still covers it: 1h history spans ~2 weeks, 6h ~3 months, 24h ~1 year.
HORIZONS: list[tuple[str, int, str]] = [
    ("1d", 24, "1h"),
    ("1w", 168, "1h"),
    ("2w", 336, "1h"),
    ("1mo", 720, "6h"),
    ("3mo", 2160, "6h"),
    ("1y", 8760, "24h"),
]


def _mid_series(hist: pd.DataFrame) -> pd.Series:
    """ts-indexed mid-price series from a history frame (sorted, NaN dropped)."""
    if hist is None or hist.empty:
        return pd.Series(dtype="float64")
    s = hist.sort_values("ts").copy()
    s["mid"] = (s["avg_high"].astype("float64") + s["avg_low"].astype("float64")) / 2.0
    return s.dropna(subset=["mid"]).set_index("ts")["mid"]


def series_changes(by_timestep: dict[str, pd.Series]) -> dict:
    """% change over each HORIZON, read off the appropriate timestep's mid series.
    Returns fractions (e.g. -0.05), or None when there isn't enough history."""
    out: dict = {}
    for label, hours, ts in HORIZONS:
        s = by_timestep.get(ts)
        if s is None or s.empty:
            out[label] = None
            continue
        now_ts, now = s.index[-1], s.iloc[-1]
        past = s.asof(now_ts - pd.Timedelta(hours=hours))
        out[label] = float(now / past - 1.0) if (pd.notna(past) and past and past > 0) else None
    return out


def item_changes(item_id: int, con=None) -> dict:
    """1d/1w/2w/1mo/3mo/1y price change for one item (fractions)."""
    own = con is None
    con = con or connect(read_only=True)
    try:
        by_ts = {ts: _mid_series(item_history_df(item_id, ts, con)) for ts in ("1h", "6h", "24h")}
    finally:
        if own:
            con.close()
    return series_changes(by_ts)


def item_series(item_id: int, timestep: str = "1h", con=None, max_points: int = 2000) -> list[dict]:
    """Price + indicator series at a chosen timestep, for the chart's timeframe toggle
    (1h ~= 2 weeks, 6h ~= 3 months, 24h ~= 1 year of backfilled history)."""
    own = con is None
    con = con or connect(read_only=True)
    try:
        hist = item_history_df(item_id, timestep, con)
    finally:
        if own:
            con.close()
    if hist.empty:
        return []
    window = {"1h": 168, "6h": 28, "24h": 30}.get(timestep, 60)
    ind = add_indicators(hist, window=window)
    if len(ind) > max_points:
        ind = ind.iloc[-max_points:]
    ind = ind.assign(time=_epoch_seconds(ind["ts"]))
    cols = ["time", "avg_high", "avg_low", "mid", "ma", "upper", "lower", "z", "rsi", "high_vol", "low_vol"]
    return [{k: _clean(v) for k, v in rec.items()} for rec in ind[cols].to_dict("records")]

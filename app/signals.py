"""Signal & flip-finder engine.

Consumes the enriched market table and produces ranked, actionable signals:

  FLIP        -- spread capture: net (after-tax) margin with real volume on
                 BOTH sides and a fresh price. This is the filter that rejects
                 the "wide margin" traps other tools show (stale/one-sided/thin).
  BUY / SELL  -- mean reversion: price is statistically cheap/expensive vs its
                 own recent history (z-score), with liquidity to act on it.

Every figure is net of the 2% GE tax and respects buy limits. Positions are
sized against your bankroll.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .analytics import market_table
from .config import (
    DEFAULT_BANKROLL,
    DEFAULT_MIN_MARGIN,
    DEFAULT_MIN_VOLUME,
)
from .db import connect

log = logging.getLogger("signals")


@dataclass
class Thresholds:
    min_volume: int = DEFAULT_MIN_VOLUME      # min units on the THINNER side
    max_price_age_min: float = 90.0           # ignore prices staler than this
    min_net_margin: int = DEFAULT_MIN_MARGIN  # gp, after tax
    min_roi: float = 0.004                    # 0.4% after tax
    z_buy: float = -1.5
    z_strong_buy: float = -2.5
    z_sell: float = 1.5
    z_strong_sell: float = 2.5
    bankroll: int = DEFAULT_BANKROLL
    max_alloc_frac: float = 0.15              # cap any single position at 15% of bankroll


def _jsonify(v):
    if v is None or v is pd.NaT:
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


def _records(df: pd.DataFrame, cols: list[str]) -> list[dict]:
    present = [c for c in cols if c in df.columns]
    return [{k: _jsonify(v) for k, v in rec.items()} for rec in df[present].to_dict("records")]


def enrich(df: pd.DataFrame, th: Thresholds) -> pd.DataFrame:
    """Add liquidity/quality flags, the signal label, ranking scores and sizing."""
    if df.empty:
        return df
    d = df.copy()

    d["vol_side"] = pd.concat([d["high_vol"], d["low_vol"]], axis=1).min(axis=1)
    d["vol_ok"] = d["vol_side"].fillna(0) >= th.min_volume
    d["fresh_ok"] = d["price_age_min"].notna() & (d["price_age_min"] <= th.max_price_age_min)
    d["tradeable"] = d["vol_ok"] & d["fresh_ok"]
    d["flip_ok"] = (
        d["tradeable"]
        & (d["net_margin"] >= th.min_net_margin)
        & (d["roi"] >= th.min_roi)
    )

    z = d["z_7d"]
    conditions = [
        ~d["tradeable"],
        z <= th.z_strong_buy,
        z <= th.z_buy,
        z >= th.z_strong_sell,
        z >= th.z_sell,
        d["flip_ok"],
    ]
    labels = ["ILLIQUID", "STRONG_BUY", "BUY", "STRONG_SELL", "SELL", "FLIP"]
    d["signal"] = np.select(conditions, labels, default="HOLD")

    d["flip_score"] = d["profit_per_cycle"].where(d["flip_ok"], other=np.nan)
    d["reversion_score"] = z.abs().where(d["signal"].isin(["STRONG_BUY", "BUY", "STRONG_SELL", "SELL"]))

    # Position sizing: cap each position at max_alloc_frac of bankroll AND the buy limit.
    buy = d["buy_price"].astype("float64")
    cap_units = np.floor((th.max_alloc_frac * th.bankroll) / buy.replace(0, np.nan))
    limit = d["buy_limit"].astype("float64")
    units = np.minimum(limit.fillna(cap_units), cap_units)
    units = np.where(np.isfinite(units), np.maximum(units, 0), 0)
    d["sugg_units"] = units
    d["sugg_capital"] = units * buy
    d["sugg_profit"] = units * d["net_margin"]
    d["affordable"] = units >= 1
    return d


def market_signals(th: Thresholds | None = None, con=None) -> pd.DataFrame:
    th = th or Thresholds()
    own = con is None
    con = con or connect(read_only=True)
    try:
        table = market_table(con, bankroll=th.bankroll)
    finally:
        if own:
            con.close()
    return enrich(table, th)


# Columns returned to the API / dashboard.
TABLE_COLS = [
    "item_id", "name", "members", "exempt", "buy_limit",
    "buy_price", "sell_price", "mid",
    "tax", "gross_margin", "net_margin", "roi",
    "profit_per_cycle", "sugg_units", "sugg_capital", "sugg_profit", "affordable",
    "high_vol", "low_vol", "vol_side", "vol_daily_7d", "price_age_min",
    "mean_7d", "sd_7d", "z_7d", "pct_30d", "volatility_7d",
    "min_30d", "max_30d",
    "signal", "flip_ok", "tradeable", "flip_score", "reversion_score",
]


def flip_table(th: Thresholds | None = None, con=None, limit: int = 100) -> list[dict]:
    d = market_signals(th, con)
    if d.empty:
        return []
    d = d[d["flip_ok"]].sort_values("flip_score", ascending=False).head(limit)
    return _records(d, TABLE_COLS)


def reversion_table(th: Thresholds | None = None, con=None, limit: int = 100) -> list[dict]:
    d = market_signals(th, con)
    if d.empty:
        return []
    d = d[d["signal"].isin(["STRONG_BUY", "BUY", "STRONG_SELL", "SELL"])]
    d = d.sort_values("reversion_score", ascending=False).head(limit)
    return _records(d, TABLE_COLS)


def full_table(th: Thresholds | None = None, con=None) -> list[dict]:
    d = market_signals(th, con)
    return _records(d, TABLE_COLS) if not d.empty else []


def _fmt(n) -> str:
    if n is None or (isinstance(n, float) and math.isnan(n)):
        return "-"
    return f"{int(n):,}"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    th = Thresholds()
    d = market_signals(th)
    if d.empty:
        print("No data. Seed demo data (python -m app.mockdata) or run the collector.")
        return

    print(f"\n=== TOP FLIPS (net of 2% tax, volume+freshness filtered) - bankroll {_fmt(th.bankroll)} ===")
    flips = d[d["flip_ok"]].sort_values("flip_score", ascending=False).head(12)
    print(f"{'item':<26}{'buy':>12}{'sell':>12}{'net/ea':>9}{'roi':>7}{'limit':>8}{'profit/cycle':>15}")
    for _, r in flips.iterrows():
        print(f"{str(r['name'])[:25]:<26}{_fmt(r['buy_price']):>12}{_fmt(r['sell_price']):>12}"
              f"{_fmt(r['net_margin']):>9}{(r['roi']*100):>6.1f}%{_fmt(r['buy_limit']):>8}{_fmt(r['profit_per_cycle']):>15}")

    print("\n=== MEAN-REVERSION SIGNALS (statistically cheap / expensive now) ===")
    rev = d[d["signal"].isin(["STRONG_BUY", "BUY", "STRONG_SELL", "SELL"])].sort_values("reversion_score", ascending=False).head(12)
    if rev.empty:
        print("(none right now)")
    print(f"{'item':<26}{'signal':>12}{'z-score':>9}{'mid':>12}{'7d mean':>12}{'30d %ile':>10}")
    for _, r in rev.iterrows():
        pct = r["pct_30d"] * 100 if pd.notna(r["pct_30d"]) else float("nan")
        print(f"{str(r['name'])[:25]:<26}{r['signal']:>12}{r['z_7d']:>9.2f}{_fmt(r['mid']):>12}"
              f"{_fmt(r['mean_7d']):>12}{pct:>9.0f}%")
    print()


if __name__ == "__main__":
    main()

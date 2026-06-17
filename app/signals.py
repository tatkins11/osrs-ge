"""Signal & flip-finder engine.

Produces ranked, actionable signals:

  FLIP        -- spread capture: net (after-tax) margin with real volume on
                 BOTH sides and a fresh price. Rejects the stale/one-sided
                 "wide margin" traps other tools show.
  BUY / SELL  -- mean reversion: price is statistically cheap/expensive vs its
                 own recent history. Each carries a TRADE PLAN: where to buy,
                 the fair-value target to sell at, the expected after-tax
                 profit/ROI, a confidence score, and the plain-English "why".

Every figure is net of the 2% GE tax and respects buy limits. Positions are
sized against your bankroll.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import tax as taxmod
from .analytics import market_table
from .config import DEFAULT_BANKROLL, DEFAULT_MIN_MARGIN, DEFAULT_MIN_VOLUME
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
    """Add liquidity/quality flags, the signal label, ranking scores, a
    mean-reversion trade plan, confidence, and position sizing."""
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

    # Rank flips by profit AND durability: a margin that held all week beats a one-tick blip.
    persist = d["margin_uptime"].fillna(0.25) if "margin_uptime" in d.columns else 0.25
    d["flip_score"] = (d["profit_per_cycle"] * (0.3 + 0.7 * persist)).where(d["flip_ok"], other=np.nan)
    d["reversion_score"] = z.abs().where(d["signal"].isin(["STRONG_BUY", "BUY", "STRONG_SELL", "SELL"]))

    # --- mean-reversion trade plan: where to act, the fair-value target, the gain ---
    mean = d["mean_7d"].astype("float64")
    high = d["sell_price"].astype("float64")   # current instabuy (sell into / buy now)
    is_buy = d["signal"].isin(["BUY", "STRONG_BUY"])
    is_sell = d["signal"].isin(["SELL", "STRONG_SELL"])
    exempt_arr = d["exempt"].to_numpy()
    net_mean = mean - taxmod.sell_tax_array(mean, exempt_arr)   # after-tax if sold at fair value
    net_high = high - taxmod.sell_tax_array(high, exempt_arr)   # after-tax if sold at current high
    buy_margin = net_mean - high     # BUY: buy ~now, sell at fair value
    sell_margin = net_high - mean    # SELL: sell high now, rebuy at fair value (round trip)
    d["mr_target"] = mean
    d["mr_entry"] = high
    d["mr_exp_margin"] = np.where(is_buy, buy_margin, np.where(is_sell, sell_margin, np.nan))
    d["mr_exp_roi"] = np.where(
        is_buy, np.where(high > 0, buy_margin / high, np.nan),
        np.where(is_sell, np.where(mean > 0, sell_margin / mean, np.nan), np.nan),
    )
    strength = np.clip((d["z_7d"].abs() - 1.5) / 2.0 + 0.5, 0.0, 1.0)   # 0.5 @1.5σ -> 1.0 @2.5σ+
    liq = np.clip(np.log10(d["vol_daily_7d"].clip(lower=1)) / 6.0, 0.0, 1.0)  # ~1 @ 1e6/day
    d["confidence"] = (100 * (0.6 * strength + 0.4 * liq)).round()

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


def _reasons(r: dict) -> list[str]:
    """Plain-English thesis bullets for a mean-reversion signal record."""
    out: list[str] = []
    z, mean, mid = r.get("z_7d"), r.get("mean_7d"), r.get("mid")
    pct30, vold, sig = r.get("pct_30d"), r.get("vol_daily_7d"), r.get("signal", "")
    if z is not None and mean and mid:
        dev = abs((mid - mean) / mean) * 100
        if z < 0:
            out.append(f"Trading {abs(z):.1f}σ below its 7-day average — ~{dev:.0f}% cheaper than usual")
        else:
            out.append(f"Trading {abs(z):.1f}σ above its 7-day average — ~{dev:.0f}% pricier than usual")
    if pct30 is not None:
        where = "bottom" if sig in ("BUY", "STRONG_BUY") else "top"
        out.append(f"Near the {where} of its 30-day range ({pct30 * 100:.0f}th percentile)")
    if vold:
        out.append(f"~{vold:,.0f} traded per day — liquid enough to get in and out")
    if sig in ("BUY", "STRONG_BUY"):
        out.append("Plan: buy near the current price, place a sell offer at the fair-value target, wait for it to revert up")
    elif sig in ("SELL", "STRONG_SELL"):
        out.append("Plan: if you hold it, sell into this elevated price; optionally rebuy near the fair-value target")
    return out


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
    "high_vol", "low_vol", "vol_side", "vol_daily_7d", "margin_uptime", "margin_median_7d", "price_age_min",
    "mean_7d", "sd_7d", "z_7d", "pct_30d", "volatility_7d", "min_30d", "max_30d",
    "mr_entry", "mr_target", "mr_exp_margin", "mr_exp_roi", "confidence",
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
    recs = _records(d, TABLE_COLS)
    for rec in recs:
        rec["reasons"] = _reasons(rec)
    return recs


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
    print(f"{'item':<24}{'buy':>11}{'sell':>11}{'net/ea':>8}{'roi':>6}{'uptime':>8}{'profit/4h':>13}")
    for _, r in flips.iterrows():
        up = r["margin_uptime"] * 100 if pd.notna(r["margin_uptime"]) else 0.0
        print(f"{str(r['name'])[:23]:<24}{_fmt(r['buy_price']):>11}{_fmt(r['sell_price']):>11}"
              f"{_fmt(r['net_margin']):>8}{(r['roi']*100):>5.1f}%{up:>7.0f}%{_fmt(r['profit_per_cycle']):>13}")

    print("\n=== MEAN-REVERSION SIGNALS (buy cheap / sell expensive vs fair value) ===")
    rev = d[d["signal"].isin(["STRONG_BUY", "BUY", "STRONG_SELL", "SELL"])].sort_values("reversion_score", ascending=False).head(12)
    if rev.empty:
        print("(none right now)")
    else:
        print(f"{'item':<24}{'signal':>11}{'now':>11}{'fair value':>12}{'exp/ea':>9}{'roi':>7}{'conf':>6}")
        for _, r in rev.iterrows():
            roi = r["mr_exp_roi"] * 100 if pd.notna(r["mr_exp_roi"]) else 0.0
            conf = int(r["confidence"]) if pd.notna(r["confidence"]) else 0
            print(f"{str(r['name'])[:23]:<24}{r['signal']:>11}{_fmt(r['mr_entry']):>11}{_fmt(r['mr_target']):>12}"
                  f"{_fmt(r['mr_exp_margin']):>9}{roi:>6.1f}%{conf:>6}")
    print()


if __name__ == "__main__":
    main()

"""Signal & flip-finder engine.

Produces ranked, actionable signals:

  FLIP        -- spread capture: net (after-tax) margin with real volume on
                 BOTH sides and a fresh price. Rejects stale/one-sided "wide
                 margin" traps, low-value junk, and trades too small to matter.
  BUY / SELL  -- mean reversion (experimental): price is statistically
                 cheap/expensive vs its own recent history, with a trade plan.

Quality gates (tunable): minimum profit per trade (sized to YOUR bankroll +
buy limits) and a minimum item price, so only meaningful trades surface.
Every figure is net of the 2% GE tax.
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
    min_gp_volume: int = 25_000_000           # ...OR admit any item turning over this much gp/day (high-value, low unit vol)
    max_price_age_min: float = 360.0          # ignore prices staler than this (6h; low-volume items trade less often)
    min_net_margin: int = DEFAULT_MIN_MARGIN  # gp, after tax
    min_roi: float = 0.004                    # 0.4% after tax
    min_profit: int = 500_000                 # min profit per trade (bankroll+limit sized)
    min_price: int = 1_000                    # skip low-value junk below this price
    max_price: int = 2_147_483_647            # price-range ceiling (default = no cap)
    crash_pct: float = 0.18                   # crash = this far below the established (7d median) level
    crash_recover_to: float = 0.95            # recovery target as a fraction of the established level
    value_min_discount: float = 0.08          # value buy: at least this far below the established level
    value_min_confidence: int = 40            # value buy: minimum 0-100 confidence to surface
    vol_spike: float = 2.0                    # "unusual volume": last 24h >= this multiple of a typical day
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
    """Add liquidity/quality flags, position sizing, the signal label, a
    mean-reversion trade plan, and confidence."""
    if df.empty:
        return df
    d = df.copy()

    d["vol_side"] = pd.concat([d["high_vol"], d["low_vol"]], axis=1).min(axis=1)
    # gp turnover/day lets high-value, low-unit-volume items (megarares, big gear) qualify
    # even though they don't trade 100+ units every 5 minutes.
    d["gp_turnover"] = d["mid"].fillna(0).astype("float64") * d["vol_daily_7d"].fillna(0).astype("float64")
    d["vol_ok"] = (d["vol_side"].fillna(0) >= th.min_volume) | (d["gp_turnover"] >= th.min_gp_volume)
    d["fresh_ok"] = d["price_age_min"].notna() & (d["price_age_min"] <= th.max_price_age_min)
    d["tradeable"] = d["vol_ok"] & d["fresh_ok"]

    # Position sizing first: cap each position at max_alloc_frac of bankroll AND the buy limit.
    buy = d["buy_price"].astype("float64")
    cap_units = np.floor((th.max_alloc_frac * th.bankroll) / buy.replace(0, np.nan))
    limit = d["buy_limit"].astype("float64")
    units = np.minimum(limit.fillna(cap_units), cap_units)
    units = np.where(np.isfinite(units), np.maximum(units, 0), 0)
    d["sugg_units"] = units
    d["sugg_capital"] = units * buy
    d["sugg_profit"] = units * d["net_margin"]              # realistic per-trade profit for YOU
    d["affordable"] = units >= 1

    # Volume-aware realistic profit per 4h: you can't fill the full buy limit if the item
    # barely trades. Throttle by ~one side's flow in a 4h window (vol_daily_7d / 12). For
    # liquid items the buy limit binds (unchanged); for thin items this kills the fantasy
    # of "net_margin x buy_limit" on something that trades a handful of times a day.
    flow_4h = d["vol_daily_7d"].fillna(0.0).astype("float64") / 12.0
    d["units_per_4h"] = np.minimum(limit.fillna(0.0), flow_4h)
    d["realistic_profit"] = d["net_margin"] * d["units_per_4h"]

    # Flip quality gates: liquid, fresh, not junk, and realistically worth >= min_profit/4h.
    d["flip_ok"] = (
        d["tradeable"]
        & (buy >= th.min_price)
        & (buy <= th.max_price)
        & (d["net_margin"] >= th.min_net_margin)
        & (d["roi"] >= th.min_roi)
        & (d["realistic_profit"].fillna(0) >= th.min_profit)
    )

    # Mean-reversion signals only on liquid, non-junk items.
    eligible = d["tradeable"] & (d["mid"].fillna(0) >= th.min_price) & (d["mid"].fillna(0) <= th.max_price)
    z = d["z_7d"]
    conditions = [
        ~eligible,
        z <= th.z_strong_buy,
        z <= th.z_buy,
        z >= th.z_strong_sell,
        z >= th.z_sell,
        d["flip_ok"],
    ]
    labels = ["ILLIQUID", "STRONG_BUY", "BUY", "STRONG_SELL", "SELL", "FLIP"]
    d["signal"] = np.select(conditions, labels, default="HOLD")

    # Rank flips by profit AND durability: a margin that held all week beats a blip.
    persist = d["margin_uptime"].fillna(0.25) if "margin_uptime" in d.columns else 0.25
    d["flip_score"] = (d["realistic_profit"] * (0.3 + 0.7 * persist)).where(d["flip_ok"], other=np.nan)
    d["reversion_score"] = z.abs().where(d["signal"].isin(["STRONG_BUY", "BUY", "STRONG_SELL", "SELL"]))

    # Mean-reversion trade plan: where to act, the fair-value target, the gain.
    mean = d["mean_7d"].astype("float64")
    high = d["sell_price"].astype("float64")   # current instabuy
    is_buy = d["signal"].isin(["BUY", "STRONG_BUY"])
    is_sell = d["signal"].isin(["SELL", "STRONG_SELL"])
    exempt_arr = d["exempt"].to_numpy()
    net_mean = mean - taxmod.sell_tax_array(mean, exempt_arr)
    net_high = high - taxmod.sell_tax_array(high, exempt_arr)
    buy_margin = net_mean - high
    sell_margin = net_high - mean
    d["mr_target"] = mean
    d["mr_entry"] = high
    d["mr_exp_margin"] = np.where(is_buy, buy_margin, np.where(is_sell, sell_margin, np.nan))
    d["mr_exp_roi"] = np.where(
        is_buy, np.where(high > 0, buy_margin / high, np.nan),
        np.where(is_sell, np.where(mean > 0, sell_margin / mean, np.nan), np.nan),
    )
    d["mr_exp_profit"] = d["mr_exp_margin"] * d["buy_limit"].astype("float64")   # full buy-limit expected gain
    strength = np.clip((d["z_7d"].abs() - 1.5) / 2.0 + 0.5, 0.0, 1.0)
    liq = np.clip(np.log10(d["vol_daily_7d"].clip(lower=1)) / 6.0, 0.0, 1.0)
    d["confidence"] = (100 * (0.6 * strength + 0.4 * liq)).round()

    # --- crash-&-recover signal (backtest-validated: ~59% win, PF ~2 even paying the spread) ---
    established = d["median_7d"].fillna(d["mean_7d"]).astype("float64")
    d["established"] = established
    d["drawdown"] = np.where(established > 0, (d["mid"] - established) / established, np.nan)
    d["is_crash"] = (
        d["tradeable"]
        & (d["mid"].fillna(0) >= th.min_price)
        & (d["mid"].fillna(0) <= th.max_price)
        & (d["drawdown"] <= -th.crash_pct)
    )
    crash_buy = d["sell_price"].astype("float64")                 # current insta-buy (what you pay)
    crash_target = established * th.crash_recover_to              # recovery target
    net_target = crash_target - taxmod.sell_tax_array(crash_target, exempt_arr)
    d["crash_target"] = crash_target
    d["crash_exp_margin"] = net_target - crash_buy               # per item, after tax
    d["crash_exp_roi"] = np.where(crash_buy > 0, d["crash_exp_margin"] / crash_buy, np.nan)
    d["crash_exp_profit"] = d["crash_exp_margin"] * d["buy_limit"].astype("float64")

    # --- value-buy signal: undervalued vs a STABLE established level (the validated reversion edge,
    # generalised across discount depths + horizons, with a confidence score) ---
    mean30 = d["mean_30d"].astype("float64")
    d["value_discount"] = np.where(established > 0, (established - d["mid"]) / established, np.nan)  # +ve = below fair
    d["level_health"] = np.where(mean30 > 0, established / mean30, np.nan)   # ~1 stable; <1 = the level itself is falling
    buy_px = d["sell_price"].astype("float64")                              # what you pay to buy now (instabuy)
    d["value_target"] = established                                         # fair value = 7d established level
    net_v = established - taxmod.sell_tax_array(established, exempt_arr)
    d["value_exp_margin"] = net_v - buy_px
    d["value_exp_roi"] = np.where(buy_px > 0, d["value_exp_margin"] / buy_px, np.nan)
    d["value_exp_profit"] = d["value_exp_margin"] * d["buy_limit"].astype("float64")
    gpv = (d["mid"].fillna(0) * d["vol_daily_7d"].fillna(0)).astype("float64")
    # confidence 0-100: discount depth + how-unusual (z) + cheapness + liquidity + level-health (falling-knife guard)
    disc = np.clip(d["value_discount"].fillna(0) / 0.30, 0, 1)
    zc = np.clip((-d["z_7d"].fillna(0) - 1.0) / 2.5, 0, 1)                  # rewards trading >=1sigma below the mean
    cheap = np.clip(1.0 - d["pct_30d"].fillna(0.5), 0, 1)                   # near the bottom of the 30d range
    liq = np.clip((np.log10(gpv.clip(lower=1)) - 6.0) / 3.0, 0, 1)         # 1M..1B gp/day
    health = np.clip((d["level_health"].fillna(0) - 0.75) / 0.25, 0, 1)    # 0.75->0, >=1.0->1 (stable level)
    d["value_confidence"] = (100 * (0.30 * disc + 0.22 * zc + 0.18 * cheap + 0.15 * liq + 0.15 * health)).round()
    # horizon by liquidity (study: liquid dislocations revert in days; thin / high-value take longer)
    d["value_horizon"] = np.select([gpv >= 200_000_000, gpv >= 20_000_000],
                                    ["1-3 days", "~1 week"], default="2-4 weeks")
    d["is_value_buy"] = (
        d["tradeable"]
        & (buy_px >= th.min_price) & (buy_px <= th.max_price)
        & (d["value_discount"].fillna(0) >= th.value_min_discount)
        & (d["value_exp_roi"].fillna(0) >= th.min_roi)
        & (d["level_health"].fillna(0) >= 0.55)                            # exclude sustained decliners (falling knives)
        & (d["value_confidence"].fillna(0) >= th.value_min_confidence)
    )
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
        where = "bottom" if (pct30 < 0.5) else "top"
        out.append(f"Near the {where} of its 30-day range ({pct30 * 100:.0f}th percentile)")
    if vold:
        out.append(f"~{vold:,.0f} traded per day — liquid enough to get in and out")
    if sig in ("BUY", "STRONG_BUY"):
        out.append("Plan: buy near the current price, sell at the fair-value target, wait for it to revert up")
    elif sig in ("SELL", "STRONG_SELL"):
        out.append("Plan: if you hold it, sell into this elevated price; optionally rebuy near fair value")
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


TABLE_COLS = [
    "item_id", "name", "members", "exempt", "buy_limit",
    "buy_price", "sell_price", "mid",
    "tax", "gross_margin", "net_margin", "roi",
    "profit_per_cycle", "realistic_profit", "units_per_4h", "sugg_units", "sugg_capital", "sugg_profit", "affordable",
    "high_vol", "low_vol", "vol_side", "vol_daily_7d", "vol_24h", "vol_ratio", "chg_24h", "margin_uptime", "margin_median_7d", "price_age_min",
    "mean_7d", "sd_7d", "z_7d", "pct_30d", "volatility_7d", "min_30d", "max_30d",
    "mr_entry", "mr_target", "mr_exp_margin", "mr_exp_roi", "mr_exp_profit", "confidence",
    "established", "drawdown", "crash_target", "crash_exp_margin", "crash_exp_roi", "crash_exp_profit", "is_crash",
    "value_discount", "level_health", "value_target", "value_exp_margin", "value_exp_roi", "value_exp_profit",
    "value_confidence", "value_horizon", "is_value_buy",
    "signal", "flip_ok", "tradeable", "flip_score", "reversion_score",
]


def flip_table(th: Thresholds | None = None, con=None, limit: int = 100) -> list[dict]:
    d = market_signals(th, con)
    if d.empty:
        return []
    d = d[d["flip_ok"]].sort_values("flip_score", ascending=False).head(limit)
    return _records(d, TABLE_COLS)


def reversion_table(th: Thresholds | None = None, con=None, limit: int = 100) -> list[dict]:
    th = th or Thresholds()
    d = market_signals(th, con)
    if d.empty:
        return []
    d = d[
        d["signal"].isin(["STRONG_BUY", "BUY", "STRONG_SELL", "SELL"])
        & (d["mr_exp_profit"].fillna(-1) >= th.min_profit)
    ]
    d = d.sort_values("reversion_score", ascending=False).head(limit)
    recs = _records(d, TABLE_COLS)
    for rec in recs:
        rec["reasons"] = _reasons(rec)
    return recs


def crash_table(th: Thresholds | None = None, con=None, limit: int = 100) -> list[dict]:
    """Items currently crashed >= crash_pct below their established level, with a
    recovery trade plan. Filtered to meaningful profit + price."""
    th = th or Thresholds()
    d = market_signals(th, con)
    if d.empty:
        return []
    d = d[d["is_crash"] & (d["crash_exp_profit"].fillna(0) >= th.min_profit)]
    d = d.sort_values("crash_exp_profit", ascending=False).head(limit)
    return _records(d, TABLE_COLS)


def volume_table(th: Thresholds | None = None, con=None, limit: int = 100) -> list[dict]:
    """Items 'in play': last-24h volume >= vol_spike x a typical day. An early-warning
    screen (news / meta shift / manipulation), not a standalone buy signal — chg_24h
    shows which way it's moving so far."""
    th = th or Thresholds()
    d = market_signals(th, con)
    if d.empty:
        return []
    d = d[
        (d["vol_ratio"].fillna(0) >= th.vol_spike)
        & (d["vol_daily_7d"].fillna(0) >= 100)
        & (d["mid"].fillna(0) >= th.min_price)
        & (d["mid"].fillna(0) <= th.max_price)
    ]
    d = d.sort_values("vol_ratio", ascending=False).head(limit)
    return _records(d, TABLE_COLS)


def invest_table(th: Thresholds | None = None, con=None, limit: int = 100) -> list[dict]:
    """Buy-only value finder: items undervalued vs a stable established level, ranked by
    confidence then expected ROI. Each carries a recovery target, upside, and a horizon."""
    th = th or Thresholds()
    d = market_signals(th, con)
    if d.empty:
        return []
    d = d[d["is_value_buy"]].sort_values(["value_confidence", "value_exp_roi"], ascending=False).head(limit)
    recs = _records(d, TABLE_COLS)
    for r in recs:
        r["reasons"] = _value_reasons(r)
    return recs


def _value_reasons(r: dict) -> list[str]:
    out: list[str] = []
    disc, z, est, mid = r.get("value_discount"), r.get("z_7d"), r.get("established"), r.get("mid")
    pct30, vold, hz = r.get("pct_30d"), r.get("vol_daily_7d"), r.get("level_health")
    if disc and est:
        out.append(f"Trading ~{disc * 100:.0f}% below its 7-day established level (~{est:,.0f} gp)")
    if z is not None and z < 0:
        out.append(f"{abs(z):.1f}sigma below its 7-day mean — an unusual dislocation")
    if pct30 is not None and pct30 < 0.5:
        out.append(f"Near the bottom of its 30-day range ({pct30 * 100:.0f}th pct)")
    if hz is not None:
        out.append("Established level has held vs the 30-day average — a dislocation, not a decline"
                   if hz >= 0.92 else "Level is somewhat soft vs the 30-day average — watch for further weakness")
    if vold:
        out.append(f"~{vold:,.0f} traded/day — liquid enough to enter and exit")
    return [x for x in out if x]


def full_table(th: Thresholds | None = None, con=None) -> list[dict]:
    th = th or Thresholds()
    d = market_signals(th, con)
    if d.empty:
        return []
    buy = d["buy_price"].fillna(0)
    d = d[(buy >= th.min_price) & (buy <= th.max_price)]
    return _records(d, TABLE_COLS)


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

    print(f"\n=== TOP FLIPS (>= {_fmt(th.min_profit)} profit, price >= {_fmt(th.min_price)}, after 2% tax) ===")
    flips = d[d["flip_ok"]].sort_values("flip_score", ascending=False).head(15)
    if flips.empty:
        print("(none clear the profit/price bar right now — lower Min profit if you want more)")
    else:
        print(f"{'item':<24}{'buy':>11}{'sell':>11}{'net/ea':>8}{'roi':>6}{'uptime':>8}{'profit/4h':>13}")
        for _, r in flips.iterrows():
            up = r["margin_uptime"] * 100 if pd.notna(r["margin_uptime"]) else 0.0
            print(f"{str(r['name'])[:23]:<24}{_fmt(r['buy_price']):>11}{_fmt(r['sell_price']):>11}"
                  f"{_fmt(r['net_margin']):>8}{(r['roi']*100):>5.1f}%{up:>7.0f}%{_fmt(r['profit_per_cycle']):>13}")

    print(f"\n=== MEAN-REVERSION SIGNALS (>= {_fmt(th.min_profit)} expected, experimental) ===")
    rev = d[d["signal"].isin(["STRONG_BUY", "BUY", "STRONG_SELL", "SELL"]) & (d["mr_exp_profit"].fillna(-1) >= th.min_profit)]
    rev = rev.sort_values("reversion_score", ascending=False).head(12)
    if rev.empty:
        print("(none clear the bar right now)")
    else:
        print(f"{'item':<24}{'signal':>11}{'now':>11}{'fair value':>12}{'exp profit':>12}{'conf':>6}")
        for _, r in rev.iterrows():
            conf = int(r["confidence"]) if pd.notna(r["confidence"]) else 0
            print(f"{str(r['name'])[:23]:<24}{r['signal']:>11}{_fmt(r['mr_entry']):>11}{_fmt(r['mr_target']):>12}"
                  f"{_fmt(r['mr_exp_profit']):>12}{conf:>6}")
    print()


if __name__ == "__main__":
    main()

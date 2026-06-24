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
from .db import connect, get_updates_df

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
    update_drop_penalty: float = 15.0         # value-confidence penalty if the drop landed ~2d after a game update
    update_drop_factor: float = 0.5           # crash-rank down-weight for the same update-driven drop
    update_drop_days: int = 2                 # window (days) after an update for a drop to count as update-driven
    overnight_disc: float = 0.10              # overnight lowball: buy offer this far below the current bid
    overnight_min_margin: float = 3000.0      # min gp captured PER ITEM — favours high-value items over high-qty junk
    vol_spike: float = 2.0                    # "unusual volume": last 24h >= this multiple of a typical day
    z_buy: float = -1.5
    z_strong_buy: float = -2.5
    z_sell: float = 1.5
    z_strong_sell: float = 2.5
    bankroll: int = DEFAULT_BANKROLL          # 8-Slot Plan treats this as FREE gp (deployable cash now)
    max_alloc_frac: float = 0.15              # cap any single position at 15% of bankroll
    min_rt_profit: int = 500_000             # 8-Slot Plan: skip buys whose per-round-trip profit is below this


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


def _nearest_update(drop_secs: np.ndarray, up_secs: np.ndarray, up_titles: np.ndarray, win: float):
    """Flag drops that occurred 0..`win` secs AFTER a game update (causal direction: the update
    precedes the drop, matching the validated `--study affected` '<=2d after update' cell).
    Returns (is_near bool[], preceding_update_title object[]). `up_secs` must be sorted ascending."""
    n = len(drop_secs)
    near = np.zeros(n, dtype=bool)
    titles = np.full(n, None, dtype=object)
    if up_secs.size == 0:
        return near, titles
    idx = np.searchsorted(up_secs, np.nan_to_num(drop_secs, nan=-1.0), side="right") - 1  # last update <= drop
    j = np.clip(idx, 0, up_secs.size - 1)
    dist = drop_secs - up_secs[j]                          # secs since that update; NaN drop -> NaN
    near = (idx >= 0) & np.isfinite(dist) & (dist >= 0) & (dist <= win)
    for k in np.nonzero(near)[0]:
        titles[k] = up_titles[j[k]]
    return near, titles


def enrich(df: pd.DataFrame, th: Thresholds, updates: pd.DataFrame | None = None) -> pd.DataFrame:
    """Add liquidity/quality flags, position sizing, the signal label, a
    mean-reversion trade plan, and confidence. ``updates`` (ts/title) drives the
    update-proximity penalty on value buys."""
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
    # a NULL catalog buy_limit means "no limit", not "zero" — throttle by flow only
    d["units_per_4h"] = np.where(limit.notna(), np.minimum(limit, flow_4h), flow_4h)
    d["realistic_profit"] = d["net_margin"] * d["units_per_4h"]

    # After-slippage margin: real fills land near the MID, not the bid (slippage study: buys land
    # ~0.7% above mid), so you don't capture the buy-side spread the raw margin assumes. Haircut the
    # margin to a buy-at-mid assumption -> the honest edge you'll actually keep.
    midf = d["mid"].astype("float64")
    d["slip_margin"] = d["net_margin"].astype("float64") - (midf - buy).clip(lower=0)
    d["slip_roi"] = np.where(midf > 0, d["slip_margin"] / midf, np.nan)
    # Capital velocity (gp/hour): how fast a flip recycles capital. cycle ~= 2 x (units / hourly vol)
    # for the buy + sell legs. Relative ranking metric -- absolute is optimistic (assumes you capture
    # whole-market flow), but the ordering surfaces fast-recycling flips the raw-margin rank buries.
    hourly_vol = d["vol_daily_7d"].fillna(0.0).astype("float64") / 24.0
    unit_cycle = np.where(limit.notna() & (limit > 0), limit, flow_4h)
    fill_h = np.where(hourly_vol > 0, unit_cycle / np.maximum(hourly_vol, 1e-9), 240.0)
    cycle_h = np.clip(2.0 * fill_h, 0.5, 240.0)
    d["gp_per_h"] = (d["net_margin"].astype("float64") * unit_cycle) / cycle_h

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
    base = 100 * (0.30 * disc + 0.22 * zc + 0.18 * cheap + 0.15 * liq + 0.15 * health)
    # validated (crashcond study): dips that bottom near their high-alch floor recover far better
    # (support<=10%: 92% win / PF 23.7 vs far>30%: 56% / PF 1.09). Reward floor-supported dips; items
    # with no meaningful floor (support NaN -> 1.0) get no bonus rather than a penalty.
    floor_bonus = 20.0 * np.clip((0.15 - d["alch_support"].fillna(1.0)) / 0.15, 0, 1)
    conf = np.minimum(100.0, base + floor_bonus)
    # update-proximity penalty (validated `--study affected`: a sharp drop within ~2 days of a
    # game update recovers ~30% worse by PF -- more likely a permanent repricing / value trap).
    # We can only condition on DATE proximity (the wiki doesn't expose which item each update changed).
    d["post_update_drop"] = False
    d["post_update_title"] = None
    if "worst_drop_ts" in d.columns and updates is not None and not updates.empty:
        u = updates.dropna(subset=["ts"]).sort_values("ts")
        up_secs = pd.to_datetime(u["ts"]).astype("datetime64[s]").astype("int64").to_numpy("float64")
        up_titles = u["title"].to_numpy(object)
        drop_dt = pd.to_datetime(d["worst_drop_ts"], errors="coerce")
        ds = drop_dt.astype("datetime64[s]").astype("int64").to_numpy("float64")
        ds[~drop_dt.notna().to_numpy()] = np.nan
        near, near_title = _nearest_update(ds, up_secs, up_titles, th.update_drop_days * 86_400.0)
        real_drop = d["worst_1d_drop"].fillna(0.0).to_numpy("float64") <= -0.08   # require a genuinely sharp drop
        d["post_update_drop"] = near & real_drop
        d["post_update_title"] = np.where(d["post_update_drop"], near_title, None)
        conf = np.where(d["post_update_drop"], conf - th.update_drop_penalty, conf)
    d["value_confidence"] = np.clip(conf, 0.0, 100.0).round()
    # crash ranking shares the update-proximity logic: an update-driven crash recovers worse, so
    # down-weight it in the Crashes sort (display profit unchanged -- penalise, don't exclude).
    d["crash_score"] = d["crash_exp_profit"].astype("float64") * np.where(d["post_update_drop"], th.update_drop_factor, 1.0)
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
        updates = get_updates_df(con)
    finally:
        if own:
            con.close()
    return enrich(table, th, updates)


TABLE_COLS = [
    "item_id", "name", "members", "exempt", "buy_limit",
    "buy_price", "sell_price", "mid",
    "tax", "gross_margin", "net_margin", "roi",
    "profit_per_cycle", "realistic_profit", "slip_margin", "slip_roi", "gp_per_h", "units_per_4h", "sugg_units", "sugg_capital", "sugg_profit", "affordable",
    "high_vol", "low_vol", "vol_side", "vol_daily_7d", "vol_24h", "vol_ratio", "chg_24h", "margin_uptime", "margin_median_7d", "price_age_min",
    "mean_7d", "sd_7d", "z_7d", "pct_30d", "volatility_7d", "min_30d", "max_30d",
    "mr_entry", "mr_target", "mr_exp_margin", "mr_exp_roi", "mr_exp_profit", "confidence",
    "established", "drawdown", "crash_target", "crash_exp_margin", "crash_exp_roi", "crash_exp_profit", "crash_score", "is_crash",
    "value_discount", "level_health", "value_target", "value_exp_margin", "value_exp_roi", "value_exp_profit",
    "value_confidence", "value_horizon", "is_value_buy", "post_update_drop", "post_update_title",
    "alch_floor", "alch_support",
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
    d = d.sort_values("crash_score", ascending=False).head(limit)   # update-driven crashes ranked lower
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


def overnight_table(th: Thresholds | None = None, con=None, limit: int = 100) -> list[dict]:
    """Lowball overnight buy offers: place a buy ~overnight_disc below the bid on a
    liquid, stable item; it fills only if the price dumps overnight (caught while you
    sleep), then sell next day toward fair value. Backtest-validated as a modest,
    infrequent reversion edge (deep offers recover; shallow ones bleed the spread)."""
    from . import overnight as on
    th = th or Thresholds()
    own = con is None
    con = con or connect(read_only=True)
    try:
        d = market_signals(th, con)
        if d.empty:
            return []
        disc = th.overnight_disc
        bid = d["buy_price"].astype("float64")     # instasell (the bid)
        ask = d["sell_price"].astype("float64")    # instabuy (where you sell back next day)
        est = d["established"].astype("float64")
        exempt = d["exempt"].to_numpy()
        buy_offer = np.floor(bid * (1.0 - disc))
        net_t = ask - taxmod.sell_tax_array(ask, exempt)       # sell back at the current ask, after tax (best case)
        d["on_buy"] = buy_offer
        d["on_target"] = np.round(ask)
        d["on_margin"] = net_t - buy_offer                     # captured discount if it reverts, after tax & spread
        d["on_roi"] = np.where(buy_offer > 0, d["on_margin"] / buy_offer, np.nan)
        # absolute gp per filled order -- favours high-value items and mid-value with big buy
        # limits over tiny-but-high-fill-rate junk. No GE limit -> cap units by a day's volume.
        units = d["buy_limit"].astype("float64")
        units = units.where(units.notna() & (units > 0), d["vol_daily_7d"].astype("float64"))
        d["on_units"] = units
        d["on_exp_profit"] = d["on_margin"] * units
        gpv = d["mid"].fillna(0) * d["vol_daily_7d"].fillna(0)
        spread_pct = np.where(bid > 0, (ask - bid) / bid, np.nan)
        vol = d["volatility_7d"].fillna(0.0)
        elig = (
            d["tradeable"] & est.notna()
            & (buy_offer >= th.min_price) & (buy_offer <= th.max_price)
            & (d["drawdown"].abs() <= 0.06)                    # at fair value (bet on a FUTURE dump, not an existing crash)
            & d["level_health"].fillna(0).between(0.85, 1.3)   # stable established level (kills the noisy-median mirage)
            & (spread_pct <= 0.05)                             # tight spread -> can realistically sell back near the ask
            & vol.between(0.02, 0.8)
            & (d["on_margin"] > 0) & (d["on_roi"] >= th.min_roi)
            & (gpv >= 25_000_000)
            & (d["on_margin"].fillna(0) >= th.overnight_min_margin)   # meaningful gp PER ITEM -> value over quantity
        )
        cand = d[elig].copy()
        if cand.empty:
            return []
        # real historical odds: how often the lowball fills overnight, and wins when it does
        exempt_map = dict(zip(cand["item_id"].astype(int).tolist(), cand["exempt"].astype(bool).tolist()))
        stats = on.fill_stats(cand["item_id"].tolist(), con, disc, exempt_map=exempt_map)
    finally:
        if own:
            con.close()

    cand["on_fill_prob"] = cand["item_id"].map(lambda i: (stats.get(int(i)) or {}).get("fill_prob"))
    cand["on_win_rate"] = cand["item_id"].map(lambda i: (stats.get(int(i)) or {}).get("win_rate"))
    cand["on_exp_margin"] = cand["item_id"].map(lambda i: (stats.get(int(i)) or {}).get("exp_margin"))
    cand["on_nights"] = cand["item_id"].map(lambda i: (stats.get(int(i)) or {}).get("nights"))
    # rank by per-ITEM expected gp (margin × odds) — surfaces high-value lowballs, not high-qty junk
    cand["on_ev"] = cand["on_margin"].fillna(0) * cand["on_fill_prob"].fillna(0) * cand["on_win_rate"].fillna(0)
    # keep only realistic, positive-edge setups: a meaningful fill chance AND a winning history when filled
    keep = (cand["on_fill_prob"].fillna(0) >= 0.30) & (cand["on_win_rate"].fillna(0) >= 0.55)
    cand = cand[keep]
    if cand.empty:
        return []
    cand = cand.sort_values("on_ev", ascending=False).head(limit)   # expected gp/night, not just fill odds
    return _records(cand, TABLE_COLS + ["on_buy", "on_target", "on_margin", "on_roi",
                                        "on_fill_prob", "on_win_rate", "on_exp_margin", "on_nights",
                                        "on_units", "on_exp_profit", "on_ev"])


def slot_allocator(th: Thresholds | None = None, con=None, free_slots: int = 8,
                   capital: float | None = None, exclude_items=None) -> dict:
    """Pick the best set of flips to fill your FREE Grand Exchange slots right now to maximize
    total gp/day, given available capital + per-item buy limits. Greedy by capital velocity
    (gp/hour), one item per slot, only flips with a positive AFTER-SLIPPAGE margin, capital
    deducted as each is allocated. Recommender only -- you place the offers yourself."""
    th = th or Thresholds()
    own = con is None
    con = con or connect(read_only=True)
    try:
        d = market_signals(th, con)
    finally:
        if own:
            con.close()
    cap0 = float(capital if capital is not None else th.bankroll)
    excl = {int(x) for x in (exclude_items or [])}
    out = {"free_slots": int(free_slots), "capital_in": round(cap0), "recommendations": [],
           "total_capital": 0, "total_gp_day": 0, "utilization": 0.0, "skipped_no_capital": 0}
    if d.empty or free_slots <= 0 or cap0 <= 0:
        return out
    c = d[d["flip_ok"].fillna(False) & ~d["item_id"].astype(int).isin(excl)
          & (d["slip_margin"].fillna(-1.0) > 0)].sort_values("gp_per_h", ascending=False)
    # cap any one slot's capital so the bankroll spreads across the 8 parallel slots (and respects
    # the user's per-position risk limit) instead of dumping everything into the single best item
    per_slot_cap = float(th.max_alloc_frac or 0) * float(th.bankroll or cap0)
    remaining, recs, skipped = cap0, [], 0
    for r in c.itertuples():
        if len(recs) >= free_slots:
            break
        buy = float(r.buy_price or 0)
        if buy <= 0:
            continue
        if remaining < buy:
            skipped += 1
            continue
        limit = max(1.0, float(r.buy_limit) if pd.notna(r.buy_limit) else (float(r.vol_daily_7d or 0) / 12.0))
        slot_cap_units = (per_slot_cap // buy) if per_slot_cap > 0 else limit
        units = int(min(limit, remaining // buy, slot_cap_units))
        if units < 1:
            # too pricey for the per-slot risk cap (one unit > max_alloc_frac of bankroll) -> skip, keep capital
            skipped += 1
            continue
        cap_used = units * buy
        vol_day = float(r.vol_daily_7d or 0)
        hourly_vol = vol_day / 24.0
        fill_h = units / hourly_vol if hourly_vol > 0 else 240.0
        cycle_h = min(240.0, max(1.0, 2.0 * fill_h))   # informational: ~one buy+sell round-trip
        # Honest daily throughput. The buy limit only resets every 4h, so you can re-buy a slot at
        # most 6x/day (units * 6) no matter how fast one batch fills -- this is what keeps a thin
        # megarare from pretending to round-trip 24x/day. Then bound by liquidity: you realistically
        # capture only a slice (~25%) of an item's daily traded volume.
        daily_units = min(units * 6.0, 0.25 * vol_day)
        slip = float(r.slip_margin or 0)
        recs.append({
            "item_id": int(r.item_id), "name": r.name,
            "buy": round(buy), "sell_target": round(float(r.sell_price or 0)),
            "units": units, "capital": round(cap_used), "slip_margin": round(slip),
            "gp_day": round(slip * max(0.0, daily_units)), "cycle_h": round(cycle_h, 1),
        })
        remaining -= cap_used
    out["recommendations"] = recs
    out["total_capital"] = round(sum(x["capital"] for x in recs))
    out["total_gp_day"] = round(sum(x["gp_day"] for x in recs))
    out["utilization"] = round(out["total_capital"] / cap0, 3) if cap0 > 0 else 0.0
    out["skipped_no_capital"] = skipped
    return out


def full_table(th: Thresholds | None = None, con=None) -> list[dict]:
    th = th or Thresholds()
    d = market_signals(th, con)
    if d.empty:
        return []
    buy = d["buy_price"].fillna(0)
    d = d[(buy >= th.min_price) & (buy <= th.max_price)]
    return _records(d, TABLE_COLS)


def snapshot_signals(th: Thresholds | None = None, con=None, per_kind: int = 25) -> list[dict]:
    """Flatten the current top signals (flips / crashes / value / overnight) into
    normalized rows for the signal log, so realized outcomes can later be measured
    against what the engine recommended at the time."""
    th = th or Thresholds()
    own = con is None
    con = con or connect(read_only=True)
    try:
        groups = [
            ("flip", flip_table(th, con, limit=per_kind), "roi", "buy_price", "sell_price", "roi", "net_margin", None),
            ("crash", crash_table(th, con, limit=per_kind), "crash_exp_roi", "sell_price", "crash_target",
             "crash_exp_roi", "crash_exp_margin", None),
            ("value", invest_table(th, con, limit=per_kind), "value_confidence", "sell_price", "value_target",
             "value_exp_roi", "value_exp_margin", "value_horizon"),
            ("overnight", overnight_table(th, con, limit=per_kind), "on_fill_prob", "on_buy", "on_target",
             "on_roi", "on_margin", None),
        ]
    finally:
        if own:
            con.close()
    out: list[dict] = []
    for kind, recs, score_k, entry_k, target_k, roi_k, margin_k, horizon_k in groups:
        for i, r in enumerate(recs):
            out.append({
                "kind": kind, "item_id": r.get("item_id"), "name": r.get("name"), "rank": i + 1,
                "score": r.get(score_k), "entry": r.get(entry_k), "target": r.get(target_k),
                "exp_roi": r.get(roi_k), "exp_margin": r.get(margin_k),
                "horizon": r.get(horizon_k) if horizon_k else kind,
                "mid": r.get("mid"), "established": r.get("established"),
            })
    return out


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

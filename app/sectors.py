"""Sector / ETF tracker.

Groups items into economic "sectors" (Runes, Magic gear, Charges, ...) and builds
a cap-weighted index for each, plus a Whole-market index, so you can watch whole
categories rotate -- where money flows in and out of the market -- over 1d / 1w /
2w / 1mo / 3mo / 1y.

Index construction (mirrors a sector ETF):
  * each item is weighted by gp traded per day (mid x daily volume) -- money-flow /
    "market-cap" weighting -- capped at WEIGHT_CAP of its group so no single
    mega-item dominates;
  * the index level at each timestamp is the weight-average of constituents' price
    normalised to the window start;
  * returns over a horizon are the ratio of index levels (level_now / level_past - 1),
    read from the coarsest history timestep that covers the horizon (1h<=2w, 6h<=3mo,
    24h<=1y).

CLASSIFICATION is wiki-category-driven (app/sectormap.py): each item is mapped to a
sector from the OSRS Wiki's own categories (equipment slot, combat style, skill, drop
source), cached in DATA_DIR/item_sectors.json. The SECTOR_DEFS list below is just the
display + the valid sector set; edit sectormap.sector_of() to change the rules.
"""
from __future__ import annotations

import logging
import re

import numpy as np
import pandas as pd

from .analytics import HORIZONS, series_changes
from .db import connect
from .signals import Thresholds, market_signals
from . import sectormap

log = logging.getLogger("sectors")

WEIGHT_CAP = 0.20              # no single item may exceed 20% of its group's weight
SECTOR_MIN_DAILY_VOL = 500     # 7-day liquidity bar (units/day) for sector membership (no price floor)
SECTOR_MIN_GP_VOL = 25_000_000 # ...OR this much gp traded/day (admits high-value low-unit items: cosmetics, megarares)
MARKET_KEY = "market"
# how much history (days) to pull per timestep when building indices
_DAYS_FOR = {"1h": 15, "6h": 95, "24h": 366}
_TIMESTEPS = ("1h", "6h", "24h")
_TF_TO_TIMESTEP = {"2wk": "1h", "3mo": "6h", "1yr": "24h"}

# --- sector list: display + the valid set. Classification is wiki-category-driven in
# app/sectormap.py; keep these keys in sync with what sectormap.sector_of() emits.
SECTOR_DEFS: list[tuple[str, str, str]] = [
    ("melee_weapons", "Melee Weapons", "Swords, maces, scythes & melee best-in-slot"),
    ("melee_armour", "Melee Armour", "Tank & strength armour, melee sets"),
    ("ranged_gear", "Ranged Gear", "Bows, crossbows, thrown & ranged armour"),
    ("magic_gear", "Magic Gear", "Staves, wands, tridents, robes & wards"),
    ("jewellery", "Jewellery", "Amulets, rings, necklaces, bracelets (incl. charged)"),
    ("runes", "Runes & Teleports", "Spell reagents & teleport tablets"),
    ("potions", "Potions", "Finished combat & skilling potions"),
    ("herbs", "Herbs & Secondaries", "Herblore inputs — herbs & secondaries"),
    ("ammo", "Ammunition", "Arrows, bolts, darts, cannonballs, chinchompas"),
    ("fletching_mats", "Fletching Materials", "Tips, shafts, unstrung bows, bowstring, feathers"),
    ("logs", "Logs", "Woodcutting & firemaking supply"),
    ("ores_bars", "Ores & Bars", "Mining & smithing feedstock"),
    ("gems", "Gems", "Cut & uncut gems"),
    ("seeds", "Seeds & Farming", "Seeds, saplings, produce"),
    ("food_fishing", "Fish & Food", "Fishing catches, raw & cooked food"),
    ("bones_prayer", "Bones & Prayer", "Bones, ashes & prayer materials"),
    ("construction", "Construction", "POH building materials"),
    ("charges", "Charges & Scales", "Powered-gear consumable charges"),
    ("misc_skilling", "Misc Skilling", "Other crafting / smithing / skilling inputs"),
    ("treasure", "Treasure & Cosmetics", "Clue rewards, 3rd age, holiday & collectibles"),
    ("boss_components", "Boss Drops & Components", "Non-gear boss/raid uniques (crafted into BiS)"),
]
SECTOR_META = {k: {"key": k, "label": l, "blurb": b} for k, l, b in SECTOR_DEFS}
SECTOR_META[MARKET_KEY] = {"key": MARKET_KEY, "label": "Whole Market",
                           "blurb": "All liquid items — the market benchmark"}
_VALID = {k for k, _, _ in SECTOR_DEFS}

# Slim name-only fallback for items not yet in the wiki-category map (brand-new items
# before the next collector refresh; the map covers ~99% of gp-volume).
_FALLBACK = [
    ("logs", re.compile(r"\blogs?$")),
    ("ores_bars", re.compile(r"\b(ore|bar)s?$|\bcoal$")),
    ("seeds", re.compile(r"(seed|sapling|spore)s?$")),
    ("ammo", re.compile(r"\b(arrows?|bolts?|darts?|javelins?)\b")),
    ("potions", re.compile(r"\(\d\)$|\bpotion\b|\bbrew\b")),
    ("jewellery", re.compile(r"\b(amulet|necklace|bracelet)\b|\bring\b")),
    ("bones_prayer", re.compile(r"\b(bones|ashes)$")),
    ("runes", re.compile(r"\brune\b|\bteleport\b|\(tablet\)$")),
]


def classify_one(name: str) -> str | None:
    """Item -> sector via the wiki-category map (app/sectormap.py), with a slim
    name-only fallback for items not yet in the map (brand-new items)."""
    if not name:
        return None
    s = sectormap.load_map().get(name.lower())
    if s in _VALID:
        return s
    n = name.lower()
    for key, pat in _FALLBACK:
        if pat.search(n):
            return key
    return None


def classify_series(names: pd.Series) -> pd.Series:
    return names.map(classify_one)


# --- membership & weighting -------------------------------------------------
def _liquid(th: Thresholds, con) -> pd.DataFrame:
    """All liquid items (7d daily volume bar, no price floor) with current deviation
    vs their 7d baseline, a money-flow weight, and a sector label."""
    d = market_signals(th, con)
    if d.empty:
        return d
    vol = d["vol_daily_7d"].fillna(0.0)
    gpv = d["mid"].fillna(0.0) * vol
    d = d[d["established"].notna() & ((vol >= SECTOR_MIN_DAILY_VOL) | (gpv >= SECTOR_MIN_GP_VOL))].copy()
    d["dev"] = d["drawdown"]                                   # (mid - established) / established
    d["gp_vol"] = (d["mid"] * d["vol_daily_7d"].fillna(0.0)).clip(lower=0.0)
    d["sector"] = classify_series(d["name"])
    return d


def _grouped(liq: pd.DataFrame) -> pd.DataFrame:
    """Long form: one row per (item, group) where group is the item's sector AND the
    whole market. Weight = gp_vol capped at WEIGHT_CAP of the group total."""
    if liq.empty:
        return liq
    sec = liq[liq["sector"].notna()].copy()
    sec["grp"] = sec["sector"]
    mkt = liq.copy()
    mkt["grp"] = MARKET_KEY
    g = pd.concat([sec, mkt], ignore_index=True)
    tot = g.groupby("grp")["gp_vol"].transform("sum")
    g["weight"] = np.minimum(g["gp_vol"], WEIGHT_CAP * tot)
    g.loc[g["weight"] <= 0, "weight"] = 1.0
    return g


def _raw_index(gmembers: pd.DataFrame, con, timestep: str) -> pd.DataFrame:
    """Per-group, per-timestamp cap-weighted index LEVEL (base-normalised to the
    window start). Columns: [grp, ts, raw].

    The normalise -> weight -> aggregate happens in DuckDB so only the small
    (group x timestamp) result is materialised, not the full history."""
    empty = pd.DataFrame(columns=["grp", "ts", "raw"])
    if gmembers.empty:
        return empty
    con.register("sector_members", gmembers[["item_id", "grp", "weight"]].copy())
    try:
        g = con.execute(
            f"""
            WITH h AS (
                SELECT item_id, ts, (avg_high + avg_low) / 2.0 AS mid
                FROM history
                WHERE timestep = '{timestep}' AND avg_high IS NOT NULL AND avg_low IS NOT NULL
            ),
            mx AS (SELECT max(ts) AS t FROM h),
            win AS (SELECT h.item_id, h.ts, h.mid FROM h CROSS JOIN mx
                    WHERE h.ts >= mx.t - INTERVAL '{_DAYS_FOR.get(timestep, 15)}' DAY),
            base AS (SELECT item_id, arg_min(mid, ts) AS base_mid FROM win GROUP BY item_id),
            -- Clip each item's normalised move to [-80%, +400%]. Cheap, low-volume items
            -- occasionally print one absurd tick (a fat-finger / manipulated trade at 1000x+),
            -- which would otherwise blow up the cap-weighted index for a single bar. Genuine
            -- moves sit well inside this band, so the clip only neutralises bad ticks.
            norm AS (SELECT w.item_id, w.ts,
                            LEAST(GREATEST(w.mid / b.base_mid, 0.2), 5.0) AS norm
                     FROM win w JOIN base b ON w.item_id = b.item_id WHERE b.base_mid > 0)
            SELECT m.grp, n.ts, sum(n.norm * m.weight) / sum(m.weight) AS raw
            FROM norm n JOIN sector_members m ON n.item_id = m.item_id
            GROUP BY m.grp, n.ts
            """
        ).df()
    finally:
        con.unregister("sector_members")
    return g if not g.empty else empty


def _grp_series(idx: pd.DataFrame, grp: str) -> pd.Series:
    """ts-indexed raw-level series for one group from a _raw_index frame."""
    if idx.empty:
        return pd.Series(dtype="float64")
    s = idx[idx["grp"] == grp]
    return s.set_index("ts")["raw"].sort_index().dropna() if not s.empty else pd.Series(dtype="float64")


def _anchored(raw: pd.Series) -> pd.Series:
    """Raw level -> % move vs the window start (anchored at 0)."""
    if raw.empty:
        return raw
    first = raw.iloc[0]
    return (raw / first - 1.0) * 100.0 if first and first > 0 else pd.Series(dtype="float64")


def _fin(x) -> float | None:
    if x is None or not np.isfinite(x):
        return None
    return float(x)


def _spark(raw: pd.Series, points: int = 32) -> list[float]:
    s = _anchored(raw)
    if s.empty:
        return []
    if len(s) <= points:
        return [round(float(v), 3) for v in s.to_numpy()]
    idx = np.linspace(0, len(s) - 1, points).round().astype(int)
    return [round(float(s.iloc[i]), 3) for i in idx]


def _changes(by_ts_raw: dict[str, pd.Series]) -> dict:
    """Multi-horizon ratio returns for one group from its raw series per timestep."""
    ch = series_changes(by_ts_raw)
    return {k: _fin(v) for k, v in ch.items()}


# --- public API -------------------------------------------------------------
def sector_table(th: Thresholds | None = None, con=None) -> dict:
    """One card per sector (+ Whole Market) with multi-horizon returns and a sparkline."""
    th = th or Thresholds()
    own = con is None
    con = con or connect(read_only=True)
    try:
        liq = _liquid(th, con)
        if liq.empty:
            return {"sectors": [], "coverage": {"classified": 0, "liquid": 0}}
        gmembers = _grouped(liq)
        idx_by_ts = {ts: _raw_index(gmembers, con, ts) for ts in _TIMESTEPS}
    finally:
        if own:
            con.close()

    cards = []
    for grp in gmembers["grp"].unique():
        members = gmembers[gmembers["grp"] == grp]
        w = members["weight"]
        m = members["dev"].notna() & (w > 0)
        dev = _fin((members["dev"][m] * w[m]).sum() / w[m].sum()) if m.any() else None
        movers = members.sort_values("dev", ascending=False)
        top_up = [{"item_id": int(r.item_id), "name": r.name, "dev": _fin(r.dev)}
                  for r in movers.head(3).itertuples() if pd.notna(r.dev)]
        top_down = [{"item_id": int(r.item_id), "name": r.name, "dev": _fin(r.dev)}
                    for r in movers.tail(3)[::-1].itertuples() if pd.notna(r.dev)]
        by_ts = {ts: _grp_series(idx_by_ts[ts], grp) for ts in _TIMESTEPS}
        cards.append({
            **SECTOR_META.get(grp, {"key": grp, "label": grp, "blurb": ""}),
            "n_items": int(len(members)),
            "gp_vol": float(members["gp_vol"].sum()),
            "dev": dev,
            "changes": _changes(by_ts),
            "spark": _spark(by_ts["1h"]),
            "top_up": top_up,
            "top_down": top_down,
        })

    market = [c for c in cards if c["key"] == MARKET_KEY]
    others = [c for c in cards if c["key"] != MARKET_KEY]
    others.sort(key=lambda c: (c["changes"].get("1d") is None, -(c["changes"].get("1d") or 0)))
    n_class = int(liq["sector"].notna().sum())
    return {"sectors": market + others, "coverage": {"classified": n_class, "liquid": int(len(liq))}}


def sector_detail(key: str, th: Thresholds | None = None, con=None, timeframe: str = "2wk") -> dict | None:
    """One group's index series (at the chosen timeframe) + ranked constituents."""
    th = th or Thresholds()
    if key not in SECTOR_META:
        return None
    chart_ts = _TF_TO_TIMESTEP.get(timeframe, "1h")
    own = con is None
    con = con or connect(read_only=True)
    try:
        liq = _liquid(th, con)
        gmembers = _grouped(liq) if not liq.empty else liq
        members = gmembers[gmembers["grp"] == key].copy() if not gmembers.empty else gmembers
        if members.empty:
            return {**SECTOR_META[key], "series": [], "constituents": [], "changes": {}}
        idx_chart = _raw_index(members, con, chart_ts)
        idx_by_ts = {ts: (_raw_index(members, con, ts) if ts != chart_ts else idx_chart)
                     for ts in _TIMESTEPS}
    finally:
        if own:
            con.close()

    raw_chart = _grp_series(idx_chart, key)
    anch = _anchored(raw_chart)
    series = [{"time": int(ts.value // 1_000_000_000), "index": round(float(v), 3)}
              for ts, v in anch.items() if pd.notna(v)]
    by_ts = {ts: _grp_series(idx_by_ts[ts], key) for ts in _TIMESTEPS}

    wsum = members["weight"].sum()
    members = members.sort_values("gp_vol", ascending=False)
    constituents = [{
        "item_id": int(r.item_id), "name": r.name,
        "mid": _fin(r.mid), "established": _fin(r.established), "dev": _fin(r.dev),
        "gp_vol": float(r.gp_vol),
        "weight_pct": float(r.weight / wsum * 100.0) if wsum > 0 else None,
    } for r in members.itertuples()]

    return {**SECTOR_META[key], "timeframe": timeframe, "series": series,
            "changes": _changes(by_ts), "constituents": constituents}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    t = sector_table()
    secs = t["sectors"]
    cov = t["coverage"]
    print(f"\n=== SECTORS ({cov.get('classified', 0)} classified / {cov.get('liquid', 0)} liquid) ===")
    if not secs:
        print("No data. Seed demo data (python -m app.mockdata) or run the collector.")
        return
    print(f"{'sector':<22}{'items':>6}{'gp/day':>13}  {'1d':>7}{'2w':>7}{'3mo':>7}{'1y':>7}")
    for s in secs:
        ch = s["changes"]
        def f(x):
            return "   -  " if x is None else f"{x * 100:+6.1f}"
        print(f"{s['label'][:21]:<22}{s['n_items']:>6}{s['gp_vol']:>13,.0f}  "
              f"{f(ch.get('1d'))}{f(ch.get('2w'))}{f(ch.get('3mo'))}{f(ch.get('1y'))}")
    print()


if __name__ == "__main__":
    main()

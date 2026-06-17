"""Sector / ETF tracker.

Groups items into economic "sectors" (Runes, Herbs & Potions, Logs, ...) and
builds a cap-weighted index for each one, so you can watch whole categories
rotate hour-by-hour -- where money is flowing in and out of the market.

Index construction (mirrors a real sector ETF):
  * each item is weighted by gp traded per day (mid x daily volume), i.e.
    money-flow / "market-cap" weighting, capped at WEIGHT_CAP of its sector so
    no single mega-item dominates;
  * the index value at each hour is the weight-average of its constituents'
    price normalised to the window start, so the line reads as "% move";
  * sector returns over 1h / 6h / 24h / 7d come straight off that index.

TAXONOMY IS EDITABLE: the SECTORS list below is plain, ordered rules
(first match wins). Tune the keyword patterns / add curated overrides to fit
how you think about the game -- nothing else needs to change.
"""
from __future__ import annotations

import logging
import re

import numpy as np
import pandas as pd

from .analytics import HISTORY_TIMESTEP
from .db import connect
from .signals import Thresholds, market_signals

log = logging.getLogger("sectors")

WEIGHT_CAP = 0.20          # no single item may exceed 20% of its sector's weight
INDEX_DAYS = 14            # how much 1h history to build the index over (~the full 1h window)

# --- taxonomy ---------------------------------------------------------------
# Each sector: (key, label, blurb, [include regex...], [exclude regex...]).
# Matched against the lower-cased item name; FIRST sector that matches wins, so
# put specific sectors before broad ones (ammo/ores before melee gear, etc).
_TIERS = r"(bronze|iron|steel|black|white|mithril|adamant|adamantite|rune|runite|dragon)"
_GEAR = (
    r"(dagger|sword|longsword|scimitar|2h sword|mace|warhammer|battleaxe|axe|hatchet|pickaxe|"
    r"halberd|spear|hasta|claws|full helm|med helm|helm|platebody|platelegs|plateskirt|"
    r"chainbody|sq shield|kiteshield|shield|boots|gauntlets|defender|crossbow|knife)"
)

SECTORS: list[tuple[str, str, str, list[str], list[str]]] = [
    ("runes", "Runes", "Spellcasting reagents — magic & alch demand",
     [r"\b(air|water|earth|fire|mind|body|cosmic|chaos|nature|law|death|blood|soul|astral|wrath|"
      r"mist|dust|mud|smoke|steam|lava|sunfire) rune"], []),
    ("ammo", "Ammunition", "Arrows, bolts, darts, javelins, cannonballs",
     [r"(arrow|bolt|bolts|dart|javelin)s?$", r"\bcannonball", r"\bbolt tips?$", r"\bbolt rack"],
     [r"\bunf$"]),
    ("ores_bars", "Ores & Bars", "Mining & smithing feedstock",
     [r"(ore|bar)s?$", r"^coal$", r"\bcoal$"], []),
    ("logs", "Logs", "Woodcutting & firemaking supply",
     [r"\blogs?$"], []),
    ("seeds", "Seeds & Farming", "Farming seeds, saplings, produce",
     [r"(seed|seedling|sapling|spore)s?$", r"\bsapling$"], []),
    ("herbs_potions", "Herbs & Potions", "Herblore — herbs, doses, secondaries",
     [r"\(\d\)$", r"\bpotion", r"\bbrew\(?", r"\b(grimy|clean)\b",
      r"\b(guam|marrentill|tarromin|harralander|ranarr|toadflax|irit|avantoe|kwuarm|snapdragon|"
      r"cadantine|lantadyme|dwarf weed|torstol|huasca)\b"], []),
    ("food_fishing", "Food & Fishing", "Raw & cooked food, fishing catches",
     [r"^raw ", r"^cooked ",
      r"\b(shark|anglerfish|manta ray|sea turtle|monkfish|lobster|tuna|swordfish|salmon|trout|"
      r"karambwan|bass|cod|herring|sardine|anchovies|mackerel|pike|shrimps|tuna potato)\b"], []),
    ("dhide_ranged", "Dragonhide & Ranged", "Hides, leather, ranged armour",
     [r"dragonhide", r"d'hide", r"\bdragon leather\b", r"\bleather\b", r"\bchaps\b",
      r"\bvambraces\b", r"\bcoif\b"], []),
    ("bones_prayer", "Bones & Prayer", "Bones & ashes — prayer training",
     [r"\b(bones|ashes)$"], []),
    ("treasure", "Treasure & Cosmetics", "Clue rewards & collectibles",
     [r"3rd age", r"\bgilded\b", r"ornament kit", r"partyhat", r"h'ween", r"halloween mask",
      r"santa hat", r"\branger boots\b", r"\bgnomish\b", r"\bspirit shield\b"], []),
    ("high_tier_pvm", "High-tier PvM gear", "Megarares & raids gear",
     [r"twisted bow", r"scythe of vitur", r"tumeken's shadow", r"sanguinesti staff",
      r"ghrazi rapier", r"justiciar", r"avernic defender", r"bow of faerdhinen", r"\bbowfa\b",
      r"primordial", r"pegasian", r"eternal", r"ancestral", r"masori", r"\btorva\b",
      r"\bvirtus\b", r"zaryte", r"dragon hunter", r"\bvoidwaker\b", r"\bvenator\b", r"\bosmumten"],
     []),
    ("melee_gear", "Melee gear", "Tiered metal weapons & armour",
     [rf"^{_TIERS} {_GEAR}$", rf"^{_TIERS}.* {_GEAR}$"], []),
    ("skilling", "Skilling supplies", "Planks, nails, feathers, bait & misc",
     [r"\bplanks?$", r"\bnails$", r"\bfeather", r"\bbait$", r"\bcompost$", r"\bbucket",
      r"\bmolten glass$", r"\bsoft clay$", r"\bglassblowing"], []),
]

# Curated name -> sector overrides (exact, case-insensitive) for items the rules miss.
OVERRIDES: dict[str, str] = {
    "coal": "ores_bars",
    "cannonball": "ammo",
    "pure essence": "runes",
    "rune essence": "runes",
    "blood shard": "runes",
    "wine of zamorak": "herbs_potions",
    "snape grass": "herbs_potions",
    "limpwurt root": "herbs_potions",
    "white berries": "herbs_potions",
}

_COMPILED = [
    (key, label, blurb,
     [re.compile(p) for p in inc],
     [re.compile(p) for p in exc])
    for key, label, blurb, inc, exc in SECTORS
]
SECTOR_META = {key: {"key": key, "label": label, "blurb": blurb}
               for key, label, blurb, _, _ in SECTORS}


def classify_one(name: str) -> str | None:
    if not name:
        return None
    n = name.lower()
    if n in OVERRIDES:
        return OVERRIDES[n]
    for key, _label, _blurb, inc, exc in _COMPILED:
        if any(p.search(n) for p in inc) and not any(p.search(n) for p in exc):
            return key
    return None


def classify_series(names: pd.Series) -> pd.Series:
    return names.map(classify_one)


# --- current snapshot -------------------------------------------------------
def _members(th: Thresholds, con) -> pd.DataFrame:
    """Liquid, classified items with current deviation vs their 7d baseline and a
    capped money-flow weight, ready to aggregate into sector indices."""
    d = market_signals(th, con)
    if d.empty:
        return d
    d = d[d["tradeable"] & (d["mid"].fillna(0) >= th.min_price) & d["established"].notna()].copy()
    d["sector"] = classify_series(d["name"])
    d = d[d["sector"].notna()].copy()
    if d.empty:
        return d
    d["dev"] = d["drawdown"]                                   # (mid - established) / established
    d["gp_vol"] = (d["mid"] * d["vol_daily_7d"].fillna(0.0)).clip(lower=0.0)

    # cap each item's weight at WEIGHT_CAP of its sector total (one pass)
    tot = d.groupby("sector")["gp_vol"].transform("sum")
    d["weight"] = np.minimum(d["gp_vol"], WEIGHT_CAP * tot).fillna(0.0)
    d.loc[d["weight"] <= 0, "weight"] = 1.0                    # floor so zero-volume items still count a little
    return d


def _index_history(members: pd.DataFrame, con, timestep: str = HISTORY_TIMESTEP,
                   days: int = INDEX_DAYS) -> pd.DataFrame:
    """Per-sector, per-hour index built from 1h history, normalised so each
    sector's series starts at 0%. Returns columns [sector, ts, index]."""
    if members.empty:
        return pd.DataFrame(columns=["sector", "ts", "index"])
    reg = members[["item_id", "sector", "weight"]].copy()
    con.register("sector_members", reg)
    try:
        hist = con.execute(
            f"""
            WITH h AS (
                SELECT item_id, ts, (avg_high + avg_low) / 2.0 AS mid
                FROM history
                WHERE timestep = '{timestep}'
                  AND avg_high IS NOT NULL AND avg_low IS NOT NULL
            ), mx AS (SELECT max(ts) AS t FROM h)
            SELECT h.item_id, h.ts, h.mid, m.sector, m.weight
            FROM h CROSS JOIN mx
            JOIN sector_members m ON h.item_id = m.item_id
            WHERE h.ts >= mx.t - INTERVAL '{int(days)}' DAY
            """
        ).df()
    finally:
        con.unregister("sector_members")
    if hist.empty:
        return pd.DataFrame(columns=["sector", "ts", "index"])

    hist = hist.sort_values(["item_id", "ts"])
    base = hist.groupby("item_id")["mid"].transform("first")
    hist["norm"] = np.where(base > 0, hist["mid"] / base, np.nan)
    hist = hist.dropna(subset=["norm"])
    hist["wn"] = hist["norm"] * hist["weight"]

    g = hist.groupby(["sector", "ts"]).agg(wn=("wn", "sum"), w=("weight", "sum")).reset_index()
    g["raw"] = np.where(g["w"] > 0, g["wn"] / g["w"], np.nan)
    # anchor each sector's series at 0% (divide by its own first value)
    first = g.sort_values("ts").groupby("sector")["raw"].transform("first")
    g["index"] = (g["raw"] / first - 1.0) * 100.0
    return g[["sector", "ts", "index"]].sort_values(["sector", "ts"])


def _fin(x) -> float | None:
    """None for missing / non-finite values (keeps JSON valid -- no NaN tokens)."""
    if x is None or not np.isfinite(x):
        return None
    return float(x)


def _returns(series: pd.Series) -> dict:
    """1h/6h/24h/7d % change off a per-hour index Series indexed by ts (sorted)."""
    keys = ("ret_1h", "ret_6h", "ret_24h", "ret_7d")
    if series.empty:
        return dict.fromkeys(keys)
    s = series.sort_index()
    now_ts, now = s.index[-1], s.iloc[-1]
    if not pd.notna(now):
        return dict.fromkeys(keys)
    out = {}
    for label, hours in zip(keys, (1, 6, 24, 168)):
        past = s.asof(now_ts - pd.Timedelta(hours=hours))
        # index is already a % level anchored at 0; the change is a difference in pp
        out[label] = _fin(now - past) if pd.notna(past) else None
    return out


def _spark(series: pd.Series, points: int = 32) -> list[float]:
    if series.empty:
        return []
    s = series.sort_index().dropna()
    if s.empty:
        return []
    if len(s) <= points:
        return [round(float(v), 3) for v in s.to_numpy()]
    idx = np.linspace(0, len(s) - 1, points).round().astype(int)
    return [round(float(s.iloc[i]), 3) for i in idx]


def sector_table(th: Thresholds | None = None, con=None) -> dict:
    """Everything the Sectors grid needs: one card per sector + a coverage note."""
    th = th or Thresholds()
    own = con is None
    con = con or connect(read_only=True)
    try:
        members = _members(th, con)
        if members.empty:
            return {"sectors": [], "coverage": {"classified": 0, "unclassified": 0}}
        idx = _index_history(members, con)
    finally:
        if own:
            con.close()

    cards = []
    for key, grp in members.groupby("sector"):
        w = grp["weight"]
        m = grp["dev"].notna() & (w > 0)
        dev = _fin((grp["dev"][m] * w[m]).sum() / w[m].sum()) if m.any() else None
        movers = grp.sort_values("dev", ascending=False)
        top_up = [{"item_id": int(r.item_id), "name": r.name, "dev": float(r.dev)}
                  for r in movers.head(3).itertuples() if pd.notna(r.dev)]
        top_down = [{"item_id": int(r.item_id), "name": r.name, "dev": float(r.dev)}
                    for r in movers.tail(3)[::-1].itertuples() if pd.notna(r.dev)]
        sec_idx = idx[idx["sector"] == key].set_index("ts")["index"] if not idx.empty else pd.Series(dtype=float)
        meta = SECTOR_META.get(key, {"key": key, "label": key, "blurb": ""})
        cards.append({
            **meta,
            "n_items": int(len(grp)),
            "gp_vol": float(grp["gp_vol"].sum()),
            "dev": dev,
            **_returns(sec_idx),
            "spark": _spark(sec_idx),
            "top_up": top_up,
            "top_down": top_down,
        })

    # sort by 24h move (most positive first), Nones last
    cards.sort(key=lambda c: (c["ret_24h"] is None, -(c["ret_24h"] or 0)))
    return {"sectors": cards, "coverage": {"classified": int(len(members))}}


def sector_detail(key: str, th: Thresholds | None = None, con=None) -> dict | None:
    """The index time series + ranked constituents for one sector's panel."""
    th = th or Thresholds()
    if key not in SECTOR_META:
        return None
    own = con is None
    con = con or connect(read_only=True)
    try:
        members = _members(th, con)
        members = members[members["sector"] == key].copy()
        if members.empty:
            return {**SECTOR_META[key], "series": [], "constituents": []}
        idx = _index_history(members, con)
    finally:
        if own:
            con.close()

    sec_idx = idx[idx["sector"] == key] if not idx.empty else pd.DataFrame(columns=["ts", "index"])
    series = [{"time": int(ts.value // 1_000_000_000), "index": round(float(v), 3)}
              for ts, v in zip(sec_idx["ts"], sec_idx["index"]) if pd.notna(v)]

    wsum = members["weight"].sum()
    members = members.sort_values("gp_vol", ascending=False)
    constituents = [{
        "item_id": int(r.item_id),
        "name": r.name,
        "mid": float(r.mid) if pd.notna(r.mid) else None,
        "established": float(r.established) if pd.notna(r.established) else None,
        "dev": float(r.dev) if pd.notna(r.dev) else None,
        "gp_vol": float(r.gp_vol),
        "weight_pct": float(r.weight / wsum * 100.0) if wsum > 0 else None,
    } for r in members.itertuples()]

    return {**SECTOR_META[key], "series": series, "constituents": constituents,
            **_returns(sec_idx.set_index("ts")["index"] if not sec_idx.empty else pd.Series(dtype=float))}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    t = sector_table()
    secs = t["sectors"]
    print(f"\n=== SECTORS ({t['coverage'].get('classified', 0)} items classified) ===")
    if not secs:
        print("No data. Seed demo data (python -m app.mockdata) or run the collector.")
        return
    print(f"{'sector':<22}{'items':>6}{'gp vol/day':>14}{'dev':>8}{'1h':>7}{'6h':>7}{'24h':>7}{'7d':>7}")
    for s in secs:
        def f(x, suf="%"):
            return "-" if x is None else f"{x:+.1f}{suf}"
        print(f"{s['label'][:21]:<22}{s['n_items']:>6}{s['gp_vol']:>14,.0f}"
              f"{f(s['dev'] and s['dev']*100):>8}{f(s['ret_1h']):>7}{f(s['ret_6h']):>7}"
              f"{f(s['ret_24h']):>7}{f(s['ret_7d']):>7}")
    print()


if __name__ == "__main__":
    main()

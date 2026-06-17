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

TAXONOMY IS EDITABLE: the SECTORS list below is plain, ordered rules (first match
wins -- specific sectors before broad ones). Tune the keyword patterns / curated
OVERRIDES to fit how you think about the game; nothing else needs to change.
"""
from __future__ import annotations

import logging
import re

import numpy as np
import pandas as pd

from .analytics import HORIZONS, series_changes
from .db import connect
from .signals import Thresholds, market_signals

log = logging.getLogger("sectors")

WEIGHT_CAP = 0.20              # no single item may exceed 20% of its group's weight
SECTOR_MIN_DAILY_VOL = 500     # 7-day liquidity bar for sector membership (no price floor)
MARKET_KEY = "market"
# how much history (days) to pull per timestep when building indices
_DAYS_FOR = {"1h": 15, "6h": 95, "24h": 366}
_TIMESTEPS = ("1h", "6h", "24h")
_TF_TO_TIMESTEP = {"2wk": "1h", "3mo": "6h", "1yr": "24h"}

# --- taxonomy ---------------------------------------------------------------
_TIERS = r"(bronze|iron|steel|black|white|mithril|adamant|adamantite|rune|runite|dragon)"
_GEAR = (
    r"(dagger|sword|longsword|scimitar|2h sword|mace|warhammer|battleaxe|axe|hatchet|pickaxe|"
    r"halberd|spear|hasta|claws|full helm|med helm|helm|platebody|platelegs|plateskirt|"
    r"chainbody|sq shield|kiteshield|shield|boots|gauntlets|defender)"
)
_FISH = (
    r"(shark|anglerfish|manta ray|sea turtle|monkfish|lobster|tuna|swordfish|salmon|trout|"
    r"karambwan|bass|cod|herring|sardine|anchovies|mackerel|pike|shrimps|sea turtle)"
)

# (key, label, blurb, [include regex], [exclude regex])  -- matched on lower-cased name
SECTORS: list[tuple[str, str, str, list[str], list[str]]] = [
    ("charges", "Charges & Scales", "Consumable charges for powered weapons & gear",
     [r"zulrah's scales", r"revenant ether", r"\bdemon tear\b", r"sunfire splinters",
      r"rubium splinters", r"ancient essence", r"aether catalyst", r"bottled (storm|dread|mind)",
      r"bracelet of ethereum", r"\bblighted\b", r"\bnumulite\b", r"eye of ayak"], []),
    ("fletching_mats", "Fletching Materials", "Tips, shafts, unstrung bows, bow string, feathers",
     [r"\b(arrow|dart|javelin|bolt) ?tips?\b", r"\b(arrow|javelin) ?shafts?\b", r"\bbow string\b",
      r"\bunstrung\b", r"\(u\)$", r"\(unf\)$", r"headless arrow", r"\bfeather"], []),
    ("ammo", "Ammunition", "Finished arrows, bolts, darts, javelins, chinchompas",
     [r"\barrows?\b", r"\bbolts?\b", r"\bdarts?\b", r"\bjavelins?\b", r"\bknives\b",
      r"\bchinchompas?\b", r"\bcannonball", r"throwing axe"],
     [r"tips?\b", r"shafts?\b", r"\(unf\)$", r"bolt of cloth"]),
    ("runes", "Runes & Teleports", "Spellcasting reagents & teleport tablets",
     [r"\b(air|water|earth|fire|mind|body|cosmic|chaos|nature|law|death|blood|soul|astral|wrath|"
      r"mist|dust|mud|smoke|steam|lava|sunfire|aether|elemental|catalytic) rune",
      r"\(tablet\)$", r"\bteleport\b"], []),
    ("herbs_potions", "Herbs & Potions", "Herblore — herbs, doses, secondaries",
     [r"\(\d\)$", r"\bpotion", r"\bbrew", r"\b(grimy|clean)\b",
      r"\b(guam|marrentill|tarromin|harralander|ranarr|toadflax|irit|avantoe|kwuarm|snapdragon|"
      r"cadantine|lantadyme|dwarf weed|torstol|huasca)\b"], []),
    ("seeds", "Seeds & Farming", "Farming seeds, saplings, produce",
     [r"(seed|seedling|sapling|spore)s?$", r"\bsapling$"], []),
    ("logs", "Logs", "Woodcutting & firemaking supply",
     [r"\blogs?$"], []),
    ("ores_bars", "Ores & Bars", "Mining & smithing feedstock",
     [r"(ore|bar)s?$", r"\bcoal$"], []),
    ("gems", "Gems", "Cut & uncut gems",
     [r"^(uncut )?(sapphire|emerald|ruby|diamond|dragonstone|onyx|zenyte|opal|jade|red topaz)( \(.*\))?$"],
     []),
    ("jewellery", "Jewellery", "Amulets, rings, necklaces, bracelets",
     [r"\bamulet\b", r"\bnecklace\b", r"\bring\b", r"\bbracelet\b", r"\btiara\b", r"lightbearer"], []),
    ("food_fishing", "Food & Fishing", "Raw & cooked food, fishing catches",
     [r"^raw ", r"^cooked ", rf"\b{_FISH}\b",
      r"\b(pie|stew|pizza|cake|bread|kebab|curry|sandwich)\b", r"jug of (water|wine)",
      r"\bgrapes\b", r"minced meat", r"\bflour\b", r"tuna potato", r"\bwine of zamorak\b"], []),
    ("bones_prayer", "Bones & Prayer", "Bones & ashes — prayer training",
     [r"\b(bones|ashes)$", r"\bensouled\b"], []),
    ("construction", "Construction", "POH building materials",
     [r"\bplanks?\b", r"magic stone", r"marble block", r"limestone brick", r"bolt of cloth",
      r"\bgold leaf\b", r"\bclockwork\b"], []),
    ("barrows", "Barrows Gear", "Dharok / Ahrim / Karil / Guthan / Torag / Verac",
     [r"\b(dharok's|ahrim's|karil's|guthan's|torag's|verac's)\b", r"\bbarrows\b"], []),
    ("high_tier_pvm", "High-tier PvM Gear", "Megarares & raids/boss best-in-slot",
     [r"twisted bow", r"scythe of vitur", r"tumeken's shadow", r"sanguinesti", r"ghrazi rapier",
      r"\bjusticiar\b", r"avernic", r"bow of faerdhinen", r"\bbowfa\b", r"primordial", r"pegasian",
      r"eternal (boots|crystal)", r"ancestral", r"\bmasori\b", r"\btorva\b", r"\bvirtus\b", r"zaryte",
      r"dragon hunter", r"voidwaker", r"venator", r"osmumten", r"\belysian\b", r"\barcane\b",
      r"\bspectral\b", r"oathplate", r"confliction gauntlets", r"tormented synapse",
      r"soulreaper axe", r"noxious halberd", r"inquisitor's", r"\bdinh's\b"], []),
    ("magic_gear", "Magic Gear", "Staves, robes, tridents, wards, tomes",
     [r"\bbattlestaff\b", r"\bstaff\b", r"\bstaves\b", r"\bwand\b", r"\btome\b", r"\bward\b",
      r"\bmystic\b", r"\binfinity\b", r"\btrident\b", r"mage's book", r"\borb\b", r"\bkodai\b",
      r"nightmare staff", r"\b(eldritch|harmonised|volatile)\b", r"twinflame staff", r"\boccult\b",
      r"saturated heart", r"imbued heart"], []),
    ("ranged_gear", "Ranged Gear", "Bows, crossbows, dragonhide, ranged armour",
     [r"dragonhide", r"d'hide", r"\bleather\b", r"\bchaps\b", r"\bvambraces\b", r"\bcoif\b",
      r"bow\b", r"blowpipe", r"crossbow", r"\bc'bow\b", r"\bbuckler\b", r"webweaver",
      r"\bcrystal (bow|body|legs|helm|armour|shield|halberd)", r"karil's",
      r"armadyl (crossbow|c'bow|helmet|chestplate|chainskirt)"], []),
    ("melee_gear", "Melee Gear", "Tiered & boss melee weapons + armour",
     [rf"^{_TIERS} {_GEAR}$", rf"^{_TIERS} .* {_GEAR}$", r"\bgodsword\b",
      r"\bbandos (chestplate|tassets|boots)\b", r"saradomin sword", r"zamorakian",
      r"abyssal (whip|dagger|bludgeon|tentacle)", r"\bgranite (maul|hammer|body|shield|longsword)\b",
      r"fighter torso", r"\bfire cape\b", r"infernal cape", r"\bdefender\b", r"dual macuahuitl",
      r"\bobsidian\b", r"\bbulwark\b",
      r"\bdragon (scimitar|dagger|mace|longsword|battleaxe|halberd|claws|sword|warhammer|hasta|spear)\b"],
     []),
    ("treasure", "Treasure & Cosmetics", "Clue rewards & collectibles",
     [r"3rd age", r"\bgilded\b", r"ornament kit", r"partyhat", r"h'ween", r"halloween mask",
      r"santa hat", r"\branger boots\b", r"robin hood", r"\brangers'\b",
      r"god (d'hide|coif|bracers|chaps|body|stole|crozier|mitre|cloak)", r"\bdecorative\b",
      r"spirit shield", r"samurai|musketeer|cavalier|highwayman|pith helmet|bucket helm",
      r"\bzealot's\b", r"\benchanted (hat|top|robe|bottom)\b", r"\bsled\b", r"yo-yo"], []),
    ("skilling", "Skilling Supplies", "Misc skilling inputs & products",
     [r"\bnails$", r"\bbait$", r"\bcompost$", r"\bbucket", r"\bmolten glass$", r"\bsoft clay$",
      r"glassblowing", r"\bflax\b", r"\bswamp (tar|paste)$", r"\bsaltpetre$", r"\bthread$",
      r"\bvial", r"\bball of wool\b", r"\bwool\b", r"\blimestone\b", r"\bclay\b",
      r"\bcrushed nest\b", r"\bash(es)?$"], []),
]

# Curated name -> sector overrides (exact, case-insensitive) for items the rules miss.
OVERRIDES: dict[str, str] = {
    "coal": "ores_bars", "pure essence": "runes", "rune essence": "runes",
    "daeyalt essence": "runes", "blood shard": "jewellery",
    "snape grass": "herbs_potions", "limpwurt root": "herbs_potions",
    "white berries": "herbs_potions", "red spiders' eggs": "herbs_potions",
    "wine of zamorak": "herbs_potions", "crushed nest": "herbs_potions",
    "unicorn horn dust": "herbs_potions", "eye of newt": "herbs_potions",
    "old school bond": "treasure", "magic longbow": "ranged_gear",
    "air orb": "magic_gear", "water orb": "magic_gear", "earth orb": "magic_gear",
    "fire orb": "magic_gear", "lizardman fang": "skilling", "minced meat": "food_fishing",
    "black chinchompa": "ammo", "red chinchompa": "ammo", "grey chinchompa": "ammo",
}

_COMPILED = [
    (key, label, blurb, [re.compile(p) for p in inc], [re.compile(p) for p in exc])
    for key, label, blurb, inc, exc in SECTORS
]
SECTOR_META = {key: {"key": key, "label": label, "blurb": blurb}
               for key, label, blurb, _, _ in SECTORS}
SECTOR_META[MARKET_KEY] = {"key": MARKET_KEY, "label": "Whole Market",
                           "blurb": "All liquid items — the market benchmark"}


def classify_one(name: str) -> str | None:
    if not name:
        return None
    n = name.lower()
    if n in OVERRIDES:
        return OVERRIDES[n]
    for key, _l, _b, inc, exc in _COMPILED:
        if any(p.search(n) for p in inc) and not any(p.search(n) for p in exc):
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
    d = d[d["established"].notna() & (d["vol_daily_7d"].fillna(0) >= SECTOR_MIN_DAILY_VOL)].copy()
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
    window start). Columns: [grp, ts, raw]."""
    if gmembers.empty:
        return pd.DataFrame(columns=["grp", "ts", "raw"])
    con.register("sector_members", gmembers[["item_id", "grp", "weight"]].copy())
    try:
        hist = con.execute(
            f"""
            WITH h AS (
                SELECT item_id, ts, (avg_high + avg_low) / 2.0 AS mid
                FROM history
                WHERE timestep = '{timestep}' AND avg_high IS NOT NULL AND avg_low IS NOT NULL
            ), mx AS (SELECT max(ts) AS t FROM h)
            SELECT h.item_id, h.ts, h.mid, m.grp, m.weight
            FROM h CROSS JOIN mx JOIN sector_members m ON h.item_id = m.item_id
            WHERE h.ts >= mx.t - INTERVAL '{_DAYS_FOR.get(timestep, 15)}' DAY
            """
        ).df()
    finally:
        con.unregister("sector_members")
    if hist.empty:
        return pd.DataFrame(columns=["grp", "ts", "raw"])
    hist = hist.sort_values(["item_id", "ts"])
    base = hist.groupby("item_id")["mid"].transform("first")
    hist["norm"] = np.where(base > 0, hist["mid"] / base, np.nan)
    hist = hist.dropna(subset=["norm"])
    hist["wn"] = hist["norm"] * hist["weight"]
    g = hist.groupby(["grp", "ts"]).agg(wn=("wn", "sum"), w=("weight", "sum")).reset_index()
    g["raw"] = np.where(g["w"] > 0, g["wn"] / g["w"], np.nan)
    return g[["grp", "ts", "raw"]]


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

"""Set <-> components conversion arbitrage (the GE clerk packs/unpacks item sets for FREE).

Validated 2026-07-02 on 365d of daily bars, execution-realistic (pieces filled at the day's
avg_low like our overnight lowballs; set sold at the day's avg_high, net of the 2% tax):

    Torag's +9.6% median/cycle (ROI>2% on 90% of days) - Guthan's +8.9% - Dagon'hai +7.2%
    Verac's +7.0% - Karil's +6.3% - Sunfire +4.5% - Ahrim's +3.5% - Obsidian +2.9%

Structural cause: Barrows-style drops ENTER the game as pieces (supply pressure on pieces),
while outfit buyers pay a convenience premium for the one-click set. Harvesting needs patient
two-sided execution + clerk trips - unattractive to 5-minute margin flippers, PERFECT for the
2-touch rhythm: lowball the pieces overnight, combine at the clerk in the morning, list the set.

The Moons sets are clerk-exchangeable (verified in-game 2026-07-02) and contain FOUR pieces —
helm + chestplate + tassets + the WEAPON. The earlier "+33% persistent basis" was a mapping
artifact (armour-only components, omitting the weapon's cost); with the correct mapping their
basis is ordinary. The too-good-to-be-true rule caught its own bug. Standing rule stays: one
cheap test cycle before scaling any newly-verified set.
"""
from __future__ import annotations

import pandas as pd

from . import tax as taxmod
from .db import connect, latest_snapshot_df
from .liquidity import fill_uptime

_A = chr(39)
SETS: dict[str, list[str]] = {
    f"Torag{_A}s armour set": [f"Torag{_A}s helm", f"Torag{_A}s platebody", f"Torag{_A}s platelegs", f"Torag{_A}s hammers"],
    f"Guthan{_A}s armour set": [f"Guthan{_A}s helm", f"Guthan{_A}s platebody", f"Guthan{_A}s chainskirt", f"Guthan{_A}s warspear"],
    f"Karil{_A}s armour set": [f"Karil{_A}s coif", f"Karil{_A}s leathertop", f"Karil{_A}s leatherskirt", f"Karil{_A}s crossbow"],
    f"Verac{_A}s armour set": [f"Verac{_A}s helm", f"Verac{_A}s brassard", f"Verac{_A}s plateskirt", f"Verac{_A}s flail"],
    f"Dharok{_A}s armour set": [f"Dharok{_A}s helm", f"Dharok{_A}s platebody", f"Dharok{_A}s platelegs", f"Dharok{_A}s greataxe"],
    f"Ahrim{_A}s armour set": [f"Ahrim{_A}s hood", f"Ahrim{_A}s robetop", f"Ahrim{_A}s robeskirt", f"Ahrim{_A}s staff"],
    f"Dagon{_A}hai robes set": [f"Dagon{_A}hai hat", f"Dagon{_A}hai robe top", f"Dagon{_A}hai robe bottom"],
    "Sunfire fanatic armour set": ["Sunfire fanatic helm", "Sunfire fanatic cuirass", "Sunfire fanatic chausses"],
    "Obsidian armour set": ["Obsidian helmet", "Obsidian platebody", "Obsidian platelegs"],
    "Dragon armour set (lg)": ["Dragon full helm", "Dragon platebody", "Dragon platelegs", "Dragon kiteshield"],
    # Moons sets INCLUDE the weapon (Tristan verified in-game 2026-07-02) — the armour-only mapping
    # printed a fake +33% basis by omitting a quarter of the cost.
    "Blood moon armour set": ["Blood moon helm", "Blood moon chestplate", "Blood moon tassets", "Dual macuahuitl"],
    "Eclipse moon armour set": ["Eclipse moon helm", "Eclipse moon chestplate", "Eclipse moon tassets", "Eclipse atlatl"],
    "Blue moon armour set": ["Blue moon helm", "Blue moon chestplate", "Blue moon tassets", "Blue moon spear"],
}
# clerk exchange confirmed for the classic families; Moons sets (Blood/Eclipse/Blue — armour-only:
# helm + chestplate + tassets, no weapon) verified IN-GAME by Tristan 2026-07-02. Sunfire remains
# unverified. Standing rule for any newly-verified set: run ONE cheap test cycle before scaling.
VERIFIED = {k for k in SETS if any(t in k for t in ("Torag", "Guthan", "Karil", "Verac", "Dharok",
                                                    "Ahrim", "Dagon", "Obsidian", "Dragon", "moon"))}

# Potion DECANT routes (Bob Barter at the GE decants ANY potion for free — mechanic is certain).
# Validated 365d, execution-realistic (buy form at bid -> decant -> sell form at ask, net of tax):
# Super attack (3)->(4) +4.1% median (87% of days >2%), Ancient brew +9.9%/85%, Superantipoison
# +9.2%/94%, Antifire +7.8%/86%, Super energy +3.1%/72%, Super strength +2.9%/80%, Super defence
# +2.8%/79%, Ranging +3.1%/74%, Prayer regeneration +2.8%/67%. The two famous routes (Prayer
# potion, Saradomin brew ~+1.1%) are already competed away — the edge lives in the second tier.
DECANTS: list[tuple[str, int, int]] = [
    ("Super attack", 3, 4), ("Super strength", 3, 4), ("Super defence", 3, 4),
    ("Super energy", 3, 4), ("Ranging potion", 3, 4), ("Ancient brew", 3, 4),
    ("Antifire potion", 3, 4), ("Superantipoison", 3, 4), ("Prayer regeneration potion", 3, 4),
    ("Prayer potion", 3, 4), ("Saradomin brew", 3, 4), ("Super restore", 3, 4),
    ("Stamina potion", 3, 4), ("Super combat potion", 4, 1), ("Divine super combat potion", 3, 4),
]


def scan(con=None) -> list[dict]:
    """Live combine-arb scan: patient piece lowballs (at the bid) -> clerk -> list the set at the
    ask, net of tax. Also flags any INSTANT arb (crossing the full spread both ways still pays)."""
    own = con is None
    con = con or connect(read_only=True)
    try:
        items = con.execute("SELECT item_id, name FROM items").df()
        byname = dict(zip(items["name"], items["item_id"]))
        snap = latest_snapshot_df(con).set_index("item_id")
    finally:
        if own:
            con.close()

    resolved = {s: (int(byname[s]), [int(byname[c]) for c in comps])
                for s, comps in SETS.items() if s in byname and all(c in byname for c in comps)}
    dec = []
    for fam, bd, sd in DECANTS:
        bi, si = byname.get(f"{fam}({bd})"), byname.get(f"{fam}({sd})")
        if bi is not None and si is not None:
            dec.append((fam, bd, sd, int(bi), int(si)))
    prof = fill_uptime([sid for sid, _ in resolved.values()] + [si for *_x, si in dec])

    def px(iid: int, col: str):
        try:
            v = snap.loc[iid, col]
            return float(v) if pd.notna(v) else None
        except KeyError:
            return None

    rows = []
    for s, (sid, comps) in resolved.items():
        piece_bids = [px(c, "instasell") for c in comps]     # patient lowball fills land ~here
        piece_asks = [px(c, "instabuy") for c in comps]
        set_ask, set_bid = px(sid, "instabuy"), px(sid, "instasell")
        if None in piece_bids or set_ask is None or not set_ask > 0:
            continue
        cost = sum(piece_bids)
        net = taxmod.net_sell(int(round(set_ask)), False) - cost
        roi = net / cost if cost > 0 else 0.0
        instant = None
        if None not in piece_asks and set_bid:
            icost = sum(piece_asks)
            instant = (taxmod.net_sell(int(round(set_bid)), False) - icost) / icost if icost > 0 else None
        pr = prof.get(sid, {})
        rows.append({
            "set_id": sid, "name": s, "pieces": SETS[s],
            "pieces_cost": round(cost), "set_sell": round(set_ask),
            "net_per_set": round(net), "roi": round(roi, 4),
            "instant_roi": round(instant, 4) if instant is not None else None,
            "set_sell_uptime": round(pr.get("sell", 0.0), 3),
            "sets_per_day": round(pr.get("sell_units_day", 0.0), 1),
            "verified": s in VERIFIED, "kind": "set",
        })

    # potion decants: normalize per SELL-form unit (buying sd/bd of the buy form makes one sell unit)
    for fam, bd, sd, bi, si in dec:
        b_bid, s_ask, s_bid, b_ask = px(bi, "instasell"), px(si, "instabuy"), px(si, "instasell"), px(bi, "instabuy")
        if b_bid is None or s_ask is None or not b_bid > 0 or not s_ask > 0:
            continue
        cost = b_bid * sd / bd
        net = taxmod.net_sell(int(round(s_ask)), False) - cost
        roi = net / cost if cost > 0 else 0.0
        instant = None
        if b_ask and s_bid:
            icost = b_ask * sd / bd
            instant = (taxmod.net_sell(int(round(s_bid)), False) - icost) / icost if icost > 0 else None
        pr = prof.get(si, {})
        rows.append({
            "set_id": si, "name": f"{fam} ({bd})→({sd})", "pieces": [f"{fam}({bd})"],
            "pieces_cost": round(cost), "set_sell": round(s_ask),
            "net_per_set": round(net), "roi": round(roi, 4),
            "instant_roi": round(instant, 4) if instant is not None else None,
            "set_sell_uptime": round(pr.get("sell", 0.0), 3),
            "sets_per_day": round(pr.get("sell_units_day", 0.0), 1),
            "verified": True, "kind": "decant",   # Bob Barter decants any potion — mechanic certain
        })
    rows.sort(key=lambda r: r["roi"], reverse=True)
    return rows

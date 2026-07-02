"""Set <-> components conversion arbitrage (the GE clerk packs/unpacks item sets for FREE).

Validated 2026-07-02 on 365d of daily bars, execution-realistic (pieces filled at the day's
avg_low like our overnight lowballs; set sold at the day's avg_high, net of the 2% tax):

    Torag's +9.6% median/cycle (ROI>2% on 90% of days) - Guthan's +8.9% - Dagon'hai +7.2%
    Verac's +7.0% - Karil's +6.3% - Sunfire +4.5% - Ahrim's +3.5% - Obsidian +2.9%

Structural cause: Barrows-style drops ENTER the game as pieces (supply pressure on pieces),
while outfit buyers pay a convenience premium for the one-click set. Harvesting needs patient
two-sided execution + clerk trips - unattractive to 5-minute margin flippers, PERFECT for the
2-touch rhythm: lowball the pieces overnight, combine at the clerk in the morning, list the set.

CAUTION: the Moons sets (Blood/Eclipse/Blue) show a persistent +9%..+40% basis that is TOO wide -
strong suspicion they are NOT exchangeable at the clerk (verify the clerk's Sets tab in-game
before trading them). Classic sets (Barrows, Dagon'hai, Obsidian, Dragon, gilded) are.
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
    "Blood moon armour set": ["Blood moon helm", "Blood moon chestplate", "Blood moon tassets"],
    "Eclipse moon armour set": ["Eclipse moon helm", "Eclipse moon chestplate", "Eclipse moon tassets"],
    "Blue moon armour set": ["Blue moon helm", "Blue moon chestplate", "Blue moon tassets"],
}
# clerk exchange confirmed for the classic families; the newer ones must be VERIFIED IN-GAME
# (GE clerk -> Sets tab) before any capital touches them.
VERIFIED = {k for k in SETS if any(t in k for t in ("Torag", "Guthan", "Karil", "Verac", "Dharok",
                                                    "Ahrim", "Dagon", "Obsidian", "Dragon"))}


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
    prof = fill_uptime([sid for sid, _ in resolved.values()])

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
            "verified": s in VERIFIED,
        })
    rows.sort(key=lambda r: r["roi"], reverse=True)
    return rows

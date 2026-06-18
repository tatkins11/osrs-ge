"""Personal position & P&L tracker computed from the logged trade log.

Moving-average cost basis. Selling applies the 2% GE tax to proceeds, so
realized/unrealized P&L is what you actually keep. Open positions are valued at
the current insta-buy price (what you'd realistically place a sell offer at),
net of tax.
"""
from __future__ import annotations

import pandas as pd

from . import tax as taxmod
from .db import connect, get_items_df, get_trades_df, latest_snapshot_df
from .sectors import SECTOR_META, classify_one
from .signals import Thresholds, market_signals


def compute(con=None) -> dict:
    own = con is None
    con = con or connect(read_only=True)
    try:
        trades = get_trades_df()  # separate trades DB (own lock); items/latest use the prices con
        items = get_items_df(con).set_index("item_id")
        latest = latest_snapshot_df(con).set_index("item_id")
        # fair-value / risk context for held items (one market pass)
        ms = market_signals(Thresholds(), con)
        info: dict[int, dict] = {}
        if not ms.empty:
            for r in ms[["item_id", "established", "alch_floor", "z_7d", "drawdown"]].itertuples(index=False):
                info[int(r.item_id)] = {"est": r.established, "alch_floor": r.alch_floor, "z": r.z_7d}
    finally:
        if own:
            con.close()

    def fnum(x):
        return float(x) if x is not None and pd.notna(x) else None

    def name_of(iid: int) -> str:
        return items.loc[iid, "name"] if iid in items.index else str(iid)

    def exempt_of(iid: int) -> bool:
        return bool(items.loc[iid, "exempt"]) if iid in items.index else False

    def cur_high(iid: int):
        if iid in latest.index and pd.notna(latest.loc[iid, "instabuy"]):
            return float(latest.loc[iid, "instabuy"])
        return None

    pos: dict[int, dict] = {}
    trade_log: list[dict] = []
    for t in trades.itertuples():
        iid = int(t.item_id)
        qty = int(t.qty)
        price = float(t.price)
        p = pos.setdefault(iid, {"qty": 0.0, "avg_cost": 0.0, "realized": 0.0})
        if t.side == "buy":
            new_qty = p["qty"] + qty
            p["avg_cost"] = (p["avg_cost"] * p["qty"] + price * qty) / new_qty if new_qty > 0 else 0.0
            p["qty"] = new_qty
        else:  # sell — proceeds net of 2% tax
            net = taxmod.net_sell(int(price), exempt_of(iid))
            p["realized"] += qty * (net - p["avg_cost"])
            p["qty"] = max(0.0, p["qty"] - qty)
        trade_log.append({
            "id": int(t.id), "ts": str(t.ts), "item_id": iid, "name": name_of(iid),
            "side": t.side, "qty": qty, "price": int(price), "note": getattr(t, "note", "") or "",
        })

    open_positions = []
    realized_total = 0.0
    unrealized_total = 0.0
    for iid, p in pos.items():
        realized_total += p["realized"]
        if p["qty"] <= 0.5:
            continue
        ch = cur_high(iid)
        cur_net = taxmod.net_sell(int(ch), exempt_of(iid)) if ch else None
        unreal = p["qty"] * (cur_net - p["avg_cost"]) if cur_net is not None else None
        if unreal is not None:
            unrealized_total += unreal
        nfo = info.get(iid, {})
        est = fnum(nfo.get("est"))                                   # 7d established fair value
        target_net = taxmod.net_sell(int(round(est)), exempt_of(iid)) if est else None
        to_target = ((est - ch) / ch) if (est and ch) else None      # upside from current price to fair value
        if cur_net is None:
            status = "no price"
        elif est and ch and ch >= est * 0.99:
            status = "sell"          # reverted to / above fair value -> take profit
        elif cur_net < p["avg_cost"]:
            status = "underwater"
        else:
            status = "hold"
        open_positions.append({
            "item_id": iid, "name": name_of(iid), "qty": int(p["qty"]),
            "avg_cost": round(p["avg_cost"]),
            "breakeven": round(taxmod.breakeven_sell(p["avg_cost"], exempt_of(iid))),  # gross sell to recover cost after tax
            "cur_price": round(ch) if ch else None,                                   # current insta-buy = where to place a sell
            "cur_net": cur_net, "cost_basis": round(p["avg_cost"] * p["qty"]),
            "market_value": round(cur_net * p["qty"]) if cur_net is not None else None,
            "unrealized": round(unreal) if unreal is not None else None,              # after 2% sell tax
            "unrealized_pct": (unreal / (p["avg_cost"] * p["qty"])) if (unreal is not None and p["avg_cost"] > 0) else None,
            "target": round(est) if est else None,                                    # fair-value sell target
            "target_net": target_net,
            "to_target": to_target,
            "alch_floor": round(fnum(nfo.get("alch_floor"))) if fnum(nfo.get("alch_floor")) else None,
            "sector": classify_one(name_of(iid)),
            "status": status,
        })

    open_positions.sort(key=lambda x: (x["unrealized"] if x["unrealized"] is not None else 0), reverse=True)
    trade_log.reverse()  # newest first

    # capital concentration by sector + an alert count for held items now worth selling
    exposure: dict[str, float] = {}
    for op in open_positions:
        key = op["sector"] or "other"
        exposure[key] = exposure.get(key, 0.0) + op["cost_basis"]
    total_inv = sum(exposure.values()) or 1.0
    sector_exposure = sorted(
        [{"sector": k, "label": SECTOR_META.get(k, {}).get("label", k.replace("_", " ").title()),
          "capital": round(v), "pct": v / total_inv} for k, v in exposure.items()],
        key=lambda x: -x["capital"],
    )
    n_alerts = sum(1 for op in open_positions if op["status"] == "sell")

    return {
        "open_positions": open_positions,
        "trades": trade_log,
        "realized_total": round(realized_total),
        "unrealized_total": round(unrealized_total),
        "invested": round(sum(p["cost_basis"] for p in open_positions)),
        "n_trades": len(trade_log),
        "n_open": len(open_positions),
        "sector_exposure": sector_exposure,
        "n_alerts": n_alerts,
    }

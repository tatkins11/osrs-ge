"""Personal position & P&L tracker computed from the logged trade log.

FIFO lot accounting. Each sell is matched against the oldest open buy lots to
produce a discrete **closed round-trip** (entry avg, exit, qty, hold time, gross,
2% sell tax, net P&L, ROI). Open positions are the unconsumed lots, valued at the
current insta-buy price (where you'd realistically place a sell offer), net of tax.
"""
from __future__ import annotations

from collections import defaultdict, deque

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

    # FIFO lots per item: deque of [qty_remaining, unit_cost, buy_ts]
    lots: dict[int, deque] = defaultdict(deque)
    realized_by_item: dict[int, float] = defaultdict(float)
    closed_trips: list[dict] = []
    trip_events: list[tuple] = []   # (sell_ts, net) for the equity curve
    trade_log: list[dict] = []

    for t in trades.itertuples():
        iid = int(t.item_id)
        qty = int(t.qty)
        price = float(t.price)
        ts = pd.Timestamp(t.ts)
        if t.side == "buy":
            lots[iid].append([float(qty), price, ts])
        else:  # sell -> match against oldest buy lots (FIFO), net of 2% tax
            net_unit = float(taxmod.net_sell(int(price), exempt_of(iid)))
            remaining = float(qty)
            matched = cost_sum = wts_sum = 0.0
            dq = lots[iid]
            while remaining > 1e-9 and dq:
                lot = dq[0]
                take = min(remaining, lot[0])
                matched += take
                cost_sum += take * lot[1]
                wts_sum += take * lot[2].value           # ns, qty-weighted entry time
                lot[0] -= take
                remaining -= take
                if lot[0] <= 1e-9:
                    dq.popleft()
            if matched > 1e-9:                            # ignore oversells with no basis to match
                buy_avg = cost_sum / matched
                buy_ts = pd.Timestamp(int(wts_sum / matched))
                net = matched * (net_unit - buy_avg)
                hold_days = max(0.0, (ts - buy_ts).total_seconds() / 86400.0)
                realized_by_item[iid] += net
                trip_events.append((ts, net))
                closed_trips.append({
                    "item_id": iid, "name": name_of(iid), "qty": int(round(matched)),
                    "buy_avg": round(buy_avg), "sell_price": int(price),
                    "gross": round(matched * (price - buy_avg)),
                    "tax": round(matched * (price - net_unit)),
                    "net": round(net),
                    "roi": (net / (matched * buy_avg)) if buy_avg > 0 else None,
                    "buy_ts": str(buy_ts), "sell_ts": str(ts), "hold_days": round(hold_days, 1),
                    "sector": classify_one(name_of(iid)),
                })
        trade_log.append({
            "id": int(t.id), "ts": str(t.ts), "item_id": iid, "name": name_of(iid),
            "side": t.side, "qty": qty, "price": int(price), "note": getattr(t, "note", "") or "",
        })

    # open positions = unconsumed lots, valued net of tax at the current insta-buy price
    open_positions = []
    unrealized_total = 0.0
    for iid, dq in lots.items():
        rem_qty = sum(l[0] for l in dq)
        if rem_qty <= 0.5:
            continue
        avg_cost = sum(l[0] * l[1] for l in dq) / rem_qty
        ch = cur_high(iid)
        cur_net = taxmod.net_sell(int(ch), exempt_of(iid)) if ch else None
        unreal = rem_qty * (cur_net - avg_cost) if cur_net is not None else None
        if unreal is not None:
            unrealized_total += unreal
        nfo = info.get(iid, {})
        est = fnum(nfo.get("est"))                                    # 7d established fair value
        target_net = taxmod.net_sell(int(round(est)), exempt_of(iid)) if est else None
        to_target = ((est - ch) / ch) if (est and ch) else None       # upside from current price to fair value
        if cur_net is None:
            status = "no price"
        elif est and ch and ch >= est * 0.99:
            status = "sell"          # reverted to / above fair value -> take profit
        elif cur_net < avg_cost:
            status = "underwater"
        else:
            status = "hold"
        open_positions.append({
            "item_id": iid, "name": name_of(iid), "qty": int(rem_qty),
            "avg_cost": round(avg_cost),
            "breakeven": round(taxmod.breakeven_sell(avg_cost, exempt_of(iid))),  # gross sell to recover cost after tax
            "cur_price": round(ch) if ch else None,                              # current insta-buy = where to place a sell
            "cur_net": cur_net, "cost_basis": round(avg_cost * rem_qty),
            "market_value": round(cur_net * rem_qty) if cur_net is not None else None,
            "unrealized": round(unreal) if unreal is not None else None,         # after 2% sell tax
            "unrealized_pct": (unreal / (avg_cost * rem_qty)) if (unreal is not None and avg_cost > 0) else None,
            "target": round(est) if est else None,                              # fair-value sell target
            "target_net": target_net,
            "to_target": to_target,
            "alch_floor": round(fnum(nfo.get("alch_floor"))) if fnum(nfo.get("alch_floor")) else None,
            "sector": classify_one(name_of(iid)),
            "status": status,
        })

    open_positions.sort(key=lambda x: (x["unrealized"] if x["unrealized"] is not None else 0), reverse=True)
    trade_log.reverse()                      # newest first
    closed_trips.reverse()                   # newest first (for display)

    # round-trip performance stats
    nets = [tr["net"] for tr in closed_trips]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    realized_total = sum(realized_by_item.values())
    stats = {
        "n_closed": len(nets),
        "win_rate": (len(wins) / len(nets)) if nets else None,
        "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(losses) / len(losses)) if losses else None,
        "best": max(nets) if nets else None,
        "worst": min(nets) if nets else None,
        "total_tax": round(sum(tr["tax"] for tr in closed_trips)),
        "avg_hold_days": (sum(tr["hold_days"] for tr in closed_trips) / len(nets)) if nets else None,
        "realized_total": round(realized_total),
    }
    realized_by_item_list = sorted(
        ({"item_id": iid, "name": name_of(iid), "net": round(v)} for iid, v in realized_by_item.items()),
        key=lambda x: -x["net"],
    )
    # cumulative realized over time (equity curve)
    cum = 0.0
    equity_curve = []
    for sell_ts, net in sorted(trip_events, key=lambda e: e[0]):
        cum += net
        equity_curve.append({"ts": str(sell_ts), "cum": round(cum)})

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
        "closed_trips": closed_trips,
        "stats": stats,
        "realized_by_item": realized_by_item_list,
        "equity_curve": equity_curve,
        "realized_total": round(realized_total),
        "unrealized_total": round(unrealized_total),
        "invested": round(sum(p["cost_basis"] for p in open_positions)),
        "n_trades": len(trade_log),
        "n_open": len(open_positions),
        "sector_exposure": sector_exposure,
        "n_alerts": n_alerts,
    }

"""The unified 8-slot decision engine.

Given your open positions (what you hold), live open orders (what's already on the GE),
and available capital, this produces ONE refined recommendation for all 8 Grand Exchange
slots: which holdings to SELL / HOLD / CUT and at what price, and which BUYS to place in the
free slots -- all with competitive pricing and realistic fill timelines.

Design:
- Every holding gets a verdict. Profitable + reverted -> SELL (take profit). Profitable but
  short of fair value -> HOLD at the target. Underwater -> a per-item RECOVERY SCORE decides
  HOLD (oversold / value / crash / floor-protected, thesis intact) vs CUT (sinking ship:
  post-update damage, falling fair value, or fair value now below your cost).
- Prices are COMPETITIVE: buy a small step above the bid, sell a small step below the ask, so
  you win the queue against others reading the same order data (balanced nudge) without giving
  away the whole spread.
- Timelines are REALISTIC: orders are sized to what you can actually buy AND sell within ~a day
  at the item's real volume (your capture share), not the buy limit -- low-value items rarely
  hit their limit, so their expected profit is throttled to what the market can fill.
- Slots: holdings you should list take priority (CUT, then SELL, then HOLD-at-target); buys
  fill whatever's left, diversified by the per-position risk cap. Recommender only.
"""
from __future__ import annotations

import pandas as pd

from . import portfolio as pf
from . import tax as taxmod
from .db import connect, get_orders_df
from .signals import Thresholds, market_signals

CAPTURE = 0.125          # realistic share of an item's daily volume one flipper can transact
TARGET_RT_H = 24.0       # size orders to buy+sell within ~a day (the realistic-timeline cap)
PRICE_EDGE = 0.10        # balanced nudge: give up ~10% of the spread per side to win the queue
HOLD_MIN = 50.0          # recovery score >= this -> hold an underwater position
CUT_MAX = 35.0           # recovery score < this -> cut it


def _f(x):
    return float(x) if x is not None and pd.notna(x) else None


def competitive(bid, ask, edge: float = PRICE_EDGE):
    """Nudge into the spread: buy a step above the bid, sell a step below the ask."""
    bid, ask = _f(bid), _f(ask)
    if bid is None or ask is None or ask <= bid:
        return bid, ask
    step = max(1.0, round(edge * (ask - bid)))
    return bid + step, ask - step


def timeline(units: float, vol_day: float, limit: float):
    """Realistic round-trip: how long to buy `units` and sell them at the item's real volume,
    gated by the 4h buy-limit reset, and the daily units that implies."""
    units = max(0.0, units)
    rate = CAPTURE * max(0.0, vol_day) / 24.0            # units/hour you can transact one side
    leg_h = units / rate if rate > 0 else 720.0
    gate_h = 4.0 if (limit and units >= limit) else 0.0  # full-limit batch can't re-buy for 4h
    rt_h = min(720.0, max(0.5, max(2.0 * leg_h, gate_h + leg_h)))
    daily_units = min(units * (24.0 / rt_h), CAPTURE * max(0.0, vol_day))
    return round(leg_h, 1), round(rt_h, 1), max(0.0, daily_units)


def size_for_timeline(vol_day: float) -> float:
    """How many units you could realistically buy AND sell within the target round-trip."""
    rate = CAPTURE * max(0.0, vol_day) / 24.0
    return max(1.0, rate * TARGET_RT_H / 2.0)


def recovery_score(sig: dict, avg_cost: float, cur_price: float | None) -> tuple[float, list[str]]:
    """0-100 read on whether an UNDERWATER holding is likely to recover (high -> hold) or is a
    sinking ship (low -> cut). Built from the same variables the rest of the tool tracks."""
    score, why = 50.0, []
    z = _f(sig.get("z_7d"))
    if z is not None:
        adj = max(-2.0, min(2.0, -z)) * 12.0            # oversold (z<0) -> +, stretched high -> -
        score += adj
        if z <= -1.0:
            why.append(f"oversold (z {z:.1f})")
    if sig.get("is_value_buy"):
        score += 12; why.append("flagged undervalued")
    vc = _f(sig.get("value_confidence"))
    if vc is not None:
        score += (vc - 50.0) * 0.2
    if sig.get("is_crash"):
        score += 10; why.append("crash-recover setup")
    alch, cur = _f(sig.get("alch_floor")), _f(cur_price)
    if alch and cur and cur <= alch * 1.10:
        score += 10; why.append("near alch floor (downside-capped)")
    if sig.get("post_update_drop"):
        score -= 25; why.append("hit by a game update")
    p30 = _f(sig.get("pct_30d"))
    if p30 is not None and p30 < -0.15:
        score -= 15; why.append(f"down {p30 * 100:.0f}% in 30d")
    est = _f(sig.get("established"))
    if est and avg_cost and est < avg_cost * 0.95:
        score -= 18; why.append("fair value now below your cost")
    return max(0.0, min(100.0, score)), why


def build_plan(th: Thresholds | None = None, con=None) -> dict:
    th = th or Thresholds()
    own = con is None
    con = con or connect(read_only=True)
    try:
        port = pf.compute(con)
        ms = market_signals(th, con)
    finally:
        if own:
            con.close()

    sig: dict[int, dict] = {}
    if not ms.empty:
        cols = ["item_id", "name", "buy_price", "sell_price", "mid", "vol_daily_7d", "buy_limit",
                "established", "z_7d", "pct_30d", "alch_floor", "value_confidence", "is_value_buy",
                "is_crash", "post_update_drop", "net_margin", "slip_margin", "exempt", "flip_ok"]
        have = [c for c in cols if c in ms.columns]
        for r in ms[have].itertuples(index=False):
            sig[int(r.item_id)] = {c: getattr(r, c) for c in have}

    positions = port.get("open_positions", [])
    odf = get_orders_df()
    open_orders = odf[odf["state"].isin(["BUYING", "SELLING"])] if not odf.empty else odf.iloc[0:0]
    live_buy_ids = {int(x) for x in open_orders[open_orders["side"] == "buy"]["item_id"].tolist()} if not open_orders.empty else set()
    live_sell_ids = {int(x) for x in open_orders[open_orders["side"] == "sell"]["item_id"].tolist()} if not open_orders.empty else set()
    committed = 0.0
    if not open_orders.empty:
        b = open_orders[open_orders["side"] == "buy"]
        if not b.empty:
            committed = float(((b["total_qty"].fillna(0) - b["filled_qty"].fillna(0)).clip(lower=0) * b["price"].fillna(0)).sum())

    # ---- 1) verdict on every holding -------------------------------------------------------
    sells = []
    held_ids = set()
    for p in positions:
        iid = int(p["item_id"])
        held_ids.add(iid)
        s = sig.get(iid, {})
        qty = float(p["qty"])
        avg_cost = float(p["avg_cost"])
        exempt = bool(s.get("exempt"))
        bid, ask = s.get("buy_price"), s.get("sell_price")
        _, sell_at = competitive(bid, ask)
        sell_at = sell_at or _f(p.get("cur_price"))
        target = _f(p.get("target"))
        cur_net = _f(p.get("cur_net"))
        net_at = taxmod.net_sell(int(round(sell_at)), exempt) if sell_at else None
        profitable = cur_net is not None and cur_net >= avg_cost
        at_fair = p.get("status") == "sell"
        to_target = _f(p.get("to_target")) or 0.0
        vol_day = _f(s.get("vol_daily_7d")) or 0.0
        limit = _f(s.get("buy_limit")) or 0.0

        rec_score, why = (None, [])
        if profitable and (at_fair or to_target < 0.03):
            action, list_at, reason = "SELL", sell_at, "at/above fair value — take profit"
        elif profitable:
            list_at = target or sell_at
            action, reason = "HOLD", f"in profit, {to_target * 100:.0f}% upside left to fair value — list at target"
        else:
            rec_score, why = recovery_score(s, avg_cost, p.get("cur_price"))
            if rec_score >= HOLD_MIN:
                list_at = target or sell_at
                action = "HOLD"
                reason = "underwater but likely to recover — " + (", ".join(why) if why else "thesis intact")
            elif rec_score < CUT_MAX:
                action, list_at, reason = "CUT", sell_at, "sinking ship — " + (", ".join(why) if why else "no recovery signal")
            else:  # grey zone: small loss gets room, bigger loss is cut
                upct = _f(p.get("unrealized_pct")) or 0.0
                if upct > -0.10:
                    list_at, action, reason = (target or sell_at), "HOLD", "small loss, give it room to revert"
                else:
                    action, list_at, reason = "CUT", sell_at, "loss deepening with a weak recovery read"

        net_list = taxmod.net_sell(int(round(list_at)), exempt) if list_at else None
        expected_net = round((net_list - avg_cost) * qty) if net_list is not None else None
        leg_h, rt_h, _ = timeline(qty, vol_day, limit)
        sells.append({
            "action": action, "item_id": iid, "name": p["name"], "qty": int(qty),
            "price": round(list_at) if list_at else None, "avg_cost": round(avg_cost),
            "cur_price": p.get("cur_price"), "target": round(target) if target else None,
            "expected_net": expected_net, "unrealized": p.get("unrealized"),
            "unrealized_pct": p.get("unrealized_pct"), "sell_h": leg_h,
            "recovery_score": round(rec_score) if rec_score is not None else None,
            "reason": reason, "live": iid in live_sell_ids, "sector": p.get("sector"),
        })

    # ---- 2) buys for the free slots --------------------------------------------------------
    cap0 = max(0.0, float(th.bankroll) - committed)
    per_slot_cap = float(th.max_alloc_frac or 0) * float(th.bankroll or cap0)
    # a holding we're listing occupies a slot; buys get whatever's left of the 8
    sell_slots = min(len(sells), 8)
    free_slots = max(0, 8 - sell_slots)
    excl = held_ids | live_buy_ids
    buys = []
    if free_slots > 0 and not ms.empty:
        cands = ms[ms["flip_ok"].fillna(False) & ~ms["item_id"].astype(int).isin(excl)
                   & (ms["slip_margin"].fillna(-1.0) > 0)].copy()
        # rank by competitive gp/day
        rows = []
        for r in cands.itertuples(index=False):
            bid, ask = _f(r.buy_price), _f(r.sell_price)
            buy_at, sell_at = competitive(bid, ask)
            if not buy_at or not sell_at:
                continue
            exempt = bool(getattr(r, "exempt", False))
            margin = taxmod.net_sell(int(round(sell_at)), exempt) - buy_at
            if margin <= 0:
                continue
            vol_day = _f(r.vol_daily_7d) or 0.0
            limit = _f(r.buy_limit) or (vol_day / 12.0)
            liq = size_for_timeline(vol_day)
            cap_units = (per_slot_cap // buy_at) if per_slot_cap > 0 else liq
            rows.append((margin, buy_at, sell_at, exempt, vol_day, limit, liq, cap_units, r))
        rows.sort(key=lambda x: x[0] * x[6], reverse=True)  # ~margin x liquidity-units = rough gp potential
        remaining = cap0
        for margin, buy_at, sell_at, exempt, vol_day, limit, liq, cap_units, r in rows:
            if len(buys) >= free_slots:
                break
            if remaining < buy_at:
                continue
            units = int(min(liq, limit, cap_units, remaining // buy_at))
            if units < 1:
                continue
            leg_h, rt_h, daily_units = timeline(units, vol_day, limit)
            cap_used = units * buy_at
            buys.append({
                "action": "BUY", "item_id": int(r.item_id), "name": r.name,
                "price": round(buy_at), "sell_target": round(sell_at), "units": units,
                "capital": round(cap_used), "margin": round(margin),
                "gp_day": round(margin * daily_units), "buy_h": leg_h, "roundtrip_h": rt_h,
                "reason": f"competitive buy; ~{units:,} units round-trip in ~{rt_h:.0f}h",
                "live": int(r.item_id) in live_buy_ids,
            })
            remaining -= cap_used

    # ---- 3) assemble the 8-slot plan -------------------------------------------------------
    order = {"CUT": 0, "SELL": 1, "HOLD": 2}
    sells.sort(key=lambda s: (order.get(s["action"], 3), -(s.get("expected_net") or 0)))
    slots = (sells + buys)[:8]
    bench = sells[8:]  # more holdings than slots — these wait for a slot to free

    expected_realized = sum(s["expected_net"] for s in sells if s["action"] in ("SELL", "CUT") and s["expected_net"])
    plan_gp_day = sum(b["gp_day"] for b in buys)
    buy_capital = sum(b["capital"] for b in buys)
    bankroll = float(th.bankroll)
    return {
        "bankroll": round(bankroll), "capital_in": round(cap0), "committed_capital": round(committed),
        "used_slots": int(len(open_orders)), "free_slots": int(free_slots),
        "n_positions": len(positions), "n_sells": len(sells), "n_buys": len(buys),
        "slots": slots, "bench": bench,
        "totals": {
            "expected_realized": round(expected_realized),
            "buy_capital": round(buy_capital),
            "plan_gp_day": round(plan_gp_day),
            "growth_day": round(plan_gp_day / bankroll, 4) if bankroll > 0 else None,
        },
    }

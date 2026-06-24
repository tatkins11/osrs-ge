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
from .db import connect, get_free_gp, get_orders_df
from .signals import Thresholds, market_signals

CAPTURE = 0.04           # realistic share of an item's daily volume you transact per leg. Conservative:
                         # a competitively-priced offer waits in a queue, so real fills are slow. Lowered
                         # from 0.125 on live feedback that buys+sells take much longer; tune w/ order data.
TARGET_RT_H = 24.0       # aim to size orders to buy+sell within ~a day at that (slow) rate
PRICE_EDGE = 0.10        # balanced nudge: give up ~10% of the spread per side to win the queue
HOLD_MIN = 50.0          # recovery score >= this -> hold an underwater position
CUT_MAX = 35.0           # recovery score < this -> cut it
# Margin-mirage guards for the plan's BUYS: a real, capturable flip has a modest spread, a margin
# that actually persists, and one that isn't a wild multiple of the item's own norm. Anything else
# is a stale/illiquid ghost quote (one side of the spread hasn't traded) -> don't recommend it.
MAX_SPREAD_FRAC = 0.12     # raw bid-ask spread above ~12% of price = stale/illiquid, not capturable
MIN_MARGIN_UPTIME = 0.45   # the margin must hold at least this fraction of the last 7 days to be real
MARGIN_SANITY_MULT = 3.0   # skip a margin more than this x the item's own 7d-typical (transient blowout)
# Items the GE restricts to ONE per slot per offer (you can't stack the offer). Only bonds.
ONE_PER_SLOT = {"old school bond"}


def offer_cap(name) -> float:
    """Max units a single GE offer can hold for this item (bonds are 1-per-slot)."""
    return 1.0 if str(name or "").strip().lower() in ONE_PER_SLOT else float("inf")


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
    rt_h = min(720.0, max(2.0, max(2.0 * leg_h, gate_h + leg_h)))  # floor 2h: even liquid flips take a while
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


def _buy_row(r, iid: int, units: float, buy_at: float, sell_at: float, exempt: bool,
             vol_day: float, limit: float, live: bool) -> dict:
    leg_h, rt_h, daily_units = timeline(units, vol_day, limit)
    margin = taxmod.net_sell(int(round(sell_at)), exempt) - buy_at
    return {
        "action": "BUY", "item_id": int(iid), "name": getattr(r, "name", str(iid)),
        "price": round(buy_at), "sell_target": round(sell_at), "units": int(units),
        "capital": round(units * buy_at), "margin": round(margin),
        "gp_day": round(margin * daily_units), "buy_h": leg_h, "roundtrip_h": rt_h,
        "reason": f"competitive buy; ~{int(units):,} units round-trip in ~{rt_h:.0f}h",
        "live": bool(live),
    }


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
            "breakeven": p.get("breakeven"),  # gross sell to recover cost after tax (for lucky-exit listing)
            "cur_price": p.get("cur_price"), "target": round(target) if target else None,
            "expected_net": expected_net, "unrealized": p.get("unrealized"),
            "unrealized_pct": p.get("unrealized_pct"), "sell_h": leg_h,
            "recovery_score": round(rec_score) if rec_score is not None else None,
            "reason": reason, "live": iid in live_sell_ids, "sector": p.get("sector"),
        })

    # ---- 2) capital picture + split holdings on/off slot + reconcile live orders ------------
    _fg = get_free_gp()                                          # server-persisted free gp (source of truth)
    free_gp = max(0.0, float(_fg if _fg is not None else th.bankroll))  # fall back to the filter until set
    holdings_value = float(port.get("invested") or 0.0) + float(port.get("unrealized_total") or 0.0)
    net_worth = free_gp + committed + holdings_value             # cash + open buys + inventory at live value
    cap0 = free_gp                                               # capital available for NEW buys right now
    per_slot_cap = (float(th.max_alloc_frac or 0) * net_worth) if net_worth > 0 else 0.0  # diversify vs TOTAL worth
    active_sells = [s for s in sells if s["action"] in ("SELL", "CUT")]  # need a slot now
    holding = [s for s in sells if s["action"] == "HOLD"]                # held OFF-MARKET, no slot
    active_sell_ids = {s["item_id"] for s in active_sells}
    holding_ids = {s["item_id"] for s in holding}

    # candidate buy universe (flip_ok + positive competitive margin): used to size buys AND to
    # judge whether an existing live buy order is still worth keeping
    cand_rows: dict[int, tuple] = {}
    mirage = 0
    if not ms.empty:
        cands = ms[ms["flip_ok"].fillna(False) & (ms["slip_margin"].fillna(-1.0) > 0)]
        for r in cands.itertuples(index=False):
            buy_at, sell_at = competitive(_f(r.buy_price), _f(r.sell_price))
            if not buy_at or not sell_at:
                continue
            ex = bool(getattr(r, "exempt", False))
            mgn = taxmod.net_sell(int(round(sell_at)), ex) - buy_at
            if mgn <= 0:
                continue
            # mirage guard: drop stale/illiquid ghost spreads so they never reach the plan
            mid = _f(getattr(r, "mid", None)) or 0.0
            raw_spread = (_f(r.sell_price) or 0.0) - (_f(r.buy_price) or 0.0)
            up = _f(getattr(r, "margin_uptime", None))
            med = _f(getattr(r, "margin_median_7d", None))
            if (mid > 0 and raw_spread / mid > MAX_SPREAD_FRAC) \
               or (up is not None and up < MIN_MARGIN_UPTIME) \
               or (med and med > 0 and mgn > MARGIN_SANITY_MULT * med):
                mirage += 1
                continue
            vol_day = _f(r.vol_daily_7d) or 0.0
            limit = _f(r.buy_limit) or (vol_day / 12.0)
            cand_rows[int(r.item_id)] = (mgn, buy_at, sell_at, ex, vol_day, limit, r)
    good_buy_ids = set(cand_rows)

    # reconcile each live order against the plan: keep / reprice / cancel
    reconcile = []
    kept_buy_ids = set()
    kept_buy_info: dict[int, tuple] = {}   # iid -> (price, total_qty, filled_qty) of the actual order
    if not open_orders.empty:
        for o in open_orders.itertuples():
            iid, side = int(o.item_id), str(o.side)
            nm = sig.get(iid, {}).get("name") or str(iid)
            price = int(o.price or 0)
            prog = f"{int(o.filled_qty or 0):,}/{int(o.total_qty or 0):,}"
            if side == "sell":
                if iid in active_sell_ids:
                    tgt = next((s["price"] for s in active_sells if s["item_id"] == iid), None)
                    if tgt and price and abs(price - tgt) / max(price, 1) > 0.02:
                        status, note = "reprice", f"reprice to ~{tgt:,} to stay competitive"
                    else:
                        status, note = "keep", "matches plan — leave it"
                elif iid in holding_ids:
                    status, note = "cancel", "hold off-market for a better price"
                else:
                    status, note = "keep", "sell offer (no tracked holding)"
            else:  # buy
                if iid in good_buy_ids:
                    status, note = "keep", f"still a good buy — keep filling ({prog})"
                    kept_buy_ids.add(iid)
                    kept_buy_info[iid] = (int(o.price or 0), int(o.total_qty or 0), int(o.filled_qty or 0))
                else:
                    status, note = "cancel", "no longer a good buy — cancel & redeploy"
            reconcile.append({"order_id": getattr(o, "order_id", None), "item_id": iid, "name": nm,
                              "side": side, "price": price, "progress": prog, "status": status, "note": note})

    # ---- 3) fill the free slots with new buys ----------------------------------------------
    slots_used_existing = len(active_sells) + len(kept_buy_ids)
    free_slots = max(0, 8 - slots_used_existing)
    excl = held_ids | live_buy_ids   # don't re-recommend held items or items already on a live buy
    new_buys = []
    remaining = cap0
    if free_slots > 0 and good_buy_ids:
        ranked = sorted(cand_rows.items(), key=lambda kv: kv[1][0] * size_for_timeline(kv[1][4]), reverse=True)
        for iid, (mgn, buy_at, sell_at, ex, vol_day, limit, r) in ranked:
            if len(new_buys) >= free_slots:
                break
            if iid in excl or remaining < buy_at:
                continue
            cap_units = (per_slot_cap // buy_at) if per_slot_cap > 0 else size_for_timeline(vol_day)
            units = int(min(size_for_timeline(vol_day), limit, cap_units, remaining // buy_at, offer_cap(r.name)))
            if units < 1:
                continue
            if mgn * units < float(th.min_rt_profit or 0):  # round-trip profit bar — skip thin flips
                continue
            new_buys.append(_buy_row(r, iid, units, buy_at, sell_at, ex, vol_day, limit, live=False))
            remaining -= units * buy_at

    # live buys we're keeping -> show them at their ACTUAL order size (the remaining qty already
    # placed), NOT a re-sized ideal — the gp is committed; we're not suggesting buying more.
    kept_buy_rows = []
    for iid in kept_buy_ids:
        mgn, buy_at, sell_at, ex, vol_day, limit, r = cand_rows[iid]
        o_price, o_total, o_filled = kept_buy_info.get(iid, (0, 0, 0))
        rem_units = max(1, o_total - o_filled)           # what's left to fill on the existing order
        order_buy = float(o_price) if o_price > 0 else buy_at
        row = _buy_row(r, iid, rem_units, order_buy, sell_at, ex, vol_day, limit, live=True)
        row["reason"] = f"live buy — keep filling ({o_filled:,}/{o_total:,})"
        kept_buy_rows.append(row)

    # ---- 4) assemble ------------------------------------------------------------------------
    active_sells.sort(key=lambda s: ({"CUT": 0, "SELL": 1}.get(s["action"], 2), -(s.get("expected_net") or 0)))
    buys = kept_buy_rows + new_buys

    # opportunistic: rather than leave slots empty, LIST holds at a hopeful price — costs nothing to
    # let the offer sit. Profitable holds list at their fair-value target (capture the upside);
    # underwater holds list at BREAKEVEN (a lucky spike lets you escape clean). The hope price must
    # be above the current price, else there's nothing to wait for.
    open_slots = max(0, 8 - len(active_sells) - len(buys))
    listed = []
    if open_slots > 0 and holding:
        # profitable holds first (real upside), then underwater ones (long-shot clean exits)
        for h in sorted(holding, key=lambda x: (x.get("expected_net") or 0), reverse=True):
            if len(listed) >= open_slots:
                break
            cur = _f(h.get("cur_price")) or 0.0
            profit = (h.get("expected_net") or 0) > 0
            price = (_f(h.get("target")) or 0.0) if profit else (_f(h.get("breakeven")) or 0.0)
            if not price or (cur and price <= cur * 1.001):  # nothing to hope for if it's at/below market
                continue
            row = dict(h)
            row["action"] = "LIST"
            row["price"] = round(price)
            if not profit:
                row["expected_net"] = 0   # a breakeven fill is ~zero P&L (you escape clean)
            row["reason"] = ("free slot — listed at fair value to catch a spike (no cost to wait)" if profit
                             else "free slot — listed at breakeven for a lucky clean exit (no cost to wait)")
            listed.append(row)
        promoted = {h["item_id"] for h in listed}
        holding = [h for h in holding if h["item_id"] not in promoted]

    slots = (active_sells + buys + listed)[:8]

    expected_realized = sum(s["expected_net"] for s in active_sells if s["expected_net"])
    plan_gp_day = sum(b.get("gp_day") or 0 for b in buys)
    return {
        "free_gp": round(free_gp), "committed_capital": round(committed),
        "holdings_value": round(holdings_value), "net_worth": round(net_worth),
        "capital_in": round(cap0),
        "free_slots": int(max(0, 8 - len(slots))), "slots_used": int(min(8, len(slots))),
        "n_positions": len(positions), "n_active_sells": len(active_sells),
        "n_holding": len(holding), "n_buys": len(buys), "n_listed": len(listed), "mirage_skipped": int(mirage),
        "slots": slots, "holding": holding, "reconcile": reconcile,
        "totals": {
            "expected_realized": round(expected_realized),
            "buy_capital": round(sum(b.get("capital") or 0 for b in buys)),
            "plan_gp_day": round(plan_gp_day),
            "growth_day": round(plan_gp_day / net_worth, 4) if net_worth > 0 else None,
        },
    }

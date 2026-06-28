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
from .liquidity import fill_uptime, market_clock, peak_hours
from .signals import KNIFE_SLOPE_PER_DAY, Thresholds, market_signals, overnight_table

CAPTURE = 0.08           # share of an item's daily volume you transact per leg. Raised 0.04->0.06->0.08 to
                         # size bigger, more concentrated positions (Tristan wants fewer/bigger high-profit
                         # bets). Tradeoff: a bigger market fraction fills somewhat slower in reality, but the
                         # MIN_SELL_UPTIME gate + exit cap keep every position on a genuinely EXITABLE item.
TARGET_RT_H = 24.0       # aim to size orders to buy+sell within ~a day at that (slow) rate
MAX_FILL_H = 14.0        # skip a buy whose modeled BUY-fill time exceeds this — too illiquid, it'll just
                         # sit unfilled (live data: 71% of orders were cancelled with zero fill). The
                         # sweet spot = high value + nice margin + enough volume to actually fill.
MIN_ROI_FLOOR = 0.03     # the plan always requires >= this competitive ROI (the 2% sell tax destroys
                         # thin-margin high-value flips, e.g. a 0.5% Kodai net). MIN ROI filter raises it.
MIN_BUY_UPTIME = 0.15    # a buy candidate must actually TRADE: >= this fraction of 5-min windows must
                         # have a seller present (last 7d). Average daily volume can't tell a 0.1%-uptime
                         # ghost (3rd age range top) from a 44%-uptime winner (Uncharged trident) -- this
                         # can. The real reason orders "never go through" was thin uptime, not bad timing.
MIN_SELL_UPTIME = 0.12   # a buy must also be EXITABLE: the SELL side (buyers present) must trade >= this
                         # often, else you get stuck holding it. The -4.8M loss was un-exitable (sell-uptime
                         # ~0.07), not merely large -- this gate refuses buys you couldn't get back out of.
MAX_UNWIND_DAYS = 5.0    # size a buy so the WHOLE position could realistically sell within ~this many days
                         # at the item's true sell-side volume. Raised 3->5 for bigger, more concentrated
                         # positions — still bounded (you can always sell out in ~5 days), the guardrail that
                         # separates an aggressive big LIQUID bet from an un-exitable one (the -4.8M Amulet).
PRICE_EDGE = 0.10        # balanced nudge: give up ~10% of the spread per side to win the queue
HOLD_MIN = 50.0          # recovery score >= this -> hold an underwater position
CUT_MAX = 35.0           # recovery score < this -> cut it
# Stale-capital recycler: a HOLD that's been parked a long time making no real progress is dead money —
# the gp would compound faster in a liquid flip. Escalate it to CUT (cut & redeploy). Live analysis
# (2026-06-27): ~78% of net worth sat in holds, several flat for days, dragging the growth rate.
STALE_DAYS = 3.0         # held longer than this with no progress -> recycle the capital
STALE_FLAT_MAX = 0.02    # "no progress" = unrealized under +2% (flat or underwater, not actually winning)
STALE_RECOVERY_OK = 70.0 # a STRONGLY-recovering position (rec >= this) earns more patience — not recycled
# Margin-mirage guards for the plan's BUYS: a real, capturable flip has a modest spread, a margin
# that actually persists, and one that isn't a wild multiple of the item's own norm. Anything else
# is a stale/illiquid ghost quote (one side of the spread hasn't traded) -> don't recommend it.
MAX_SPREAD_FRAC = 0.12     # raw bid-ask spread above ~12% of price = stale/illiquid, not capturable
MIN_MARGIN_UPTIME = 0.45   # the margin must hold at least this fraction of the last 7 days to be real
MARGIN_SANITY_MULT = 3.0   # skip a margin more than this x the item's own 7d-typical (transient blowout)
# ...BUT fill-frequency is DIRECT proof of liquidity. Where an item clearly trades on BOTH sides, that
# evidence overrides the indirect spread / margin-persistence proxies above (which false-positived on
# genuine high-ROI flips like Hydra bones @94% fill or Raw dashing kebbit @53%). A wide, persistent
# spread on a heavily-traded item is a real edge, not a ghost. Only a loosened glitch backstop remains.
LIQ_PROOF_BUY = 0.30       # buy-side uptime that "proves" the item trades (then waive spread + m-uptime)
LIQ_PROOF_SELL = 0.15      # and it must trade on the sell side too, or you could never exit
LIQ_SANITY_MULT = 12.0     # a PROVEN-liquid item (trades both sides) whose margin widened above its 7d
                           # norm is a real opportunity, not a stale ghost — only block a true glitch
                           # (>12x median). Raised 6->12 to stop dropping big, tight-spread, heavily-traded
                           # flips (Berserker necklace etc.) that were leaving bankroll idle. Non-proven
                           # items keep the strict MARGIN_SANITY_MULT=3x (the illiquid-ghost guard).
# Items the GE restricts to ONE per slot per offer (you can't stack the offer). Only bonds.
ONE_PER_SLOT = {"old school bond"}
# Items that are NEVER worth flipping regardless of margin/liquidity — keep them out of BUY recs.
# Old School Bond: confirmed in live play (2026-06-26) that re-trading a bought bond costs ~10% to
# make it tradeable again, which dwarfs any GE margin. (Earlier wiki read said the 10% only applied to
# untradeable->tradeable conversion; real gameplay says otherwise.) Bonds are for membership, not flips.
EXCLUDE_FLIP = {"old school bond"}


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
    slope = _f(sig.get("slope_7d"))
    knife = slope is not None and slope <= -KNIFE_SLOPE_PER_DAY    # steep 7d downtrend, not a dip
    z = _f(sig.get("z_7d"))
    if z is not None and not knife:                     # a falling knife's low z isn't "oversold", it's trend
        adj = max(-2.0, min(2.0, -z)) * 12.0            # oversold (z<0) -> +, stretched high -> -
        score += adj
        if z <= -1.0:
            why.append(f"oversold (z {z:.1f})")
    if knife:
        score -= 15; why.append(f"still falling ({slope * 100:.0f}%/day) — not yet oversold")
    if sig.get("is_value_buy"):
        score += 12; why.append("flagged undervalued")
    vc = _f(sig.get("value_confidence"))
    if vc is not None:
        score += (vc - 50.0) * 0.2
    if sig.get("is_crash"):
        score += 10; why.append("crash-recover setup")
    # alch floor = the strongest validated recovery signal (re-graded crashcond: within 10% of the
    # floor -> 92% recover, PF 24; within 30% -> 75%). Graded bump, was a flat +10.
    alch, cur = _f(sig.get("alch_floor")), _f(cur_price)
    if alch and cur:
        if cur <= alch * 1.10:
            score += 22; why.append("at alch floor (92% recover historically)")
        elif cur <= alch * 1.30:
            score += 11; why.append("near alch floor (downside support)")
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
                "established", "z_7d", "slope_7d", "pct_30d", "alch_floor", "value_confidence", "is_value_buy",
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
        if str(p.get("name", "")).strip().lower() in EXCLUDE_FLIP:
            # bonds: never sell on the GE (re-trading one costs ~10%) — keep it to spend on membership
            action, list_at, reason = "HOLD", None, "use for membership — re-selling a bond costs ~10%, don't flip it"
        elif profitable and (at_fair or to_target < 0.03):
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

        # stale-capital recycler: a HOLD parked past STALE_DAYS that isn't actually winning, and isn't
        # strongly recovering, is dead money -> escalate to CUT so the gp redeploys into a liquid flip.
        held_days = _f(p.get("held_days"))
        stale = bool(
            action == "HOLD" and held_days is not None and held_days >= STALE_DAYS
            and (_f(p.get("unrealized_pct")) or 0.0) < STALE_FLAT_MAX
            and (rec_score is None or rec_score < STALE_RECOVERY_OK)
            and str(p.get("name", "")).strip().lower() not in EXCLUDE_FLIP   # bonds are kept for membership
        )
        if stale:
            action, list_at = "CUT", sell_at
            reason = f"stale capital — held {held_days:.0f}d with no progress; cut & redeploy into a faster flip"

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
            "held_days": held_days, "stale": stale,
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

    # candidate buy universe (flip_ok + positive competitive margin): used to size buys AND to judge
    # whether an existing live buy order is still worth keeping.
    cands = ms[ms["flip_ok"].fillna(False) & (ms["slip_margin"].fillna(-1.0) > 0)] if not ms.empty else ms.iloc[0:0]

    # fill-frequency (trade uptime) for EVERY candidate + held + live order, computed BEFORE the mirage
    # guard so it can override the proxies. buy uptime = how often a seller is present (your buy fills);
    # sell uptime = how often a buyer is present (your sell fills). This is the direct "does it actually
    # trade" signal that avg daily volume can't give.
    cand_ids = [int(x) for x in cands["item_id"].tolist()] if not cands.empty else []
    prof_ids = set(cand_ids) | held_ids | live_buy_ids | live_sell_ids
    prof = fill_uptime(list(prof_ids)) if prof_ids else {}
    for s in sells:  # annotate every holding verdict with how often + how fast it can actually SELL out
        pr = prof.get(s["item_id"], {})
        s["fill_freq"] = round(pr.get("sell", 0.0), 3)
        sud = pr.get("sell_units_day", 0.0)
        s["days_to_liquidate"] = round((s["qty"] / (CAPTURE * sud)), 1) if (sud > 0 and s.get("qty")) else None

    cand_rows: dict[int, tuple] = {}
    mirage = 0
    for r in cands.itertuples(index=False):
        iid = int(r.item_id)
        if str(getattr(r, "name", "")).strip().lower() in EXCLUDE_FLIP:  # bonds etc. — never a real flip
            continue
        buy_at, sell_at = competitive(_f(r.buy_price), _f(r.sell_price))
        if not buy_at or not sell_at:
            continue
        ex = bool(getattr(r, "exempt", False))
        mgn = taxmod.net_sell(int(round(sell_at)), ex) - buy_at
        if mgn <= 0:
            continue
        # mirage guard: drop stale/illiquid ghost spreads (one side hasn't traded). But if fill-freq
        # PROVES the item trades on both sides, trust that over the spread/margin-persistence proxies
        # (a wide spread on a 90%-fill item is a real edge); keep only a loosened glitch backstop.
        pr = prof.get(iid, {})
        proven = pr.get("buy", 0.0) >= LIQ_PROOF_BUY and pr.get("sell", 0.0) >= LIQ_PROOF_SELL
        mid = _f(getattr(r, "mid", None)) or 0.0
        raw_spread = (_f(r.sell_price) or 0.0) - (_f(r.buy_price) or 0.0)
        up = _f(getattr(r, "margin_uptime", None))
        med = _f(getattr(r, "margin_median_7d", None))
        spread_bad = (not proven) and mid > 0 and raw_spread / mid > MAX_SPREAD_FRAC
        uptime_bad = (not proven) and up is not None and up < MIN_MARGIN_UPTIME
        sanity_bad = med and med > 0 and mgn > (LIQ_SANITY_MULT if proven else MARGIN_SANITY_MULT) * med
        if spread_bad or uptime_bad or sanity_bad:
            mirage += 1
            continue
        vol_day = _f(r.vol_daily_7d) or 0.0
        limit = _f(r.buy_limit) or (vol_day / 12.0)
        cand_rows[iid] = (mgn, buy_at, sell_at, ex, vol_day, limit, r)
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
                bu = prof.get(iid, {}).get("buy", 0.0)
                if iid in good_buy_ids and bu >= MIN_BUY_UPTIME:
                    status, note = "keep", f"still a good buy — keep filling ({prog})"
                    kept_buy_ids.add(iid)
                    kept_buy_info[iid] = (int(o.price or 0), int(o.total_qty or 0), int(o.filled_qty or 0))
                elif iid in good_buy_ids:  # still has a margin, but it barely trades — it'll just sit
                    status, note = "cancel", f"rarely trades — a seller appears only ~{bu * 100:.0f}% of the time; cancel & redeploy"
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
    slow_skip = 0
    thin_skip = 0
    exit_skip = 0
    if free_slots > 0 and good_buy_ids:
        # Rank by TOTAL deployable round-trip profit (margin x the units this slot can realistically size),
        # tilted by liquidity — so idle bankroll flows into the bigger liquid flips instead of small
        # high-per-unit-margin singles that leave capital idle. The quality bars (uptime/sell/ROI/profit/
        # exit gates) are unchanged; this only reorders WHICH qualifying buys fill the slots.
        def _score(kv):  # LIVE ranking: total deployable profit x liquidity credit
            iid_, (mgn, buy_at, sell_at, ex, vol_day, limit, r) = kv
            up = prof.get(iid_, {}).get("buy", 0.0)                         # buy-side trade frequency
            liq = 0.25 + 0.75 * min(1.0, up / 0.5)                          # full liquidity credit at >=50% uptime
            pcap = (per_slot_cap // buy_at) if (per_slot_cap > 0 and buy_at > 0) else float("inf")
            units = max(1.0, min(size_for_timeline(vol_day), limit, pcap, offer_cap(r.name)))
            return mgn * units * liq                                        # margin x sized units = gp/round-trip

        def _ev(iid_, mgn, buy_at, vol_day, limit, r):
            # capital-normalized, TWO-SIDED expected value (gp per gp-of-capital per day): two-sided fill
            # probability x return-per-capital (tax+slippage netted) x daily turnover. Logged for A/B vs
            # _score; once it proves out on fills+profit we flip the live ranking to this.
            pr = prof.get(iid_, {})
            pfill = pr.get("buy", 0.0) * pr.get("sell", 0.0)               # both legs must trade
            slip_roi = _f(getattr(r, "slip_roi", None))
            if slip_roi is None:
                slip_roi = (mgn / buy_at) if buy_at else 0.0
            size = max(size_for_timeline(vol_day), 1.0)
            turnover = timeline(size, vol_day, limit)[2] / size            # fraction of the position cycled/day
            return pfill * max(slip_roi, 0.0) * max(turnover, 0.0)

        ranked = sorted(cand_rows.items(), key=_score, reverse=True)
        for iid, (mgn, buy_at, sell_at, ex, vol_day, limit, r) in ranked:
            if len(new_buys) >= free_slots:
                break
            if iid in excl or remaining < buy_at:
                continue
            pr = prof.get(iid, {})
            if pr.get("buy", 0.0) < MIN_BUY_UPTIME:                         # too rarely traded -> it'll just sit
                thin_skip += 1
                continue
            if pr.get("sell", 0.0) < MIN_SELL_UPTIME:                       # un-EXITABLE -> you'd get stuck holding it
                exit_skip += 1
                continue
            if buy_at > 0 and (mgn / buy_at) < max(float(th.min_roi or 0), MIN_ROI_FLOOR):  # clear the (post-nudge) ROI floor — beats the 2% tax
                continue
            sell_ud = pr.get("sell_units_day", 0.0)
            exit_cap = (CAPTURE * sell_ud * MAX_UNWIND_DAYS) if sell_ud > 0 else 0  # size so you can SELL it back out
            cap_units = (per_slot_cap // buy_at) if per_slot_cap > 0 else size_for_timeline(vol_day)
            units = int(min(size_for_timeline(vol_day), limit, cap_units, remaining // buy_at, offer_cap(r.name), exit_cap))
            if units < 1:
                continue
            if timeline(units, vol_day, limit)[0] > MAX_FILL_H:             # too illiquid -> would sit unfilled
                slow_skip += 1
                continue
            if mgn * units < float(th.min_rt_profit or 0):                  # round-trip profit bar
                continue
            row = _buy_row(r, iid, units, buy_at, sell_at, ex, vol_day, limit, live=False)
            row["fill_freq"] = round(pr.get("buy", 0.0), 3)
            row["sell_freq"] = round(pr.get("sell", 0.0), 3)
            row["days_to_liquidate"] = round(units / (CAPTURE * sell_ud), 1) if sell_ud > 0 else None
            row["ev_score"] = round(_ev(iid, mgn, buy_at, vol_day, limit, r), 6)
            new_buys.append(row)
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
        row["fill_freq"] = round(prof.get(iid, {}).get("buy", 0.0), 3)
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
            if str(h.get("name", "")).strip().lower() in EXCLUDE_FLIP:  # never list a bond for sale (10% re-trade)
                continue
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

    # attach each slotted item's best UTC hours to place (when its side of the book is busiest)
    ph = peak_hours([s["item_id"] for s in slots]) if slots else {}
    for s in slots:
        hrs = ph.get(s["item_id"])
        if hrs:
            s["best_hours"] = hrs

    # OVERNIGHT PICKS — the only OOS-PROVEN signal family (+7.5% median/night, 78% win). Surfaced in the
    # main plan but kept SEPARATE from the 8 flip/sell slots: different timing (place in the evening, sell
    # next morning), so they don't compete for "right now" slots. Affordable, not already held/on order.
    overnight = []
    try:
        for o in overnight_table(th, d=ms, limit=12):
            iid = int(o.get("item_id"))
            if iid in held_ids or iid in live_buy_ids or (o.get("on_buy") or 0) > free_gp:
                continue
            overnight.append({
                "item_id": iid, "name": o.get("name"),
                "buy": round(o.get("on_buy") or 0), "target": round(o.get("on_target") or 0),
                "margin": round(o.get("on_margin") or 0), "roi": o.get("on_roi"),
                "fill_prob": o.get("on_fill_prob"), "win_rate": o.get("on_win_rate"),
                "units": int(o.get("on_units") or 0), "ev": round(o.get("on_ev") or 0),
            })
            if len(overnight) >= 5:
                break
    except Exception:  # noqa: BLE001 — never let the overnight add-on break the core plan
        overnight = []

    expected_realized = sum(s["expected_net"] for s in active_sells if s["expected_net"])
    plan_gp_day = sum(b.get("gp_day") or 0 for b in buys)
    n_stale = sum(1 for s in sells if s.get("stale"))
    stale_capital = sum((s.get("avg_cost") or 0) * (s.get("qty") or 0) for s in sells if s.get("stale"))
    return {
        "free_gp": round(free_gp), "committed_capital": round(committed),
        "holdings_value": round(holdings_value), "net_worth": round(net_worth),
        "capital_in": round(cap0),
        # portfolio totals (so the API can auto-snapshot the growth curve on every plan view)
        "realized_total": round(float(port.get("realized_total") or 0.0)),
        "unrealized_total": round(float(port.get("unrealized_total") or 0.0)),
        "invested": round(float(port.get("invested") or 0.0)),
        "free_slots": int(max(0, 8 - len(slots))), "slots_used": int(min(8, len(slots))),
        "n_positions": len(positions), "n_active_sells": len(active_sells),
        "n_holding": len(holding), "n_buys": len(buys), "n_listed": len(listed),
        "mirage_skipped": int(mirage), "slow_skipped": int(slow_skip), "thin_skipped": int(thin_skip),
        "exit_skipped": int(exit_skip), "n_stale": int(n_stale), "stale_capital": round(stale_capital),
        "liquidity_clock": market_clock(), "overnight": overnight,
        "slots": slots, "holding": holding, "reconcile": reconcile,
        "totals": {
            "expected_realized": round(expected_realized),
            "buy_capital": round(sum(b.get("capital") or 0 for b in buys)),
            "plan_gp_day": round(plan_gp_day),
            "growth_day": round(plan_gp_day / net_worth, 4) if net_worth > 0 else None,
        },
    }

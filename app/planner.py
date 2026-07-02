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

import time

import pandas as pd

from . import portfolio as pf
from . import tax as taxmod
from .db import connect, connect_trades, get_free_gp, get_orders_df, get_signal_outcomes_df, utcnow
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
# Post-mortem guards (2026-07-01). Four days of live losses traced to three failure modes:
# (a) corrupted free_gp (343M vs ~77M real) oversized single-item bets (8x Dinh's bulwark = 107M rec, -9.5M);
# (b) the proven-liquidity waiver passed a 32.8% "margin" mirage on a 6.4M item (Blood moon chestplate, -5.7M);
# (c) flip-buys at the top of a +77%/2d spike and into intraday knives (Searing page, Seeking dragon arrow).
# Live scoreboard: <6h flips = 94% win, 1.27M gp/slot-day; >5M items = net NEGATIVE; >1d holds = 6-13% win.
TRUST_MULT = 1.5         # sizing cash <= this x median of the last 5 daily bankroll snapshots (corruption clamp;
                         # snapshots record the RAW value, so genuine growth keeps raising the ceiling)
MAX_ITEM_FRAC = 0.20     # hard cap: ONE item <= this fraction of net worth, whatever the slot cap says
BIG_TICKET = 5_000_000   # >= this price, the proven-liquidity waiver does NOT apply — the >5M band is net-
                         # negative live; thin books at high prices fool the uptime proof
MAX_MARGIN_FRAC = 0.15   # modeled margin > 15% of price on a >=100K item = too-good-to-be-true -> mirage
Z_SPIKE_MAX = 2.0        # don't flip-buy a blowoff top (price > +2 sigma vs its own 7d)
CHG24_SPIKE = 0.25       # don't chase a +25%/24h pump (it mean-reverts on you)
CHG24_KNIFE = -0.05      # don't flip-buy an intraday knife (-5%/24h) — unless it sits on the alch floor
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


def _trusted_cash(free_gp: float) -> tuple[float, bool]:
    """Clamp the SIZING cash against recent history. free_gp is dead-reckoned from streamed offer
    events, so one accounting slip can inflate it wildly (343M vs ~77M real on 6/30 — every buy that
    day was oversized off the fake number). Median-of-5 daily snapshots is robust to a couple of
    poisoned rows; the snapshots store the RAW value, so genuine growth raises the ceiling itself."""
    try:
        con = connect_trades(read_only=True)
        try:
            rows = con.execute(
                "SELECT bankroll FROM net_worth_log WHERE bankroll IS NOT NULL AND bankroll > 0 ORDER BY day DESC LIMIT 5"
            ).fetchall()
            man = con.execute("SELECT value FROM settings WHERE key = 'free_gp_manual'").fetchone()
            mts = con.execute("SELECT value FROM settings WHERE key = 'free_gp_manual_ts'").fetchone()
        finally:
            con.close()
    except Exception:
        return free_gp, False
    vals = sorted(float(r[0]) for r in rows)
    if len(vals) < 3:
        return free_gp, False
    cap = TRUST_MULT * vals[len(vals) // 2]
    # a RECENT manual re-baseline is the user stating ground truth — trust it over the trailing
    # median (which lags several days behind a deliberate correction).
    if man and man[0] is not None and mts and mts[0] is not None and (time.time() - float(mts[0])) < 3 * 86400:
        cap = max(cap, float(man[0]))
    return (min(free_gp, cap), free_gp > cap)


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


def _overnight_roster() -> dict[int, tuple[int, float]]:
    """Graded overnight history per item (n graded, win rate) from signal_outcomes — the repeat-
    winner roster. Within qualified overnight picks our score adds nothing (every score quartile
    wins 68-83%), but items with 3+ graded cycles at 80-100% win (Spirit shield, Kovac's grog,
    Karil's coif...) are a real, persistent pattern — unfashionable mid-price items the crowd's
    margin-scanner tools never rank. Boost those; fade proven losers."""
    try:
        so = get_signal_outcomes_df()
        if so is None or so.empty:
            return {}
        ov = so[so["kind"] == "overnight"]
        if ov.empty:
            return {}
        g = ov.groupby("item_id")["win"].agg(["size", "mean"])
        return {int(i): (int(r["size"]), float(r["mean"])) for i, r in g.iterrows()}
    except Exception:  # noqa: BLE001 — roster is a bonus signal, never break the plan
        return {}


def build_plan(th: Thresholds | None = None, con=None, mode: str = "active") -> dict:
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
                "is_crash", "post_update_drop", "net_margin", "slip_margin", "exempt", "flip_ok", "clear_sell"]
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
        # fill-realistic exits: SELL/CUT rows use this price and need to actually TRADE — undercut to
        # the trailing 45m clearing print when the passive ask isn't where buyers are paying (a CUT
        # that sits at a never-printing ask just keeps decaying; sells crossing the clearing VWAP
        # filled 58% vs 30%). HOLD/LIST keep patient fair-value targets.
        cl_s = _f(s.get("clear_sell"))
        if sell_at and cl_s and cl_s < sell_at:
            sell_at = float(max(round(cl_s) - 1, (_f(bid) or 0) + 1))
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
    free_gp_raw = max(0.0, float(_fg if _fg is not None else th.bankroll))  # fall back to the filter until set
    free_gp, cash_clamped = _trusted_cash(free_gp_raw)           # corruption guard: never SIZE off an inflated number
    holdings_value = float(port.get("invested") or 0.0) + float(port.get("unrealized_total") or 0.0)
    net_worth_raw = free_gp_raw + committed + holdings_value     # honest picture (reported + growth snapshots)
    net_worth = free_gp + committed + holdings_value             # sizing picture (clamped cash)
    cap0 = free_gp                                               # capital available for NEW buys right now
    per_slot_cap = (float(th.max_alloc_frac or 0) * net_worth) if net_worth > 0 else 0.0  # diversify vs TOTAL worth
    item_cap_gp = MAX_ITEM_FRAC * net_worth                      # hard single-item ceiling (the 107M-Dinh's guard)
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
    mirage = spikes = knives = 0
    for r in cands.itertuples(index=False):
        iid = int(r.item_id)
        if str(getattr(r, "name", "")).strip().lower() in EXCLUDE_FLIP:  # bonds etc. — never a real flip
            continue
        buy_at, sell_at = competitive(_f(r.buy_price), _f(r.sell_price))
        if not buy_at or not sell_at:
            continue
        # fill-realistic pricing: place at the price that's actually PRINTING, not the passive quote.
        # Calibrated on our own orders: 63% of buys sat at/below the bid and only 21% filled; offers
        # crossing the trailing 45m clearing VWAP filled ~2x as often (buys 26%->50%, sells 30%->58%).
        # The margin is then computed from the CAPTURABLE prices — flips whose "margin" only exists at
        # prices that never print get killed by the ROI floor instead of wasting a slot for 30 minutes.
        bid, ask = _f(r.buy_price) or 0.0, _f(r.sell_price) or 0.0
        cl_b, cl_s = _f(getattr(r, "clear_buy", None)), _f(getattr(r, "clear_sell", None))
        if cl_b and cl_b > buy_at:
            buy_at = float(min(round(cl_b) + 1, ask - 1)) if ask > 0 else float(round(cl_b) + 1)
        if cl_s and cl_s < sell_at:
            sell_at = float(max(round(cl_s) - 1, bid + 1)) if bid > 0 else float(round(cl_s) - 1)
        if sell_at <= buy_at:
            continue
        ex = bool(getattr(r, "exempt", False))
        mgn = taxmod.net_sell(int(round(sell_at)), ex) - buy_at
        if mgn <= 0:
            continue
        # mirage guard: drop stale/illiquid ghost spreads (one side hasn't traded). But if fill-freq
        # PROVES the item trades on both sides, trust that over the spread/margin-persistence proxies
        # (a wide spread on a 90%-fill item is a real edge); keep only a loosened glitch backstop.
        # EXCEPT on big-ticket items: the uptime proof is fooled by thin books at high prices (the
        # >5M band is net-negative live; Blood moon chestplate passed here with a 32.8% "margin").
        pr = prof.get(iid, {})
        proven = (buy_at < BIG_TICKET
                  and pr.get("buy", 0.0) >= LIQ_PROOF_BUY and pr.get("sell", 0.0) >= LIQ_PROOF_SELL)
        mid = _f(getattr(r, "mid", None)) or 0.0
        raw_spread = (_f(r.sell_price) or 0.0) - (_f(r.buy_price) or 0.0)
        up = _f(getattr(r, "margin_uptime", None))
        med = _f(getattr(r, "margin_median_7d", None))
        spread_bad = (not proven) and mid > 0 and raw_spread / mid > MAX_SPREAD_FRAC
        uptime_bad = (not proven) and up is not None and up < MIN_MARGIN_UPTIME
        sanity_bad = med and med > 0 and mgn > (LIQ_SANITY_MULT if proven else MARGIN_SANITY_MULT) * med
        toogood_bad = buy_at >= 100_000 and (mgn / buy_at) > MAX_MARGIN_FRAC   # 30%+ "margins" are mirages
        if spread_bad or uptime_bad or sanity_bad or toogood_bad:
            mirage += 1
            continue
        # momentum gates: don't buy blowoff tops (Searing page: rec'd at the top of +77%/2d, -4.9M
        # unrealized) and don't catch intraday knives the 7d-slope guard is too slow to see.
        z = _f(getattr(r, "z_7d", None))
        c24 = _f(getattr(r, "chg_24h", None))
        af = _f(getattr(r, "alch_floor", None))
        at_floor = af is not None and buy_at <= af * 1.10        # alch floor = 92% historical recovery
        if (z is not None and z > Z_SPIKE_MAX) or (c24 is not None and c24 > CHG24_SPIKE):
            spikes += 1
            continue
        if c24 is not None and c24 < CHG24_KNIFE and not at_floor:
            knives += 1
            continue
        vol_day = _f(r.vol_daily_7d) or 0.0
        limit = _f(r.buy_limit) or (vol_day / 12.0)
        cand_rows[iid] = (mgn, buy_at, sell_at, ex, vol_day, limit, r)
    good_buy_ids = set(cand_rows)

    # tonight's overnight candidates — needed DURING reconcile (a live lowball must be judged as
    # an overnight order, not a fast flip) and reused by the 2-touch allocator below.
    omap: dict[int, dict] = {}
    if mode == "2touch":
        try:
            omap = {int(oc.get("item_id") or 0): oc for oc in overnight_table(th, d=ms, limit=40)}
        except Exception:  # noqa: BLE001
            omap = {}

    # reconcile each live order against the plan: keep / reprice / cancel
    reconcile = []
    kept_buy_ids = set()
    kept_buy_info: dict[int, tuple] = {}   # iid -> (price, total_qty, filled_qty) of the actual order
    kept_on_ids = set()                    # live OVERNIGHT lowballs we're keeping (occupy slots too)
    kept_on_info: dict[int, tuple] = {}    # iid -> (price, total, filled, tonight's candidate row or None)
    now_ts = pd.Timestamp(utcnow())
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
                bid_now = _f(sig.get(iid, {}).get("buy_price")) or 0.0
                opened = getattr(o, "opened_ts", None)
                age_h = ((now_ts - pd.Timestamp(opened)).total_seconds() / 3600.0) if opened is not None and pd.notna(opened) else None
                oc = omap.get(iid)
                # A LOWBALL (priced at/below the current bid) in 2-touch mode is the overnight
                # strategy working as designed — it's SUPPOSED to sit unfilled unless a dip hits
                # it. Judging it with fast-flip logic created a place -> "cancel" -> re-recommend
                # loop. Lifecycle instead: keep overnight; reprice if tonight's optimal lowball
                # moved; cancel only once the overnight window has passed without a fill.
                # market slid BELOW the lowball: the offer is now at/above the bid and would fill
                # at a stale (no-longer-discounted) price — reprice down to tonight's lowball
                if mode == "2touch" and price > 0 and bid_now > 0 and price > bid_now * 1.005 and oc:
                    ob2 = _f(oc.get("on_buy"))
                    if ob2:
                        reconcile.append({"order_id": getattr(o, "order_id", None), "item_id": iid, "name": nm,
                                          "side": side, "price": price, "progress": prog, "status": "reprice",
                                          "note": f"market moved below your offer — reprice to tonight's lowball ~{ob2:,.0f}"})
                        kept_on_ids.add(iid)
                        kept_on_info[iid] = (price, int(o.total_qty or 0), int(o.filled_qty or 0), oc)
                        continue
                # 0.5% tolerance: the bid drifts tick-to-tick after placement; in 2-touch there are
                # no fast-flip BUY recs, so anything at/near the bid is a lowball by construction
                if mode == "2touch" and price > 0 and bid_now > 0 and price <= bid_now * 1.005:
                    ob = _f(oc.get("on_buy")) if oc else None
                    if age_h is not None and age_h > 14:
                        status, note = "cancel", f"lowball didn't fill overnight ({prog} after {age_h:.0f}h) — cancel & redeploy the cash"
                    elif ob and abs(price - ob) / max(ob, 1.0) > 0.03:
                        status, note = "reprice", f"reprice to ~{ob:,.0f} — tonight's optimal lowball moved with the bid"
                        kept_on_ids.add(iid)
                        kept_on_info[iid] = (price, int(o.total_qty or 0), int(o.filled_qty or 0), oc)
                    else:
                        status, note = "keep", "overnight lowball working — fills only if price dips; check it at your morning session"
                        kept_on_ids.add(iid)
                        kept_on_info[iid] = (price, int(o.total_qty or 0), int(o.filled_qty or 0), oc)
                    reconcile.append({"order_id": getattr(o, "order_id", None), "item_id": iid, "name": nm,
                                      "side": side, "price": price, "progress": prog, "status": status, "note": note})
                    continue
                # the item being a good buy at the COMPETITIVE price isn't enough — the margin must
                # also survive at YOUR standing order's price, or "keep filling" locks in a loss
                # (the flaw showed as a negative Buys-gp/day tile on a kept order).
                order_mgn = 0.0
                if iid in good_buy_ids and price > 0:
                    _m, _b, c_sell, c_ex, *_rest = cand_rows[iid]
                    order_mgn = taxmod.net_sell(int(round(c_sell)), c_ex) - price
                if iid in good_buy_ids and bu >= MIN_BUY_UPTIME and order_mgn > 0:
                    status, note = "keep", f"still a good buy — keep filling ({prog})"
                    kept_buy_ids.add(iid)
                    kept_buy_info[iid] = (int(o.price or 0), int(o.total_qty or 0), int(o.filled_qty or 0))
                elif iid in good_buy_ids and order_mgn <= 0:
                    status, note = "cancel", "your buy price no longer clears a profit after tax — cancel & re-place lower"
                elif iid in good_buy_ids:  # still has a margin, but it barely trades — it'll just sit
                    status, note = "cancel", f"rarely trades — a seller appears only ~{bu * 100:.0f}% of the time; cancel & redeploy"
                else:
                    status, note = "cancel", "no longer a good buy — cancel & redeploy"
            reconcile.append({"order_id": getattr(o, "order_id", None), "item_id": iid, "name": nm,
                              "side": side, "price": price, "progress": prog, "status": status, "note": note})

    # ---- 3) fill the free slots with new buys ----------------------------------------------
    slots_used_existing = len(active_sells) + len(kept_buy_ids) + len(kept_on_ids)
    free_slots = max(0, 8 - slots_used_existing)
    excl = held_ids | live_buy_ids   # don't re-recommend held items or items already on a live buy
    new_buys = []
    remaining = cap0
    slow_skip = 0
    thin_skip = 0
    exit_skip = 0
    small_skip = 0
    if mode == "2touch" and free_slots > 0:
        # ---- 2-TOUCH MODE: overnight-first allocation --------------------------------------
        # The whole free-slot budget goes to the one OOS-PROVEN edge (78% win, +7% median/night,
        # stable across 3 graded weeks) instead of presence-required fast flips. Rationale: with
        # ~2h/day at the keyboard, fast flips punish absence (every big hit happened while away);
        # the overnight premium exists BECAUSE you're asleep — actives can't harvest it without
        # also sleeping on inventory. Evening: place these before bed. Morning: collect + list.
        roster = _overnight_roster()
        ocands = list(omap.values())   # computed above (deep pool; the per-slot profit bar culls it)
        oids = [int(o.get("item_id") or 0) for o in ocands]
        oprof = fill_uptime([i for i in oids if i and i not in excl]) if oids else {}
        scored = []
        for o in ocands:
            iid = int(o.get("item_id") or 0)
            on_buy = float(o.get("on_buy") or 0)
            if not iid or iid in excl or on_buy <= 0:
                continue
            pr = oprof.get(iid, {})
            if pr.get("sell", 0.0) < MIN_SELL_UPTIME:      # must be exitable the next DAY, not just fillable at night
                exit_skip += 1
                continue
            n_gr, win_gr = roster.get(iid, (0, 0.0))
            boost = 1.5 if (n_gr >= 3 and win_gr >= 0.75) else (0.5 if (n_gr >= 3 and win_gr <= 0.45) else 1.0)
            sell_ud = pr.get("sell_units_day", 0.0)
            # overnight thesis = out the NEXT day: size so the position can sell through in ~2 days max
            exit_cap = (CAPTURE * sell_ud * 2.0) if sell_ud > 0 else 0.0
            icap = (item_cap_gp // on_buy) if item_cap_gp > 0 else float("inf")
            pcap = (per_slot_cap // on_buy) if per_slot_cap > 0 else float("inf")
            units_max = min(float(o.get("on_units") or 0), icap, pcap, exit_cap, offer_cap(o.get("name")))
            if units_max < 1:
                continue
            ev_unit = float(o.get("on_ev") or 0)           # margin x fill-prob x win-rate, per unit
            scored.append((ev_unit * boost * units_max, boost, units_max, o, pr, n_gr, win_gr))
        scored.sort(key=lambda x: x[0], reverse=True)
        for _exp, boost, units_max, o, pr, n_gr, win_gr in scored:
            if len(new_buys) >= free_slots or remaining <= 0:
                break
            iid = int(o["item_id"])
            on_buy = float(o.get("on_buy") or 0)
            units = int(min(units_max, remaining // on_buy))
            if units < 1:
                continue
            margin = float(o.get("on_margin") or 0)
            fp = float(o.get("on_fill_prob") or 0)
            wr = float(o.get("on_win_rate") or 0)
            # per-SLOT profit bar (same bar as the fast-flip loop): a filled night must pay at
            # least min_rt_profit, or the pick is a scrap that wastes one of 8 slots — an EMPTY
            # slot (capital free in the morning) beats a 15K/night position. Thin-sell items
            # size down to a handful of units via exit_cap and die here; that's the point.
            if margin * units < float(th.min_rt_profit or 0):
                small_skip += 1
                continue
            odisc = _f(o.get("on_disc"))
            why = (f"overnight lowball {odisc:.0%} below bid — " if odisc else "overnight lowball — ") \
                + f"fills {fp:.0%} of nights, wins {wr:.0%} when filled"
            if boost > 1.0:
                why += f"; PROVEN roster ({n_gr} graded, {win_gr:.0%} win)"
            elif boost < 1.0:
                why += f"; caution: graded {win_gr:.0%} win over {n_gr}"
            sud = pr.get("sell_units_day", 0.0)
            new_buys.append({
                "action": "BUY", "item_id": iid, "name": o.get("name"),
                "price": round(on_buy), "sell_target": round(float(o.get("on_target") or 0)),
                "units": units, "capital": round(units * on_buy), "margin": round(margin),
                "gp_day": round(margin * units * fp * wr),   # per-NIGHT expected gp, odds included
                "buy_h": None, "roundtrip_h": None,
                "reason": why, "live": False, "overnight": True,
                "fill_freq": round(fp, 3), "sell_freq": round(pr.get("sell", 0.0), 3),
                "days_to_liquidate": round(units / (CAPTURE * sud), 1) if sud > 0 else None,
            })
            remaining -= units * on_buy
    elif free_slots > 0 and good_buy_ids:
        # Rank by modeled gp PER DAY (margin x the units you actually CYCLE per day), tilted by liquidity.
        # This is the true profit-rate objective: it rewards both deploying capital AND recycling it fast,
        # so a big slow position no longer beats a slightly-smaller one that round-trips 3x as often. The
        # quality bars (uptime/sell/ROI/profit/exit gates) are unchanged; this only reorders the slots.
        def _fair_tilt(r):
            # Validated (graded flips): a flip BELOW its 7d fair value wins 59% and COMPLETES its sell 2x
            # more (reached 39% vs 19%) — the price drifts UP toward your ask. Above fair, it reverts down
            # and the sell sticks. So favor below-fair, mildly deprioritise significantly-above-fair. The
            # effect is a STEP not a gradient (below-fair good, near/above both meh), so use bands.
            disc = _f(getattr(r, "value_discount", None))   # +ve = below fair value
            if disc is None:
                return 1.0
            if disc >= 0.05:
                return 1.4                                   # >=5% below fair: wins 59%, completes 39%
            if disc > 0.0:
                return 1.15
            if disc > -0.05:
                return 1.0                                   # near fair
            if disc > -0.12:
                return 0.55                                  # 5-12% above fair: completes only ~19% -> stuck-sell
            return 0.4                                       # >=12% above fair: significantly stretched, avoid

        def _score(kv):  # LIVE ranking: modeled gp/DAY (margin x daily turnover) x liquidity x fair-value tilt
            iid_, (mgn, buy_at, sell_at, ex, vol_day, limit, r) = kv
            up = prof.get(iid_, {}).get("buy", 0.0)                         # buy-side trade frequency
            liq = 0.25 + 0.75 * min(1.0, up / 0.5)                          # full liquidity credit at >=50% uptime
            gp_cap = min(per_slot_cap, item_cap_gp) if per_slot_cap > 0 else item_cap_gp
            pcap = (gp_cap // buy_at) if (gp_cap > 0 and buy_at > 0) else float("inf")
            units = max(1.0, min(size_for_timeline(vol_day), limit, pcap, offer_cap(r.name)))
            daily_units = timeline(units, vol_day, limit)[2]               # units realistically cycled per day
            return mgn * daily_units * liq * _fair_tilt(r)                 # gp/day, tilted toward below-fair flips

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
            icap = (item_cap_gp // buy_at) if (item_cap_gp > 0 and buy_at > 0) else float("inf")  # one item <= 20% of NW
            units = int(min(size_for_timeline(vol_day), limit, cap_units, remaining // buy_at, offer_cap(r.name), exit_cap, icap))
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
        pr = prof.get(iid, {})
        row["fill_freq"] = round(pr.get("buy", 0.0), 3)
        row["sell_freq"] = round(pr.get("sell", 0.0), 3)
        sud = pr.get("sell_units_day", 0.0)
        row["days_to_liquidate"] = round(rem_units / (CAPTURE * sud), 1) if sud > 0 else None
        kept_buy_rows.append(row)

    # live overnight lowballs we're keeping: show them as 🌙 BUY rows at their ACTUAL order price
    # (they occupy slots; the allocator's excl already stops re-recommending the same item)
    for iid, (o_price, o_total, o_filled, oc) in kept_on_info.items():
        rem_units = max(1, o_total - o_filled)
        s_info = sig.get(iid, {})
        tgt = (_f(oc.get("on_target")) if oc else None) or _f(s_info.get("established"))
        ex = bool(s_info.get("exempt"))
        mgn = (taxmod.net_sell(int(round(tgt)), ex) - o_price) if tgt else None
        fp = _f(oc.get("on_fill_prob")) if oc else None
        wr = _f(oc.get("on_win_rate")) if oc else None
        kept_buy_rows.append({
            "action": "BUY", "item_id": iid,
            "name": (oc or {}).get("name") or s_info.get("name") or str(iid),
            "price": round(o_price), "sell_target": round(tgt) if tgt else None,
            "units": int(rem_units), "capital": round(rem_units * o_price),
            "margin": round(mgn) if mgn is not None else None,
            "gp_day": round(mgn * rem_units * fp * wr) if (mgn and fp and wr) else None,
            "buy_h": None, "roundtrip_h": None, "live": True, "overnight": True,
            "reason": f"live overnight lowball — leave it working ({o_filled:,}/{o_total:,} filled)",
            "fill_freq": round(fp, 3) if fp is not None else None,
            "sell_freq": None, "days_to_liquidate": None,
        })

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
        for o in (overnight_table(th, d=ms, limit=12) if mode != "2touch" else []):  # 2touch: they ARE the slots
            iid = int(o.get("item_id"))
            if iid in held_ids or iid in live_buy_ids or (o.get("on_buy") or 0) > free_gp:
                continue
            overnight.append({
                "item_id": iid, "name": o.get("name"),
                "buy": round(o.get("on_buy") or 0), "target": round(o.get("on_target") or 0),
                "margin": round(o.get("on_margin") or 0), "roi": o.get("on_roi"),
                "fill_prob": o.get("on_fill_prob"), "win_rate": o.get("on_win_rate"),
                "units": int(o.get("on_units") or 0), "ev": round(o.get("on_ev") or 0),
                "disc": o.get("on_disc"),
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
        # report the RAW cash picture (the growth snapshot must stay honest so the trusted-cash
        # median self-heals); sizing above used the clamped value when the two disagree.
        "free_gp": round(free_gp_raw), "committed_capital": round(committed),
        "holdings_value": round(holdings_value), "net_worth": round(net_worth_raw),
        "cash_clamped": bool(cash_clamped), "sizing_cash": round(free_gp),
        "capital_in": round(cap0),
        # portfolio totals (so the API can auto-snapshot the growth curve on every plan view)
        "realized_total": round(float(port.get("realized_total") or 0.0)),
        "unrealized_total": round(float(port.get("unrealized_total") or 0.0)),
        "invested": round(float(port.get("invested") or 0.0)),
        "free_slots": int(max(0, 8 - len(slots))), "slots_used": int(min(8, len(slots))),
        "n_positions": len(positions), "n_active_sells": len(active_sells),
        "n_holding": len(holding), "n_buys": len(buys), "n_listed": len(listed),
        "mirage_skipped": int(mirage), "slow_skipped": int(slow_skip), "thin_skipped": int(thin_skip),
        "exit_skipped": int(exit_skip), "spike_skipped": int(spikes), "knife_skipped": int(knives),
        "small_skipped": int(small_skip),
        "n_stale": int(n_stale), "stale_capital": round(stale_capital),
        "liquidity_clock": market_clock(), "overnight": overnight, "mode": mode,
        "slots": slots, "holding": holding, "reconcile": reconcile,
        "totals": {
            "expected_realized": round(expected_realized),
            "buy_capital": round(sum(b.get("capital") or 0 for b in buys)),
            "plan_gp_day": round(plan_gp_day),
            "growth_day": round(plan_gp_day / net_worth, 4) if net_worth > 0 else None,
        },
    }

"""Bankroll growth tracker — the 'are we winning, and how fast' instrument.

Net worth = your liquid cash (bankroll) + holdings at live value. The realized-P&L curve from the
trade log is the real, time-stamped record of how fast trading is compounding the bankroll; from it
we derive a daily growth rate and project the date you cross 1B / 2B / 5B. We also surface the
plan's MODELED forward gp/day (optimistic ceiling) and an idle-capital flag, since undeployed gp is
the silent killer of compounding.
"""
from __future__ import annotations

import math

import pandas as pd

from . import portfolio as pf
from .db import connect, get_net_worth_log_df, record_net_worth
from .planner import build_plan
from .signals import Thresholds

TARGETS = [(5e8, "500M"), (1e9, "1B"), (2e9, "2B"), (5e9, "5B"), (1e10, "10B")]


def _days_to(cur: float, target: float, pct: float | None) -> float | None:
    """Days for `cur` to reach `target` compounding at `pct`/day. None if it never would."""
    if target <= cur:
        return 0.0
    if not pct or pct <= 0:
        return None
    return math.log(target / cur) / math.log(1.0 + pct)


def compute_growth(th: Thresholds | None = None, con=None) -> dict:
    th = th or Thresholds()
    own = con is None
    con = con or connect(read_only=True)
    try:
        port = pf.compute(con)
        plan = build_plan(th, con)
    finally:
        if own:
            con.close()

    free_gp = float(th.bankroll)                                  # th.bankroll is now your FREE deployable gp
    committed = float(plan.get("committed_capital") or 0.0)       # gp locked in open buy offers
    invested = float(port.get("invested") or 0.0)
    unreal = float(port.get("unrealized_total") or 0.0)
    realized_total = float(port.get("realized_total") or 0.0)
    holdings_value = invested + unreal
    net_worth = free_gp + committed + holdings_value              # cash + open buys + inventory at live value
    bankroll = free_gp                                            # (kept name for the snapshot's cash column)
    stats = port.get("stats") or {}

    curve = port.get("equity_curve") or []
    hist: list[dict] = []
    days_active = 0.0
    recent_gp_day = lifetime_gp_day = 0.0
    win_days = 1.0
    if curve:
        ts0, tsN = pd.Timestamp(curve[0]["ts"]), pd.Timestamp(curve[-1]["ts"])
        days_active = max(0.0, (tsN - ts0).total_seconds() / 86400.0)
        # reconstruct a net-worth curve: starting capital + realized profit accrued by each point
        baseline = net_worth - realized_total
        hist = [{"ts": str(p["ts"]), "value": round(baseline + float(p["cum"]))} for p in curve]
        lifetime_gp_day = realized_total / days_active if days_active > 0.5 else realized_total
        cutoff = tsN - pd.Timedelta(days=7)
        prior = [p for p in curve if pd.Timestamp(p["ts"]) <= cutoff]
        cum_prior = float(prior[-1]["cum"]) if prior else 0.0
        win_days = min(7.0, days_active) or 1.0
        recent_gp_day = (realized_total - cum_prior) / win_days

    daily_pct = (recent_gp_day / net_worth) if net_worth > 0 else 0.0
    modeled_gp_day = float(plan["totals"].get("plan_gp_day") or 0.0)
    modeled_pct = (modeled_gp_day / net_worth) if net_worth > 0 else 0.0
    capital_in = float(plan.get("capital_in") or 0.0)            # = free gp deployable now
    idle_frac = (capital_in / net_worth) if net_worth > 0 else 0.0  # free cash as a share of total worth

    # Snapshot today's net worth (once/day) and, once >=2 daily snapshots exist, chart the REAL
    # net-worth curve (which captures unrealized swings) instead of the realized-only reconstruction.
    try:
        record_net_worth(net_worth, bankroll, holdings_value, realized_total, unreal, invested)
    except Exception:  # noqa: BLE001 - snapshot is best-effort, never break the endpoint
        pass
    history_source = "realized"
    nwlog = get_net_worth_log_df()
    if len(nwlog) >= 2:
        nwlog = nwlog.sort_values("day")
        hist = [{"ts": str(r.day), "value": int(r.net_worth)} for r in nwlog.itertuples()]
        history_source = "snapshots"
        last_ts = pd.Timestamp(nwlog["ts"].iloc[-1])
        cutoff = last_ts - pd.Timedelta(days=7)
        prior = nwlog[pd.to_datetime(nwlog["ts"]) <= cutoff]
        ref = prior.iloc[-1] if not prior.empty else nwlog.iloc[0]
        nw_then = float(ref["net_worth"])
        span_d = max(0.5, (last_ts - pd.Timestamp(ref["ts"])).total_seconds() / 86400.0)
        if span_d >= 2.0 and nw_then > 0:   # enough span for a meaningful net-worth growth rate
            daily_pct = (net_worth / nw_then) ** (1.0 / span_d) - 1.0
            recent_gp_day = (net_worth - nw_then) / span_d
            win_days = span_d

    targets = [{
        "label": label, "value": val,
        "days_realized": _days_to(net_worth, val, daily_pct),
        "days_modeled": _days_to(net_worth, val, modeled_pct),
    } for val, label in TARGETS]

    return {
        "bankroll": round(bankroll), "committed": round(committed),
        "holdings_value": round(holdings_value), "net_worth": round(net_worth),
        "realized_total": round(realized_total), "unrealized_total": round(unreal),
        "days_active": round(days_active, 1),
        "lifetime_gp_day": round(lifetime_gp_day), "recent_gp_day": round(recent_gp_day),
        "recent_days": round(win_days, 1),
        "daily_pct": round(daily_pct, 4), "modeled_gp_day": round(modeled_gp_day), "modeled_pct": round(modeled_pct, 4),
        "capital_in": round(capital_in), "idle_frac": round(idle_frac, 3),
        "win_rate": stats.get("win_rate"), "n_closed": stats.get("n_closed"),
        "history": hist, "history_source": history_source, "n_snapshots": int(len(nwlog)),
        "targets": targets,
    }

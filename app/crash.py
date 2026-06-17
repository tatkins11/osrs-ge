"""Crash-&-recover study.

Thesis: a normally-stable item drops sharply BELOW its established level (a
temporary dislocation -- panic dump, event, manipulation), then recovers. Buy
the dip, target recovery back toward the pre-crash level, stop out if it keeps
falling (that means it was a real decline, not a blip).

Signal: established level = trailing median over `ref_window` periods. Enter
when price <= level*(1 - crash_pct) and (optionally) the price was near that
level within the last `recent_k` periods (so the drop is RECENT/sharp, not a
long grind down). Exit on recovery to entry_level*recover_to, a hard stop, or
timeout. Proceeds taxed 2%.

Run on the VPS (uses 6h/24h backfilled history -- swings play out over days):
    python -m app.crash --sweep
    python -m app.crash --timestep 24h --min-price 1000000   # high-value only
    python -m app.crash --crash-pct 0.2 --recent-k 8
"""
from __future__ import annotations

import argparse
import logging

import numpy as np
import pandas as pd

from . import tax as taxmod
from .db import connect, get_items_df

log = logging.getLogger("crash")
WINSOR = 0.50


def _load(con, timestep: str) -> pd.DataFrame:
    return con.execute(
        """
        SELECT item_id, ts, (avg_high + avg_low) / 2.0 AS mid,
               (COALESCE(high_vol, 0) + COALESCE(low_vol, 0)) AS vol
        FROM history
        WHERE timestep = ? AND avg_high IS NOT NULL AND avg_low IS NOT NULL
        ORDER BY item_id, ts
        """,
        [timestep],
    ).df()


def backtest_item(mid: np.ndarray, ref_window: int, crash_pct: float, recover_to: float,
                  stop_pct: float, max_hold: int, recent_k: int, exempt: bool) -> list[dict]:
    n = len(mid)
    if n < ref_window + 5:
        return []
    s = pd.Series(mid, dtype="float64")
    ref = s.rolling(ref_window, min_periods=max(3, ref_window // 2)).median().to_numpy()

    trades: list[dict] = []
    in_pos = False
    entry_i = 0
    entry_p = 0.0
    entry_ref = 0.0
    for i in range(n):
        if np.isnan(ref[i]) or ref[i] <= 0:
            continue
        if not in_pos:
            crashed = mid[i] <= ref[i] * (1 - crash_pct)
            if crashed and recent_k > 0:
                lo = max(0, i - recent_k)
                crashed = np.nanmax(mid[lo:i + 1]) >= ref[i] * 0.97  # was near its level recently -> sharp
            if crashed:
                in_pos, entry_i, entry_p, entry_ref = True, i, mid[i], ref[i]
        else:
            hold = i - entry_i
            reason = ""
            if mid[i] >= entry_ref * recover_to:
                reason = "recovered"
            elif mid[i] <= entry_p * (1 - stop_pct):
                reason = "stop"
            elif hold >= max_hold:
                reason = "timeout"
            if reason and entry_p > 0:
                net = taxmod.net_sell(int(round(mid[i])), exempt) - entry_p
                trades.append({"net": net, "ret": net / entry_p, "hold": hold, "reason": reason})
                in_pos = False
    return trades


def run(timestep: str = "6h", ref_window: int = 28, crash_pct: float = 0.18, recover_to: float = 0.95,
        stop_pct: float = 0.15, max_hold: int = 40, recent_k: int = 0,
        min_gp_vol: float = 50_000_000.0, min_price: float = 1000.0,
        hist: pd.DataFrame | None = None, items: pd.DataFrame | None = None, con=None) -> pd.DataFrame:
    if hist is None or items is None:
        own = con is None
        con = con or connect(read_only=True)
        try:
            hist = _load(con, timestep)
            items = get_items_df(con).set_index("item_id")
        finally:
            if own:
                con.close()
    if hist.empty:
        return pd.DataFrame()

    rows: list[dict] = []
    for iid, g in hist.groupby("item_id"):
        avg_price = g["mid"].mean()
        if avg_price * g["vol"].mean() * 24.0 < min_gp_vol or avg_price < min_price:
            continue
        exempt = bool(items.loc[iid, "exempt"]) if iid in items.index else False
        for t in backtest_item(g["mid"].to_numpy(float), ref_window, crash_pct, recover_to,
                               stop_pct, max_hold, recent_k, exempt):
            t["item_id"] = int(iid)
            t["name"] = items.loc[iid, "name"] if iid in items.index else str(iid)
            rows.append(t)
    return pd.DataFrame(rows)


def summarize(tr: pd.DataFrame, timestep_h: int) -> dict:
    if tr.empty:
        return {"trades": 0}
    wins = tr[tr["net"] > 0]["net"].sum()
    losses = tr[tr["net"] < 0]["net"].sum()
    return {
        "trades": len(tr),
        "items": tr["item_id"].nunique(),
        "win_rate": (tr["net"] > 0).mean(),
        "avg_return": tr["ret"].clip(-WINSOR, WINSOR).mean(),
        "median_return": tr["ret"].median(),
        "median_hold_days": tr["hold"].median() * timestep_h / 24.0,
        "profit_factor": (wins / abs(losses)) if losses < 0 else float("inf"),
        "recovered_pct": (tr["reason"] == "recovered").mean(),
    }


def _pct(x):
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.1f}%"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Crash-&-recover backtest.")
    ap.add_argument("--timestep", default="6h", choices=["6h", "24h", "1h"])
    ap.add_argument("--ref-window", type=int, default=28)
    ap.add_argument("--crash-pct", type=float, default=0.18)
    ap.add_argument("--recover-to", type=float, default=0.95)
    ap.add_argument("--stop-pct", type=float, default=0.15)
    ap.add_argument("--max-hold", type=int, default=40)
    ap.add_argument("--recent-k", type=int, default=0, help="require price near its level within K periods (sharp drop)")
    ap.add_argument("--min-gp-vol", type=float, default=50_000_000.0)
    ap.add_argument("--min-price", type=float, default=1000.0)
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()
    tsh = {"1h": 1, "6h": 6, "24h": 24}[args.timestep]

    con = connect(read_only=True)
    try:
        hist = _load(con, args.timestep)
        items = get_items_df(con).set_index("item_id")
    finally:
        con.close()

    base = dict(timestep=args.timestep, ref_window=args.ref_window, recover_to=args.recover_to,
                stop_pct=args.stop_pct, max_hold=args.max_hold, recent_k=args.recent_k,
                min_gp_vol=args.min_gp_vol, min_price=args.min_price, hist=hist, items=items)
    print(f"Crash-&-recover on {args.timestep} history -- liquid items (>= {args.min_gp_vol:,.0f} gp/day, "
          f"price >= {args.min_price:,.0f}), recover_to {args.recover_to}, stop {args.stop_pct}, "
          f"recent_k {args.recent_k}; returns winsorized +/-{int(WINSOR*100)}%.\n")

    if args.sweep:
        print(f"{'crash%':>8}{'trades':>8}{'items':>7}{'win':>8}{'avg ret':>9}{'median':>9}{'med days':>10}{'recov%':>8}{'PF':>7}")
        for cp in (0.12, 0.18, 0.25, 0.35):
            s = summarize(run(crash_pct=cp, **base), tsh)
            if s["trades"]:
                pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
                print(f"{cp*100:>7.0f}%{s['trades']:>8}{s['items']:>7}{_pct(s['win_rate']):>8}{_pct(s['avg_return']):>9}"
                      f"{_pct(s['median_return']):>9}{s['median_hold_days']:>9.1f}d{_pct(s['recovered_pct']):>8}{pf:>7}")
            else:
                print(f"{cp*100:>7.0f}%{0:>8}")
        return

    tr = run(crash_pct=args.crash_pct, **base)
    s = summarize(tr, tsh)
    if not s["trades"]:
        print("No trades. Loosen filters or collect more history.")
        return
    print(f"crash >= {args.crash_pct*100:.0f}% below level:")
    print(f"  trades        : {s['trades']} across {s['items']} items")
    print(f"  win rate      : {_pct(s['win_rate'])}")
    print(f"  avg return    : {_pct(s['avg_return'])}  median {_pct(s['median_return'])}")
    print(f"  median hold   : {s['median_hold_days']:.1f} days")
    print(f"  recovered     : {_pct(s['recovered_pct'])} of exits")
    print(f"  profit factor : {s['profit_factor']:.2f}")
    print("\n  Top items (>=3 trades, by median return):")
    by = (tr.groupby(["item_id", "name"]).agg(trades=("net", "size"), win=("net", lambda x: (x > 0).mean()),
          med=("ret", "median")).reset_index())
    by = by[by["trades"] >= 3].sort_values("med", ascending=False).head(12)
    for _, r in by.iterrows():
        print(f"    {str(r['name'])[:28]:<29}{int(r['trades']):>4} trades  win {_pct(r['win']):>6}  med {_pct(r['med']):>7}")


if __name__ == "__main__":
    main()

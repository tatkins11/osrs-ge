"""Backtest the mean-reversion strategy on collected hourly history.

Strategy under test: when an item's price falls to <= entry_z standard
deviations below its trailing mean, BUY. Exit when it reverts to the mean, or
after max_hold hours (timeout), or if it keeps falling past stop_z (stop-loss).
Every exit is taxed at the 2% GE rate.

Honesty notes (printed in the report too):
  * Trailing rolling stats only -> no look-ahead bias.
  * Fills assumed at the period mid price -> optimistic; real fills depend on
    volume and the spread, so treat returns as an upper-ish bound.
  * Buy limits are ignored (this measures per-unit edge, not throughput).

Run where history exists (the VPS):
    python -m app.backtest
    python -m app.backtest --entry-z -2.0 --max-hold 48
    python -m app.backtest --sweep
"""
from __future__ import annotations

import argparse
import logging

import numpy as np
import pandas as pd

from . import tax as taxmod
from .db import connect, get_items_df

log = logging.getLogger("backtest")


def _load_history(con) -> pd.DataFrame:
    return con.execute(
        """
        SELECT item_id, ts,
               (avg_high + avg_low) / 2.0 AS mid,
               (COALESCE(high_vol, 0) + COALESCE(low_vol, 0)) AS vol
        FROM history
        WHERE timestep = '1h' AND avg_high IS NOT NULL AND avg_low IS NOT NULL
        ORDER BY item_id, ts
        """
    ).df()


def backtest_item(mid: np.ndarray, window: int, entry_z: float, stop_z: float,
                  max_hold: int, exempt: bool) -> list[dict]:
    n = len(mid)
    if n < window + 10:
        return []
    s = pd.Series(mid, dtype="float64")
    ma = s.rolling(window, min_periods=max(2, window // 2)).mean().to_numpy()
    sd = s.rolling(window, min_periods=max(2, window // 2)).std().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        z = np.where(sd > 0, (mid - ma) / sd, np.nan)

    trades: list[dict] = []
    in_pos = False
    entry_i = 0
    entry_p = 0.0
    for i in range(n):
        if np.isnan(z[i]):
            continue
        if not in_pos:
            if z[i] <= entry_z:
                in_pos, entry_i, entry_p = True, i, mid[i]
        else:
            hold = i - entry_i
            reason = ""
            if mid[i] >= ma[i]:
                reason = "reverted"
            elif z[i] <= stop_z:
                reason = "stop"
            elif hold >= max_hold:
                reason = "timeout"
            if reason and entry_p > 0:
                net = taxmod.net_sell(int(round(mid[i])), exempt) - entry_p
                trades.append({
                    "entry_p": entry_p, "exit_p": mid[i], "net": net,
                    "ret": net / entry_p, "hold": hold, "reason": reason,
                })
                in_pos = False
    return trades


def run(entry_z: float = -1.5, window: int = 168, max_hold: int = 72,
        stop_z: float = -4.0, min_daily_vol: float = 5000.0, con=None) -> pd.DataFrame:
    own = con is None
    con = con or connect(read_only=True)
    try:
        hist = _load_history(con)
        items = get_items_df(con).set_index("item_id")
    finally:
        if own:
            con.close()
    if hist.empty:
        log.warning("no history; run the backfill / collector first")
        return pd.DataFrame()

    rows: list[dict] = []
    for iid, g in hist.groupby("item_id"):
        if g["vol"].mean() * 24 < min_daily_vol:
            continue  # liquidity filter: only items you could actually trade
        exempt = bool(items.loc[iid, "exempt"]) if iid in items.index else False
        for t in backtest_item(g["mid"].to_numpy(float), window, entry_z, stop_z, max_hold, exempt):
            t["item_id"] = int(iid)
            t["name"] = items.loc[iid, "name"] if iid in items.index else str(iid)
            rows.append(t)
    return pd.DataFrame(rows)


def summarize(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"trades": 0}
    wins = tr[tr["net"] > 0]["net"].sum()
    losses = tr[tr["net"] < 0]["net"].sum()
    return {
        "trades": len(tr),
        "items": tr["item_id"].nunique(),
        "win_rate": (tr["net"] > 0).mean(),
        "avg_return": tr["ret"].mean(),
        "median_return": tr["ret"].median(),
        "median_hold_h": tr["hold"].median(),
        "profit_factor": (wins / abs(losses)) if losses < 0 else float("inf"),
        "reverted_pct": (tr["reason"] == "reverted").mean(),
    }


def _pct(x):
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.1f}%"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Backtest the mean-reversion strategy.")
    ap.add_argument("--entry-z", type=float, default=-1.5)
    ap.add_argument("--window", type=int, default=168, help="rolling window in hours (168 = 7d)")
    ap.add_argument("--max-hold", type=int, default=72, help="max hold hours before timeout exit")
    ap.add_argument("--stop-z", type=float, default=-4.0)
    ap.add_argument("--min-daily-vol", type=float, default=5000.0)
    ap.add_argument("--sweep", action="store_true", help="sweep entry-z thresholds")
    args = ap.parse_args()

    print("Mean-reversion backtest (fills @ mid, 2% tax on exit, trailing stats, limits ignored)\n")
    if args.sweep:
        print(f"{'entry z':>8}{'trades':>9}{'items':>7}{'win rate':>10}{'avg ret':>10}{'med hold':>10}{'rev %':>8}")
        for ez in (-1.0, -1.5, -2.0, -2.5, -3.0):
            s = summarize(run(entry_z=ez, window=args.window, max_hold=args.max_hold,
                              stop_z=args.stop_z, min_daily_vol=args.min_daily_vol))
            if s["trades"]:
                print(f"{ez:>8.1f}{s['trades']:>9}{s['items']:>7}{_pct(s['win_rate']):>10}"
                      f"{_pct(s['avg_return']):>10}{s['median_hold_h']:>9.0f}h{_pct(s['reverted_pct']):>8}")
            else:
                print(f"{ez:>8.1f}{0:>9}")
        return

    tr = run(entry_z=args.entry_z, window=args.window, max_hold=args.max_hold,
             stop_z=args.stop_z, min_daily_vol=args.min_daily_vol)
    s = summarize(tr)
    print(f"entry z <= {args.entry_z}, window {args.window}h, max hold {args.max_hold}h, stop z {args.stop_z}\n")
    if not s["trades"]:
        print("No trades (need more history, or loosen the liquidity filter).")
        return
    print(f"  trades        : {s['trades']}  across {s['items']} items")
    print(f"  win rate      : {_pct(s['win_rate'])}")
    print(f"  avg return    : {_pct(s['avg_return'])}  (after 2% tax)")
    print(f"  median return : {_pct(s['median_return'])}")
    print(f"  median hold   : {s['median_hold_h']:.0f}h")
    print(f"  profit factor : {s['profit_factor']:.2f}")
    print(f"  reverted/timeout/stop split by exits that hit the mean: {_pct(s['reverted_pct'])} reverted")

    print("\n  Top mean-reverters (>=3 trades, by avg return):")
    by_item = (tr.groupby(["item_id", "name"])
               .agg(trades=("net", "size"), win_rate=("net", lambda x: (x > 0).mean()),
                    avg_ret=("ret", "mean"), med_hold=("hold", "median"))
               .reset_index())
    by_item = by_item[by_item["trades"] >= 3].sort_values("avg_ret", ascending=False).head(12)
    print(f"    {'item':<26}{'trades':>7}{'win':>7}{'avg ret':>10}{'med hold':>10}")
    for _, r in by_item.iterrows():
        print(f"    {str(r['name'])[:25]:<26}{int(r['trades']):>7}{_pct(r['win_rate']):>7}"
              f"{_pct(r['avg_ret']):>10}{r['med_hold']:>9.0f}h")


if __name__ == "__main__":
    main()

"""Backtest the mean-reversion strategy on collected hourly history.

Strategy under test: when an item's price falls to <= entry_z standard
deviations below its trailing mean, BUY. Exit when it reverts to the mean, or
after max_hold hours (timeout), or if it keeps falling past stop_z (stop-loss).
Every exit is taxed at the 2% GE rate.

Realism filters (this matters — without them the results are a mirage):
  * Liquidity by GP TRADED PER DAY (price x volume), not unit count, so cheap
    junk items (seeds, feathers) that can't absorb real capital are excluded.
  * Minimum price, so sub-threshold rounding noise doesn't masquerade as returns.
  * Per-trade returns winsorized to +/-50% for the average, so a few anomalous
    prints don't dominate. The median is reported uncapped.

Honesty notes:
  * Trailing rolling stats only -> no look-ahead bias.
  * Fills assumed at the period mid price -> optimistic; real fills depend on
    depth, so treat returns as an upper bound.
  * Buy limits ignored (this measures per-unit edge, not throughput).

Run where history exists (the VPS):
    python -m app.backtest
    python -m app.backtest --sweep
    python -m app.backtest --entry-z -2.0 --min-gp-vol 100000000
"""
from __future__ import annotations

import argparse
import logging

import numpy as np
import pandas as pd

from . import tax as taxmod
from .db import connect, get_items_df

log = logging.getLogger("backtest")

WINSOR = 0.50  # cap per-trade return at +/-50% for the average


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
                trades.append({"entry_p": entry_p, "exit_p": mid[i], "net": net,
                               "ret": net / entry_p, "hold": hold, "reason": reason})
                in_pos = False
    return trades


def run(entry_z: float = -1.5, window: int = 168, max_hold: int = 72, stop_z: float = -4.0,
        min_gp_vol: float = 50_000_000.0, min_price: float = 1000.0,
        hist: pd.DataFrame | None = None, items: pd.DataFrame | None = None, con=None) -> pd.DataFrame:
    if hist is None or items is None:
        own = con is None
        con = con or connect(read_only=True)
        try:
            hist = _load_history(con)
            items = get_items_df(con).set_index("item_id")
        finally:
            if own:
                con.close()
    if hist.empty:
        return pd.DataFrame()

    rows: list[dict] = []
    for iid, g in hist.groupby("item_id"):
        avg_price = g["mid"].mean()
        gp_per_day = avg_price * g["vol"].mean() * 24.0
        if gp_per_day < min_gp_vol or avg_price < min_price:
            continue  # only genuinely liquid, non-trivial-price items
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
        "avg_return_wins": tr["ret"].clip(-WINSOR, WINSOR).mean(),
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
    ap.add_argument("--window", type=int, default=168, help="rolling window hours (168 = 7d)")
    ap.add_argument("--max-hold", type=int, default=72)
    ap.add_argument("--stop-z", type=float, default=-4.0)
    ap.add_argument("--min-gp-vol", type=float, default=50_000_000.0, help="min gp traded/day")
    ap.add_argument("--min-price", type=float, default=1000.0)
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    con = connect(read_only=True)
    try:
        hist = _load_history(con)
        items = get_items_df(con).set_index("item_id")
    finally:
        con.close()

    print("Mean-reversion backtest -- liquid items only (gp-vol + min-price filtered, "
          f"returns winsorized +/-{int(WINSOR*100)}% for the average)\n"
          f"filters: >= {args.min_gp_vol:,.0f} gp traded/day, price >= {args.min_price:,.0f}\n")

    if args.sweep:
        print(f"{'entry z':>8}{'trades':>9}{'items':>7}{'win rate':>10}{'avg ret':>9}{'median':>9}{'med hold':>10}{'rev %':>8}")
        for ez in (-1.0, -1.5, -2.0, -2.5, -3.0):
            s = summarize(run(entry_z=ez, window=args.window, max_hold=args.max_hold, stop_z=args.stop_z,
                              min_gp_vol=args.min_gp_vol, min_price=args.min_price, hist=hist, items=items))
            if s["trades"]:
                print(f"{ez:>8.1f}{s['trades']:>9}{s['items']:>7}{_pct(s['win_rate']):>10}"
                      f"{_pct(s['avg_return_wins']):>9}{_pct(s['median_return']):>9}{s['median_hold_h']:>9.0f}h{_pct(s['reverted_pct']):>8}")
            else:
                print(f"{ez:>8.1f}{0:>9}")
        return

    tr = run(entry_z=args.entry_z, window=args.window, max_hold=args.max_hold, stop_z=args.stop_z,
             min_gp_vol=args.min_gp_vol, min_price=args.min_price, hist=hist, items=items)
    s = summarize(tr)
    print(f"entry z <= {args.entry_z}, window {args.window}h, max hold {args.max_hold}h\n")
    if not s["trades"]:
        print("No trades after filtering. Lower --min-gp-vol or --min-price, or collect more history.")
        return
    print(f"  trades             : {s['trades']}  across {s['items']} liquid items")
    print(f"  win rate           : {_pct(s['win_rate'])}")
    print(f"  avg return (wins.) : {_pct(s['avg_return_wins'])}  (after 2% tax, capped +/-{int(WINSOR*100)}%)")
    print(f"  median return      : {_pct(s['median_return'])}")
    print(f"  median hold        : {s['median_hold_h']:.0f}h")
    print(f"  profit factor      : {s['profit_factor']:.2f}")
    print(f"  exits that reverted: {_pct(s['reverted_pct'])}")

    print("\n  Top liquid mean-reverters (>=3 trades, by median return):")
    by_item = (tr.groupby(["item_id", "name"])
               .agg(trades=("net", "size"), win_rate=("net", lambda x: (x > 0).mean()),
                    med_ret=("ret", "median"), med_hold=("hold", "median"))
               .reset_index())
    by_item = by_item[by_item["trades"] >= 3].sort_values("med_ret", ascending=False).head(15)
    print(f"    {'item':<28}{'trades':>7}{'win':>7}{'med ret':>9}{'med hold':>10}")
    for _, r in by_item.iterrows():
        print(f"    {str(r['name'])[:27]:<28}{int(r['trades']):>7}{_pct(r['win_rate']):>7}"
              f"{_pct(r['med_ret']):>9}{r['med_hold']:>9.0f}h")


if __name__ == "__main__":
    main()

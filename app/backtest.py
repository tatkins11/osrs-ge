"""Backtest the mean-reversion strategy on collected hourly history.

Base strategy: when an item's price falls to <= entry_z std-devs below its
trailing mean, BUY. Exit when it reverts to the mean, after max_hold hours
(timeout), or on a stop. Exits taxed at 2%.

Two optional defenses against "catching a falling knife":
  * confirm N  -- only enter once the price has ticked UP vs N hours ago
                  (wait for the bounce instead of buying mid-crash).
  * stop_pct   -- hard stop: bail if price drops this fraction below entry,
                  capping the loss instead of riding down to stop_z.

Realism filters: liquidity by GP TRADED/DAY (not unit count), a minimum price,
and per-trade returns winsorized to +/-50% for the average (median is uncapped).
Trailing stats only (no look-ahead); fills @ mid (optimistic); buy limits ignored.

Run on the VPS:
    python -m app.backtest
    python -m app.backtest --sweep
    python -m app.backtest --grid          # compare filter combinations
"""
from __future__ import annotations

import argparse
import logging

import numpy as np
import pandas as pd

from . import tax as taxmod
from .db import connect, get_items_df

log = logging.getLogger("backtest")

WINSOR = 0.50


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
                  max_hold: int, exempt: bool, confirm: int = 0, stop_pct: float = 0.0) -> list[dict]:
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
            ok = z[i] <= entry_z
            if ok and confirm > 0:                       # wait for an up-tick (bounce)
                ok = i >= confirm and mid[i] > mid[i - confirm]
            if ok:
                in_pos, entry_i, entry_p = True, i, mid[i]
        else:
            hold = i - entry_i
            reason = ""
            if mid[i] >= ma[i]:
                reason = "reverted"
            elif stop_pct > 0 and mid[i] <= entry_p * (1 - stop_pct):
                reason = "stop"
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
        min_gp_vol: float = 50_000_000.0, min_price: float = 1000.0, confirm: int = 0, stop_pct: float = 0.0,
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
        if avg_price * g["vol"].mean() * 24.0 < min_gp_vol or avg_price < min_price:
            continue
        exempt = bool(items.loc[iid, "exempt"]) if iid in items.index else False
        for t in backtest_item(g["mid"].to_numpy(float), window, entry_z, stop_z, max_hold,
                               exempt, confirm=confirm, stop_pct=stop_pct):
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
    ap.add_argument("--window", type=int, default=168)
    ap.add_argument("--max-hold", type=int, default=72)
    ap.add_argument("--stop-z", type=float, default=-4.0)
    ap.add_argument("--min-gp-vol", type=float, default=50_000_000.0)
    ap.add_argument("--min-price", type=float, default=1000.0)
    ap.add_argument("--confirm", type=int, default=0, help="require up-tick vs N hours ago before entry")
    ap.add_argument("--stop-pct", type=float, default=0.0, help="hard stop, fraction below entry (e.g. 0.08)")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--grid", action="store_true", help="compare confirm/stop_pct combos at --entry-z")
    args = ap.parse_args()

    con = connect(read_only=True)
    try:
        hist = _load_history(con)
        items = get_items_df(con).set_index("item_id")
    finally:
        con.close()

    base = dict(window=args.window, max_hold=args.max_hold, stop_z=args.stop_z,
                min_gp_vol=args.min_gp_vol, min_price=args.min_price, hist=hist, items=items)
    print(f"Liquid items only: >= {args.min_gp_vol:,.0f} gp/day, price >= {args.min_price:,.0f}; "
          f"returns winsorized +/-{int(WINSOR*100)}% for the average.\n")

    if args.grid:
        print(f"Filter grid at entry z <= {args.entry_z}  (looking for profit factor > 1):\n")
        print(f"{'confirm':>8}{'stop%':>7}{'trades':>8}{'win':>7}{'median':>9}{'profit factor':>15}")
        for confirm in (0, 2, 4):
            for stop_pct in (0.0, 0.08, 0.15):
                s = summarize(run(entry_z=args.entry_z, confirm=confirm, stop_pct=stop_pct, **base))
                if s["trades"]:
                    pf = s["profit_factor"]
                    pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
                    print(f"{confirm:>8}{stop_pct*100:>6.0f}%{s['trades']:>8}{_pct(s['win_rate']):>7}"
                          f"{_pct(s['median_return']):>9}{pf_s:>15}")
                else:
                    print(f"{confirm:>8}{stop_pct*100:>6.0f}%{0:>8}")
        return

    if args.sweep:
        print(f"{'entry z':>8}{'trades':>9}{'items':>7}{'win rate':>10}{'avg ret':>9}{'median':>9}{'med hold':>10}{'PF':>7}")
        for ez in (-1.0, -1.5, -2.0, -2.5, -3.0):
            s = summarize(run(entry_z=ez, confirm=args.confirm, stop_pct=args.stop_pct, **base))
            if s["trades"]:
                pf = s["profit_factor"]
                pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
                print(f"{ez:>8.1f}{s['trades']:>9}{s['items']:>7}{_pct(s['win_rate']):>10}"
                      f"{_pct(s['avg_return_wins']):>9}{_pct(s['median_return']):>9}{s['median_hold_h']:>9.0f}h{pf_s:>7}")
            else:
                print(f"{ez:>8.1f}{0:>9}")
        return

    tr = run(entry_z=args.entry_z, confirm=args.confirm, stop_pct=args.stop_pct, **base)
    s = summarize(tr)
    print(f"entry z <= {args.entry_z}, confirm {args.confirm}h, stop_pct {args.stop_pct}, max hold {args.max_hold}h\n")
    if not s["trades"]:
        print("No trades after filtering.")
        return
    print(f"  trades             : {s['trades']}  across {s['items']} liquid items")
    print(f"  win rate           : {_pct(s['win_rate'])}")
    print(f"  avg return (wins.) : {_pct(s['avg_return_wins'])}")
    print(f"  median return      : {_pct(s['median_return'])}")
    print(f"  median hold        : {s['median_hold_h']:.0f}h")
    print(f"  profit factor      : {s['profit_factor']:.2f}")


if __name__ == "__main__":
    main()

"""Crash-&-recover study.

Thesis: a normally-stable item drops sharply BELOW its established level (a
temporary dislocation -- panic dump, event, manipulation), then recovers. Buy
the dip, target recovery toward the pre-crash level, stop out if it keeps
falling. Proceeds taxed 2%.

Fill realism matters enormously here:
  * fill="spread" (default, CONSERVATIVE): buy at the insta-buy/ask, sell at the
    insta-sell/bid -- you pay the full spread. This neutralises the fake "crash"
    artifact where a single cheap insta-sell print drops the MID but the ask
    never moved (so you could never actually buy cheap).
  * fill="mid": buy and sell at the mid -- optimistic, prone to that artifact.
A real, tradeable edge should survive the conservative fill.

Run on the VPS (6h/24h backfilled history):
    python -m app.crash --sweep                 # conservative fills
    python -m app.crash --sweep --fill mid       # optimistic, for comparison
    python -m app.crash --sweep --confirm 1      # require the crash to persist a period
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
        SELECT item_id, ts, avg_high, avg_low,
               (avg_high + avg_low) / 2.0 AS mid,
               (COALESCE(high_vol, 0) + COALESCE(low_vol, 0)) AS vol,
               COALESCE(low_vol, 0) AS low_vol   -- separate insta-sell volume: the liquidity floor for SELL exits
        FROM history
        WHERE timestep = ? AND avg_high IS NOT NULL AND avg_low IS NOT NULL
        ORDER BY item_id, ts
        """,
        [timestep],
    ).df()


def backtest_item(mid, hi, lo, ref_window, crash_pct, recover_to, stop_pct, max_hold,
                  recent_k, exempt, fill="spread", confirm=0) -> list[dict]:
    n = len(mid)
    if n < ref_window + 5:
        return []
    s = pd.Series(mid, dtype="float64")
    ref = s.rolling(ref_window, min_periods=max(3, ref_window // 2)).median().to_numpy()

    trades: list[dict] = []
    in_pos = False
    entry_i = entry_ref = entry_mid = buy_p = 0.0
    for i in range(n):
        if np.isnan(ref[i]) or ref[i] <= 0:
            continue
        if not in_pos:
            thresh = ref[i] * (1 - crash_pct)
            crashed = mid[i] <= thresh
            if crashed and confirm > 0:  # crash must have persisted, not a 1-print spike
                crashed = all(j >= 0 and mid[j] <= ref[j] * (1 - crash_pct) for j in range(i - confirm, i))
            if crashed and recent_k > 0:  # and the level was intact recently (sharp, not a grind)
                lo_i = max(0, i - recent_k)
                crashed = np.nanmax(mid[lo_i:i + 1]) >= ref[i] * 0.97
            if crashed:
                in_pos = True
                entry_i, entry_ref, entry_mid = i, ref[i], mid[i]
                buy_p = hi[i] if (fill == "spread" and not np.isnan(hi[i])) else mid[i]
        else:
            hold = i - entry_i
            reason = ""
            if mid[i] >= entry_ref * recover_to:
                reason = "recovered"
            elif mid[i] <= entry_mid * (1 - stop_pct):
                reason = "stop"
            elif hold >= max_hold:
                reason = "timeout"
            if reason and buy_p > 0:
                sell_p = lo[i] if (fill == "spread" and not np.isnan(lo[i])) else mid[i]
                net = taxmod.net_sell(int(round(sell_p)), exempt) - buy_p
                trades.append({"net": net, "ret": net / buy_p, "hold": hold, "reason": reason})
                in_pos = False
    return trades


def run(timestep="6h", ref_window=28, crash_pct=0.18, recover_to=0.95, stop_pct=0.15, max_hold=40,
        recent_k=0, fill="spread", confirm=0, min_gp_vol=50_000_000.0, min_price=1000.0,
        hist=None, items=None, con=None) -> pd.DataFrame:
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
        for t in backtest_item(g["mid"].to_numpy(float), g["avg_high"].to_numpy(float),
                               g["avg_low"].to_numpy(float), ref_window, crash_pct, recover_to,
                               stop_pct, max_hold, recent_k, exempt, fill, confirm):
            t["item_id"] = int(iid)
            t["name"] = items.loc[iid, "name"] if iid in items.index else str(iid)
            rows.append(t)
    return pd.DataFrame(rows)


def summarize(tr: pd.DataFrame, timestep_h: int) -> dict:
    if tr.empty:
        return {"trades": 0}
    r = tr["ret"].clip(-WINSOR, WINSOR)          # PF on winsorized returns, consistent with avg_return
    wins = r[r > 0].sum()
    losses = r[r < 0].sum()
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
    ap.add_argument("--recent-k", type=int, default=0)
    ap.add_argument("--confirm", type=int, default=0, help="crash must persist this many prior periods")
    ap.add_argument("--fill", default="spread", choices=["spread", "mid"])
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
                stop_pct=args.stop_pct, max_hold=args.max_hold, recent_k=args.recent_k, fill=args.fill,
                confirm=args.confirm, min_gp_vol=args.min_gp_vol, min_price=args.min_price, hist=hist, items=items)
    print(f"Crash-&-recover on {args.timestep} -- fill={args.fill} ({'CONSERVATIVE: pay the spread' if args.fill=='spread' else 'optimistic: mid'}), "
          f"recover_to {args.recover_to}, stop {args.stop_pct}, confirm {args.confirm}, recent_k {args.recent_k}; "
          f"liquid >= {args.min_gp_vol:,.0f} gp/day, price >= {args.min_price:,.0f}.\n")

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

    s = summarize(run(crash_pct=args.crash_pct, **base), tsh)
    if not s["trades"]:
        print("No trades.")
        return
    print(f"crash >= {args.crash_pct*100:.0f}%: trades {s['trades']} / {s['items']} items | win {_pct(s['win_rate'])} | "
          f"avg {_pct(s['avg_return'])} median {_pct(s['median_return'])} | hold {s['median_hold_days']:.1f}d | "
          f"recovered {_pct(s['recovered_pct'])} | PF {s['profit_factor']:.2f}")


if __name__ == "__main__":
    main()

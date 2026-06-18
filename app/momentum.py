"""Momentum / trend-following study.

Thesis (the classic equity momentum factor, applied to OSRS): items in an
established uptrend keep rising. Go long when price holds above its moving
average; exit when the trend breaks (price falls back below the MA), a stop
hits, or a max hold elapses. Proceeds taxed 2%.

This is the MIRROR of the crash-&-recover study (reversion). It only earns its
place as a signal if it survives the same conservative test:
  * fill="spread" (default): buy at the insta-buy/ask, sell at the insta-sell/bid
    -- you pay the full spread on every round trip. Momentum trades more often
    than buy-and-hold, so spread + 2% tax are a real drag; a true edge must clear
    them.
  * fill="mid": optimistic mid-to-mid, for comparison.

Run on the VPS (6h/24h backfilled history):
    python -m app.momentum --sweep                  # conservative fills, daily bars
    python -m app.momentum --sweep --fill mid        # optimistic, for comparison
    python -m app.momentum --sweep --timestep 6h     # finer bars
"""
from __future__ import annotations

import argparse
import logging

import numpy as np
import pandas as pd

from . import tax as taxmod
from .db import connect, get_items_df

log = logging.getLogger("momentum")
WINSOR = 0.50


def _load(con, timestep: str) -> pd.DataFrame:
    return con.execute(
        """
        SELECT item_id, ts, avg_high, avg_low,
               (avg_high + avg_low) / 2.0 AS mid,
               (COALESCE(high_vol, 0) + COALESCE(low_vol, 0)) AS vol
        FROM history
        WHERE timestep = ? AND avg_high IS NOT NULL AND avg_low IS NOT NULL
        ORDER BY item_id, ts
        """,
        [timestep],
    ).df()


def backtest_item(mid, hi, lo, window, entry_buf, stop_pct, max_hold,
                  exempt, fill="spread", mode="ma") -> list[dict]:
    """mode="ma": enter when price > MA*(1+buf), exit on dip below MA / stop / timeout
       mode="breakout": enter on a new `window`-bar high, HOLD through dips (exit only
       on stop / timeout) -- tests "winners keep winning" without MA-whipsaw."""
    n = len(mid)
    if n < window + 5:
        return []
    s = pd.Series(mid, dtype="float64")
    mp = max(3, window // 2)
    ma = s.rolling(window, min_periods=mp).mean().to_numpy()
    prior_high = s.shift(1).rolling(window, min_periods=mp).max().to_numpy()

    trades: list[dict] = []
    in_pos = False
    entry_i = entry_mid = buy_p = 0.0
    for i in range(n):
        ref = ma[i] if mode == "ma" else prior_high[i]
        if np.isnan(ref) or ref <= 0:
            continue
        if not in_pos:
            enter = (mid[i] > ma[i] * (1 + entry_buf)) if mode == "ma" else (mid[i] >= prior_high[i])
            if enter:
                in_pos = True
                entry_i, entry_mid = i, mid[i]
                buy_p = hi[i] if (fill == "spread" and not np.isnan(hi[i])) else mid[i]
        else:
            hold = i - entry_i
            reason = ""
            if mode == "ma" and not np.isnan(ma[i]) and mid[i] < ma[i]:
                reason = "trend_break"
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


def run(timestep="24h", window=20, entry_buf=0.02, stop_pct=0.15, max_hold=60,
        fill="spread", mode="ma", min_gp_vol=50_000_000.0, min_price=1000.0,
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
                               g["avg_low"].to_numpy(float), window, entry_buf,
                               stop_pct, max_hold, exempt, fill, mode):
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
        "trend_break_pct": (tr["reason"] == "trend_break").mean(),
    }


def _pct(x):
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.1f}%"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Momentum / trend-following backtest.")
    ap.add_argument("--timestep", default="24h", choices=["6h", "24h", "1h"])
    ap.add_argument("--ma-window", type=int, default=20)
    ap.add_argument("--entry-buf", type=float, default=0.02)
    ap.add_argument("--stop-pct", type=float, default=0.15)
    ap.add_argument("--max-hold", type=int, default=60)
    ap.add_argument("--fill", default="spread", choices=["spread", "mid"])
    ap.add_argument("--mode", default="ma", choices=["ma", "breakout"])
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

    base = dict(timestep=args.timestep, entry_buf=args.entry_buf, stop_pct=args.stop_pct,
                max_hold=args.max_hold, fill=args.fill, mode=args.mode, min_gp_vol=args.min_gp_vol,
                min_price=args.min_price, hist=hist, items=items)
    print(f"Momentum mode={args.mode} on {args.timestep} -- fill={args.fill} "
          f"({'CONSERVATIVE: pay the spread' if args.fill == 'spread' else 'optimistic: mid'}), "
          f"entry_buf {args.entry_buf}, stop {args.stop_pct}, max_hold {args.max_hold}; "
          f"liquid >= {args.min_gp_vol:,.0f} gp/day, price >= {args.min_price:,.0f}.\n")

    if args.sweep:
        print(f"{'MA win':>7}{'trades':>8}{'items':>7}{'win':>8}{'avg ret':>9}{'median':>9}{'med days':>10}{'brk%':>7}{'PF':>7}")
        for mw in (7, 14, 20, 30, 50):
            s = summarize(run(window=mw, **base), tsh)
            if s["trades"]:
                pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
                print(f"{mw:>7}{s['trades']:>8}{s['items']:>7}{_pct(s['win_rate']):>8}{_pct(s['avg_return']):>9}"
                      f"{_pct(s['median_return']):>9}{s['median_hold_days']:>9.1f}d{_pct(s['trend_break_pct']):>7}{pf:>7}")
            else:
                print(f"{mw:>7}{0:>8}")
        return

    s = summarize(run(window=args.ma_window, **base), tsh)
    if not s["trades"]:
        print("No trades.")
        return
    print(f"window {args.ma_window}: trades {s['trades']} / {s['items']} items | win {_pct(s['win_rate'])} | "
          f"avg {_pct(s['avg_return'])} median {_pct(s['median_return'])} | hold {s['median_hold_days']:.1f}d | "
          f"PF {s['profit_factor']:.2f}")


if __name__ == "__main__":
    main()

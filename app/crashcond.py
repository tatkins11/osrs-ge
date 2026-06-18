"""Crash conditioning study.

Two questions, on the already-validated crash-&-recover edge (buy the dip at the
ask, sell recovery at the bid, 2% tax -- conservative):
  1. Do crashes that bottom NEAR their high-alch floor recover better? (alching is
     a real hard buyer -> downside support)
  2. Do crashes on a CAPITULATION volume spike recover better than a quiet drift?

Each trade is tagged at entry with:
  * support  = (buy - alch_floor)/buy        (alch_floor = highalch - nature rune)
  * volspike = entry volume / trailing median volume
then win-rate / profit-factor are bucketed by each. If "near floor" / "high volume"
beat the rest, it's a real conditioning signal to fold into the Crashes/Invest score.

Run on the VPS:  python -m app.crashcond
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from . import tax as taxmod
from .crash import _load
from .db import connect, get_items_df

WINSOR = 0.50
NAT_COST = 120.0  # approx nature rune cost for the alch floor (highalch dominates the floor)


def backtest_item(mid, hi, lo, vol, ref_window, crash_pct, recover_to, stop_pct, max_hold, exempt, alch_floor):
    n = len(mid)
    if n < ref_window + 5:
        return []
    ref = pd.Series(mid, dtype="float64").rolling(ref_window, min_periods=max(3, ref_window // 2)).median().to_numpy()
    vmed = pd.Series(vol, dtype="float64").rolling(ref_window, min_periods=max(3, ref_window // 2)).median().to_numpy()
    trades, in_pos = [], False
    entry_i = entry_ref = entry_mid = buy_p = 0.0
    e_support = e_volspike = None
    for i in range(n):
        if np.isnan(ref[i]) or ref[i] <= 0:
            continue
        if not in_pos:
            if mid[i] <= ref[i] * (1 - crash_pct):
                in_pos, entry_i, entry_ref, entry_mid = True, i, ref[i], mid[i]
                buy_p = hi[i] if not np.isnan(hi[i]) else mid[i]
                e_support = ((buy_p - alch_floor) / buy_p) if (alch_floor > 0 and buy_p > 0) else None
                e_volspike = (vol[i] / vmed[i]) if (vmed[i] and vmed[i] > 0) else None
        else:
            hold = i - entry_i
            reason = ("recovered" if mid[i] >= entry_ref * recover_to
                      else "stop" if mid[i] <= entry_mid * (1 - stop_pct)
                      else "timeout" if hold >= max_hold else "")
            if reason and buy_p > 0:
                sell_p = lo[i] if not np.isnan(lo[i]) else mid[i]
                net = taxmod.net_sell(int(round(sell_p)), exempt) - buy_p
                trades.append({"net": net, "ret": net / buy_p, "support": e_support, "volspike": e_volspike})
                in_pos = False
    return trades


def run(timestep="6h", crash_pct=0.18, ref_window=28, recover_to=0.95, stop_pct=0.15, max_hold=40,
        min_gp_vol=50_000_000.0, min_price=1000.0) -> pd.DataFrame:
    con = connect(read_only=True)
    try:
        hist = _load(con, timestep)
        items = get_items_df(con).set_index("item_id")
    finally:
        con.close()
    rows = []
    for iid, g in hist.groupby("item_id"):
        avg = g["mid"].mean()
        if avg * g["vol"].mean() * 24.0 < min_gp_vol or avg < min_price:
            continue
        exempt = bool(items.loc[iid, "exempt"]) if iid in items.index else False
        ha = float(items.loc[iid, "highalch"]) if (iid in items.index and pd.notna(items.loc[iid, "highalch"])) else 0.0
        alch_floor = (ha - NAT_COST) if ha > 0 else 0.0
        rows += backtest_item(g["mid"].to_numpy(float), g["avg_high"].to_numpy(float), g["avg_low"].to_numpy(float),
                              g["vol"].to_numpy(float), ref_window, crash_pct, recover_to, stop_pct, max_hold, exempt, alch_floor)
    return pd.DataFrame(rows)


def _line(label, d):
    if not len(d):
        print(f"  {label:<16}{0:>8}")
        return
    r = d["ret"].clip(-WINSOR, WINSOR)
    w, l = r[r > 0].sum(), r[r < 0].sum()
    pf = "inf" if l >= 0 else f"{w / abs(l):.2f}"
    print(f"  {label:<16}{len(d):>8}{(d['net'] > 0).mean() * 100:>7.0f}%{r.mean() * 100:>8.1f}%{pf:>7}")


def _buckets(df, col, edges, labels):
    print(f"  {'bucket':<16}{'trades':>8}{'win':>8}{'avg ret':>9}{'PF':>7}")
    for lab, lo, hi in zip(labels, edges[:-1], edges[1:]):
        _line(lab, df[(df[col] >= lo) & (df[col] < hi)])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    df = run()
    print(f"\nCrash conditioning (6h, crash>=18%, conservative fills): {len(df)} trades")
    print(f"{'  baseline':<16}{'':>0}")
    _line("ALL", df)
    print("\nBy alch-floor support (lower = price bottomed nearer the alch floor):")
    _buckets(df.dropna(subset=["support"]), "support", [-1e9, 0.10, 0.30, 1e9],
             ["near (<=10%)", "mid (10-30%)", "far (>30%)"])
    print("\nBy capitulation volume (entry volume / trailing median):")
    _buckets(df.dropna(subset=["volspike"]), "volspike", [0, 1.0, 2.0, 1e18],
             ["quiet (<1x)", "elevated (1-2x)", "spike (>2x)"])
    print()


if __name__ == "__main__":
    main()

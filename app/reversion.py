"""Reversion half-life study (6h bars).

For each liquid item, after a DOWNSIDE dislocation (z <= -Z_THRESH below its
trailing mean) we measure:
  * half_life  -- bars until the dip recovers HALF-way back to the mean
  * reliability-- fraction of dips that recover halfway within MAX_BARS

Why: the Invest tab currently labels holding horizon by a crude liquidity bucket
(gp/day). If a *measured* per-item half-life is predictive out-of-sample, it should
replace that guess and sharpen confidence (short, reliable revert = real edge;
long/unreliable = the week-horizon mirage the validation already flagged).

Validation (--oos): fit reliability/half-life on the FIRST half of each item's
history; on the SECOND half measure realized reliability AND a conservative
reversion return (buy the dip at the ask, sell on recovery at the bid, 2% tax).
If in-sample reliability predicts out-of-sample reliability/return, it's real.

Run on the VPS:  python -m app.reversion --oos
"""
from __future__ import annotations

import argparse
import logging

import numpy as np
import pandas as pd

from . import tax as taxmod
from .db import connect, get_items_df

W = 28            # trailing mean/sd window (~7 days on 6h bars)
Z_THRESH = 1.5    # a "dislocation" is this many sigma below the trailing mean
MAX_BARS = 40     # give a dip this long to recover halfway (~10 days on 6h)
BARS_PER_DAY = 4  # 6h bars
MIN_GP_VOL = 25_000_000.0


def _load(con, timestep: str = "6h") -> pd.DataFrame:
    return con.execute(
        f"""SELECT item_id, ts, avg_high, avg_low, (COALESCE(high_vol,0)+COALESCE(low_vol,0)) AS vol
            FROM history WHERE timestep='{timestep}' AND avg_high IS NOT NULL AND avg_low IS NOT NULL
            ORDER BY item_id, ts"""
    ).df()


def _events(mid, lo, hi, exempt: bool, measure_return: bool):
    """Dip events on one item's arrays. Each -> (half_life_bars|None, reverted bool, ret|None)."""
    n = len(mid)
    if n < W + 5:
        return []
    s = pd.Series(mid)
    mean = s.rolling(W).mean().to_numpy()
    sd = s.rolling(W).std().to_numpy()
    out = []
    i = W
    while i < n:
        if not (sd[i] > 0) or np.isnan(mean[i]):
            i += 1
            continue
        z = (mid[i] - mean[i]) / sd[i]
        prevz = (mid[i - 1] - mean[i - 1]) / sd[i - 1] if (sd[i - 1] > 0 and not np.isnan(mean[i - 1])) else 0.0
        if z <= -Z_THRESH and prevz > -Z_THRESH:        # fresh downside dislocation
            d0 = mean[i] - mid[i]                        # gap below the mean (positive)
            target = mean[i] - 0.5 * d0                  # halfway back up
            hl, reverted = None, False
            jmax = min(i + MAX_BARS, n)
            for j in range(i + 1, jmax):
                if mid[j] >= target:
                    hl, reverted = j - i, True
                    break
            ret = None
            if measure_return:
                buy = hi[i]                              # pay the ask at the dip (conservative)
                jend = (i + hl) if reverted else (jmax - 1)
                sell = lo[jend]                          # sell into the bid on recovery / timeout
                if buy and buy > 0 and not np.isnan(buy) and not np.isnan(sell):
                    ret = (taxmod.net_sell(int(round(sell)), exempt) - buy) / buy
            out.append((hl, reverted, ret))
            i = (i + hl) if hl else jmax                 # skip past this event (no overlap)
        else:
            i += 1
    return out


def _agg(events, measure_return: bool) -> dict:
    if not events:
        return {}
    hls = [e[0] for e in events if e[0] is not None]
    rel = float(np.mean([1.0 if e[1] else 0.0 for e in events]))
    out = {"n_events": len(events), "reliability": rel,
           "half_life_bars": float(np.median(hls)) if hls else None}
    if measure_return:
        rets = [e[2] for e in events if e[2] is not None]
        out["mean_ret"] = float(np.mean(np.clip(rets, -0.5, 0.5))) if rets else None
    return out


def run(timestep: str = "6h", con=None) -> pd.DataFrame:
    own = con is None
    con = con or connect(read_only=True)
    try:
        hist = _load(con, timestep)
        items = get_items_df(con).set_index("item_id")
    finally:
        if own:
            con.close()
    rows = []
    for iid, g in hist.groupby("item_id"):
        if g["avg_high"].mean() * g["vol"].mean() * BARS_PER_DAY < MIN_GP_VOL:
            continue
        mid = ((g["avg_high"] + g["avg_low"]) / 2.0).to_numpy("float64")
        ex = bool(items.loc[iid, "exempt"]) if iid in items.index else False
        a = _agg(_events(mid, g["avg_low"].to_numpy("float64"), g["avg_high"].to_numpy("float64"), ex, False), False)
        if a.get("n_events", 0) >= 3:
            rows.append({"item_id": int(iid), **a})
    return pd.DataFrame(rows)


def oos(timestep: str = "6h", con=None) -> pd.DataFrame:
    own = con is None
    con = con or connect(read_only=True)
    try:
        hist = _load(con, timestep)
        items = get_items_df(con).set_index("item_id")
    finally:
        if own:
            con.close()
    rows = []
    for iid, g in hist.groupby("item_id"):
        if g["avg_high"].mean() * g["vol"].mean() * BARS_PER_DAY < MIN_GP_VOL:
            continue
        mid = ((g["avg_high"] + g["avg_low"]) / 2.0).to_numpy("float64")
        lo = g["avg_low"].to_numpy("float64")
        hi = g["avg_high"].to_numpy("float64")
        ex = bool(items.loc[iid, "exempt"]) if iid in items.index else False
        h = len(mid) // 2
        if h < W + 10:
            continue
        is_a = _agg(_events(mid[:h], lo[:h], hi[:h], ex, False), False)
        oos_a = _agg(_events(mid[h:], lo[h:], hi[h:], ex, True), True)
        if is_a.get("n_events", 0) >= 3 and oos_a.get("n_events", 0) >= 3:
            rows.append({
                "item_id": int(iid),
                "is_reliability": is_a["reliability"], "is_half_life": is_a.get("half_life_bars"),
                "oos_reliability": oos_a["reliability"], "oos_ret": oos_a.get("mean_ret"),
            })
    return pd.DataFrame(rows)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Reversion half-life study.")
    ap.add_argument("--oos", action="store_true", help="out-of-sample predictiveness")
    args = ap.parse_args()

    if not args.oos:
        df = run()
        print(f"\nReversion half-life (6h bars, z<=-{Z_THRESH}, halfway-recovery within {MAX_BARS} bars):")
        print(f"  items with >=3 dislocations: {len(df)}")
        if len(df):
            hl_days = df["half_life_bars"].dropna() / BARS_PER_DAY
            print(f"  half-life (days): median {hl_days.median():.1f}, p25 {hl_days.quantile(.25):.1f}, p75 {hl_days.quantile(.75):.1f}")
            print(f"  reliability: median {df['reliability'].median()*100:.0f}%, "
                  f"share of items >=70% reliable: {(df['reliability']>=0.7).mean()*100:.0f}%")
        return

    df = oos()
    print(f"\nOUT-OF-SAMPLE reversion predictiveness (fit on 1st half, test on 2nd):")
    print(f"  items with >=3 dislocations each half: {len(df)}")
    if len(df) < 20:
        print("  too few items to judge."); return
    c1 = np.corrcoef(df["is_reliability"], df["oos_reliability"])[0, 1]
    sub = df.dropna(subset=["oos_ret"])
    c2 = np.corrcoef(sub["is_reliability"], sub["oos_ret"])[0, 1] if len(sub) > 10 else float("nan")
    print(f"  corr(IS reliability, OOS reliability) = {c1:+.2f}")
    print(f"  corr(IS reliability, OOS return)      = {c2:+.2f}")
    df["bucket"] = pd.qcut(df["is_reliability"].rank(method="first"), 3, labels=["low", "mid", "high"])
    print(f"\n  {'IS-reliability bucket':<22}{'items':>7}{'IS rel':>8}{'OOS rel':>9}{'OOS ret':>9}")
    for b in ["low", "mid", "high"]:
        d = df[df["bucket"] == b]
        rr = d["oos_ret"].dropna()
        print(f"  {b:<22}{len(d):>7}{d['is_reliability'].mean()*100:>7.0f}%{d['oos_reliability'].mean()*100:>8.0f}%"
              f"{(rr.mean()*100 if len(rr) else float('nan')):>8.1f}%")
    print()


if __name__ == "__main__":
    main()

"""Batch of quant research studies (read-only backtests; run on the VPS).

    python -m app.studies               # run all
    python -m app.studies --study movers

Conventions: 6h bars unless noted; "net" returns are conservative (buy the ask,
sell the bid, 2% tax). Returns winsorized at +/-50% for means. A real edge must
clear costs out-of-sample / not be an artifact.
"""
from __future__ import annotations

import argparse
import logging

import numpy as np
import pandas as pd

from . import tax as taxmod
from .crash import _load, backtest_item as crash_bt
from .db import connect, get_items_df
from .sectors import classify_one

WINSOR = 0.50
BPD = 4  # 6h bars per day


def _exempt(items, iid):
    return bool(items.loc[iid, "exempt"]) if iid in items.index else False


def _liquid(g, min_gp_vol=50_000_000.0, min_price=1000.0):
    p = g["mid"].mean()
    return p >= min_price and p * g["vol"].mean() * BPD >= min_gp_vol


def _wmean(x):
    a = np.asarray([v for v in x if v is not None and np.isfinite(v)], dtype="float64")
    return float(np.clip(a, -WINSOR, WINSOR).mean()) if a.size else float("nan")


# --- 1. Movers: does a volume spike predict a forward move? -----------------
def movers(hist, items, spike=2.0, k_fwd=4, win=28):
    rows = []
    for iid, g in hist.groupby("item_id"):
        if not _liquid(g):
            continue
        g = g.reset_index(drop=True)
        n = len(g)
        if n < win + k_fwd + 2:
            continue
        mid = g["mid"].to_numpy(float); vol = g["vol"].to_numpy(float)
        ask = g["avg_high"].to_numpy(float); bid = g["avg_low"].to_numpy(float)
        vmed = pd.Series(vol).rolling(win, min_periods=win // 2).median().to_numpy()
        ex = _exempt(items, iid)
        for i in range(win, n - k_fwd):
            if not (vmed[i] > 0) or np.isnan(mid[i]) or mid[i] <= 0 or np.isnan(mid[i - 1]):
                continue
            if vol[i] / vmed[i] < spike:
                continue
            d = "up" if mid[i] >= mid[i - 1] else "down"
            fwd = mid[i + k_fwd] / mid[i] - 1.0
            net = ((taxmod.net_sell(int(round(bid[i + k_fwd])), ex) - ask[i]) / ask[i]
                   if (ask[i] > 0 and not np.isnan(ask[i]) and not np.isnan(bid[i + k_fwd])) else None)
            rows.append({"dir": d, "fwd": fwd, "net": net})
    df = pd.DataFrame(rows)
    print(f"\n[1] MOVERS — after a >= {spike:.0f}x volume bar, forward {k_fwd*6}h move ({len(df)} spikes):")
    if df.empty:
        print("  no spikes."); return
    for lab, d in [("all spikes", df), ("spike + price UP", df[df.dir == "up"]), ("spike + price DOWN", df[df.dir == "down"])]:
        print(f"  {lab:<20} n={len(d):>5}  fwd mid {_wmean(d['fwd'])*100:+5.1f}%  net(after cost) {_wmean(d['net'])*100:+5.1f}%  win {(d['net']>0).mean()*100:>3.0f}%")


# --- 2. Crash x liquidity ---------------------------------------------------
def crashliq(hist, items, crash_pct=0.18):
    rows = []
    for iid, g in hist.groupby("item_id"):
        if not _liquid(g):
            continue
        gpv = g["mid"].mean() * g["vol"].mean() * BPD
        ex = _exempt(items, iid)
        for t in crash_bt(g["mid"].to_numpy(float), g["avg_high"].to_numpy(float), g["avg_low"].to_numpy(float),
                          28, crash_pct, 0.95, 0.15, 40, 0, ex, "spread", 0):
            t["gpv"] = gpv
            rows.append(t)
    df = pd.DataFrame(rows)
    print(f"\n[2] CRASH x LIQUIDITY — crash>={crash_pct*100:.0f}% recover, by gp/day ({len(df)} trades):")
    if df.empty:
        print("  no trades."); return
    print(f"  {'liquidity tier':<18}{'trades':>8}{'win':>7}{'avg ret':>9}{'PF':>7}")
    for lab, lo, hi in [("<100M", 0, 100e6), ("100-500M", 100e6, 500e6), (">500M", 500e6, 1e18)]:
        d = df[(df.gpv >= lo) & (df.gpv < hi)]
        if not len(d):
            print(f"  {lab:<18}{0:>8}"); continue
        r = d["ret"].clip(-WINSOR, WINSOR); w, l = r[r > 0].sum(), r[r < 0].sum()
        pf = "inf" if l >= 0 else f"{w/abs(l):.2f}"
        print(f"  {lab:<18}{len(d):>8}{(d['net']>0).mean()*100:>6.0f}%{r.mean()*100:>8.1f}%{pf:>7}")


# --- 3. Sector-level mean-reversion -----------------------------------------
def sectorrev(hist, items, z_thresh=1.0, k_fwd=4, win=28):
    name = items["name"]
    h = hist.copy()
    h["sector"] = h["item_id"].map(lambda i: classify_one(name.loc[i]) if i in name.index else None)
    h = h[h["sector"].notna()]
    rows = []
    for sec, sg in h.groupby("sector"):
        # equal-weight sector index = mean across constituents of (mid / item-mean) per ts
        sg = sg.copy()
        sg["norm"] = sg["mid"] / sg.groupby("item_id")["mid"].transform("mean")
        idx = sg.groupby("ts")["norm"].mean().sort_index()
        if len(idx) < win + k_fwd + 2:
            continue
        v = idx.to_numpy(float)
        m = pd.Series(v).rolling(win, min_periods=win // 2).mean().to_numpy()
        sd = pd.Series(v).rolling(win, min_periods=win // 2).std().to_numpy()
        for i in range(win, len(v) - k_fwd):
            if not (sd[i] > 0) or np.isnan(m[i]):
                continue
            z = (v[i] - m[i]) / sd[i]
            zprev = (v[i - 1] - m[i - 1]) / sd[i - 1] if (sd[i - 1] > 0 and not np.isnan(m[i - 1])) else 0.0
            if z <= -z_thresh and zprev > -z_thresh:
                rows.append({"sector": sec, "fwd": v[i + k_fwd] / v[i] - 1.0})
    df = pd.DataFrame(rows)
    print(f"\n[3] SECTOR MEAN-REVERSION — sector index z<=-{z_thresh}, forward {k_fwd*6}h ({len(df)} dislocations):")
    if df.empty:
        print("  none."); return
    print(f"  pooled: forward index move {_wmean(df['fwd'])*100:+.1f}%  positive {(df['fwd']>0).mean()*100:.0f}%   "
          f"(NOTE: index move, not net of per-item spread+tax)")


# --- 4. Flip-margin persistence (does margin_uptime carry forward?) ---------
def flipuptime(hist, items):
    rows = []
    for iid, g in hist.groupby("item_id"):
        if not _liquid(g):
            continue
        fm = (g["avg_high"].to_numpy(float) * 0.98 - g["avg_low"].to_numpy(float))
        n = len(fm); h = n // 2
        if h < 10:
            continue
        rows.append({"is_uptime": (fm[:h] > 0).mean(), "oos_uptime": (fm[h:] > 0).mean()})
    df = pd.DataFrame(rows).dropna()
    print(f"\n[4] FLIP-MARGIN PERSISTENCE — does 1st-half margin-uptime predict 2nd-half? ({len(df)} items):")
    if len(df) < 20:
        print("  too few."); return
    c = np.corrcoef(df["is_uptime"], df["oos_uptime"])[0, 1]
    print(f"  corr(IS uptime, OOS uptime) = {c:+.2f}")
    df["b"] = pd.qcut(df["is_uptime"].rank(method="first"), 3, labels=["low", "mid", "high"])
    for b in ["low", "mid", "high"]:
        d = df[df.b == b]
        print(f"  IS uptime {b:<5}: n={len(d):>5}  OOS uptime {d['oos_uptime'].mean()*100:>3.0f}%")


# --- 5. Market-wide seasonality (hour-of-day + day-of-week) -----------------
def seasonality(hist6, items):
    con = connect(read_only=True)
    try:
        h1 = _load(con, "1h")
    finally:
        con.close()
    liq = {iid for iid, g in h1.groupby("item_id") if _liquid(g)}
    h1 = h1[h1["item_id"].isin(liq)].copy()
    h1["ts"] = pd.to_datetime(h1["ts"]); h1["hour"] = h1["ts"].dt.hour; h1["day"] = h1["ts"].dt.normalize()
    h1["dm"] = h1.groupby(["item_id", "day"])["mid"].transform("mean")
    h1["dev"] = np.where(h1["dm"] > 0, h1["mid"] / h1["dm"] - 1.0, np.nan)
    hod = h1.groupby("hour")["dev"].mean() * 100
    print(f"\n[5] SEASONALITY — avg % vs daily mean by UTC hour (liquid universe):")
    cheap, rich = hod.idxmin(), hod.idxmax()
    print(f"  cheapest hour {cheap:02d}:00 ({hod[cheap]:+.2f}%)  richest hour {rich:02d}:00 ({hod[rich]:+.2f}%)  spread {hod[rich]-hod[cheap]:.2f}%")
    print("  by hour: " + " ".join(f"{hh:02d}:{hod.get(hh, float('nan')):+.1f}" for hh in range(0, 24, 3)))
    h = hist6.copy()
    h = h[h["item_id"].isin({iid for iid, g in hist6.groupby("item_id") if _liquid(g)})].copy()
    h["ts"] = pd.to_datetime(h["ts"]); h["dow"] = h["ts"].dt.dayofweek
    h["wm"] = h.groupby("item_id")["mid"].transform(lambda s: s.rolling(28, min_periods=14).mean())
    h["dev"] = np.where(h["wm"] > 0, h["mid"] / h["wm"] - 1.0, np.nan)
    dow = h.groupby("dow")["dev"].mean() * 100
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    print("  by day-of-week vs weekly mean: " + " ".join(f"{days[d]} {dow.get(d, float('nan')):+.2f}%" for d in range(7)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--study", default="all",
                    choices=["all", "movers", "crashliq", "sectorrev", "flipuptime", "seasonality"])
    args = ap.parse_args()
    con = connect(read_only=True)
    try:
        hist = _load(con, "6h")
        items = get_items_df(con).set_index("item_id")
    finally:
        con.close()
    if args.study in ("all", "movers"):
        movers(hist, items)
    if args.study in ("all", "crashliq"):
        crashliq(hist, items)
    if args.study in ("all", "sectorrev"):
        sectorrev(hist, items)
    if args.study in ("all", "flipuptime"):
        flipuptime(hist, items)
    if args.study in ("all", "seasonality"):
        seasonality(hist, items)
    print()


if __name__ == "__main__":
    main()

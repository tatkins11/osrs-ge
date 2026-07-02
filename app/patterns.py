"""Per-item chart-pattern rosters — the aggressive, item-specific edges.

Two playbooks, validated 2026-07-02 on unit-floored liquid items (>=30 units/day, >=25M gp/day,
50K-60M price), execution-honest (entries at the day's avg_low with >=10 sell-side prints,
exits at avg_high with >=10 buy-side prints, net of 2% tax), walk-forward bands (no lookahead):

RANGE plays  — items that reliably oscillate: buy at the item's OWN trailing-60d P20, sell at
               P70, 45d stop. OOS split (roster picked on H1, traded on H2): +7.7% median/cycle
               at 80% win, ~15-35 day holds. In-sample roster medians +6-20%/cycle.
CRASH plays  — items with a REPEATED crash->recover history: 5d drop <=-20%, buy next day,
               +15% target, 30d cap. Pooled 960 events: +17.2% median, 72% win, q10 -28%
               (size positions <=15% NW; the tail is real). Stable 2025 vs 2026.

The rosters are slow-moving (rebuilt at most every 12h, cached); the /api/patterns endpoint
joins them to LIVE prices so the UI can flag what's actionable right now.
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

from . import tax as taxmod
from .db import connect

_CACHE: dict = {"ts": 0.0, "out": None}
_TTL = 12 * 3600.0


def _load(con) -> pd.DataFrame:
    h = con.execute(
        """SELECT item_id, ts, avg_high, avg_low, high_vol, low_vol
           FROM history WHERE timestep = '24h' ORDER BY item_id, ts"""
    ).df()
    h["ts"] = pd.to_datetime(h["ts"])
    h["mid"] = (h.avg_high + h.avg_low) / 2
    h["units"] = h.high_vol + h.low_vol
    agg = h.groupby("item_id").agg(gp=("mid", lambda s: 0), u=("units", "median"), m=("mid", "median"))
    gp = h.assign(gp=h.mid * h.units).groupby("item_id")["gp"].mean()
    agg["gp"] = gp
    agg = agg.astype("float64").fillna(0)
    uni = agg[(agg.gp >= 25_000_000) & (agg.u >= 30) & (agg.m >= 50_000) & (agg.m <= 60_000_000)].index
    return h[h.item_id.isin(uni) & (h.high_vol > 0) & (h.low_vol > 0)]


def _sim_range(g: pd.DataFrame) -> list[tuple]:
    g = g.reset_index(drop=True)
    lo_b = g.mid.rolling(60, min_periods=45).quantile(0.20).shift(1)
    hi_b = g.mid.rolling(60, min_periods=45).quantile(0.70).shift(1)
    trades, pos = [], None
    for i in range(len(g)):
        if pos is None:
            if not np.isnan(lo_b[i]) and g.mid[i] <= lo_b[i] and g.low_vol[i] >= 10:
                pos = (i, float(g.avg_low[i]))
        else:
            i0, entry = pos
            held = i - i0
            if not np.isnan(hi_b[i]) and g.mid[i] >= hi_b[i] and g.high_vol[i] >= 10:
                trades.append(((taxmod.net_sell(int(g.avg_high[i]), False) - entry) / entry, held)); pos = None
            elif held >= 45:
                trades.append(((taxmod.net_sell(int(g.avg_low[i]), False) - entry) / entry, held)); pos = None
    return trades


def _sim_crash(g: pd.DataFrame) -> list[float]:
    g = g.reset_index(drop=True)
    r5 = g.mid.pct_change(5)
    ev, i = [], 6
    while i < len(g) - 31:
        if r5[i] <= -0.20 and g.low_vol[min(i + 1, len(g) - 1)] >= 10:
            entry = float(g.avg_low[i + 1])
            if entry > 0:
                ret = None
                for j in range(i + 2, min(i + 31, len(g))):
                    if g.high_vol[j] < 10:
                        continue
                    r = (taxmod.net_sell(int(g.avg_high[j]), False) - entry) / entry
                    if r >= 0.15:
                        ret = r
                        break
                if ret is None:
                    j = min(i + 30, len(g) - 1)
                    ret = (taxmod.net_sell(int(g.avg_high[j]), False) - entry) / entry
                ev.append(float(np.clip(ret, -0.6, 0.6)))
            i += 12
        else:
            i += 1
    return ev


def rosters(con=None) -> dict:
    """Build (or serve cached) pattern rosters joined to live band positions."""
    now = time.time()
    if _CACHE["out"] is not None and now - _CACHE["ts"] < _TTL:
        return _CACHE["out"]
    own = con is None
    con = con or connect(read_only=True)
    try:
        items = con.execute("SELECT item_id, name FROM items").df()
        name_of = dict(zip(items.item_id, items["name"]))
        h = _load(con)
    finally:
        if own:
            con.close()

    rng_rows, crash_rows = [], []
    for iid, g in h.groupby("item_id"):
        if len(g) < 250:
            continue
        tr = _sim_range(g)
        if len(tr) >= 4:
            r = pd.Series([t[0] for t in tr]).clip(-0.5, 0.5)
            held = float(np.mean([t[1] for t in tr]))
            if r.median() >= 0.06 and (r > 0).mean() >= 0.75:
                gg = g.reset_index(drop=True)
                p20 = float(gg.mid.tail(60).quantile(0.20))
                p70 = float(gg.mid.tail(60).quantile(0.70))
                cur = float(gg.mid.iloc[-1])
                rng_rows.append({
                    "item_id": int(iid), "name": name_of.get(iid, str(iid)),
                    "cycles": len(tr), "med_ret": round(float(r.median()), 4),
                    "win": round(float((r > 0).mean()), 3), "avg_days": round(held, 1),
                    "p20": round(p20), "p70": round(p70), "cur": round(cur),
                    "at_band": bool(cur <= p20 * 1.01),
                })
        ev = _sim_crash(g)
        if len(ev) >= 3:
            e = pd.Series(ev)
            if e.median() >= 0.10 and (e > 0).mean() >= 0.80:
                gg = g.reset_index(drop=True)
                r5_now = float(gg.mid.iloc[-1] / gg.mid.iloc[-6] - 1) if len(gg) > 6 else 0.0
                crash_rows.append({
                    "item_id": int(iid), "name": name_of.get(iid, str(iid)),
                    "crashes": len(ev), "med_ret": round(float(e.median()), 4),
                    "win": round(float((e > 0).mean()), 3), "worst": round(float(e.min()), 4),
                    "r5_now": round(r5_now, 4), "crashing_now": bool(r5_now <= -0.20),
                })
    rng_rows.sort(key=lambda x: (not x["at_band"], -x["med_ret"]))
    crash_rows.sort(key=lambda x: (not x["crashing_now"], -x["med_ret"]))
    out = {"range": rng_rows, "crash": crash_rows, "built_at": pd.Timestamp.utcnow().isoformat()}
    _CACHE["ts"], _CACHE["out"] = now, out
    return out

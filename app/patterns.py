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


def _swing_scan(con) -> list[dict]:
    """INTRADAY SWING LANES (discovered 2026-07-05 in the 5-min archive): items that reliably
    oscillate WITHIN the day. Standing bid at the item's own trailing-24h P20 (bid-side prints),
    standing ask at its P80 — ~0.5-3 cycles/day, backtested win 75-100% net of tax with
    depth-aware fills and a 48h stop. Per-lane capital is small (0.3-5M) but efficiency is
    extreme (5-50%/day on lane capital) and TOTAL capacity ~16M/day across stable lanes —
    the engine that scales with bankroll. Bonds excluded always (10% re-trade fee)."""
    items = con.execute("SELECT item_id, name, buy_limit FROM items").df()
    name_of = dict(zip(items.item_id, items["name"]))
    limit_of = dict(zip(items.item_id, items["buy_limit"]))
    uni = con.execute(
        """SELECT item_id, median(high_vol+low_vol) AS units_day, median((avg_high+avg_low)/2.0) AS mid
           FROM history WHERE timestep='24h' AND ts >= now() - INTERVAL 30 DAY
           GROUP BY item_id
           HAVING avg((avg_high+avg_low)/2.0*(high_vol+low_vol)) >= 25000000
              AND median((avg_high+avg_low)/2.0) BETWEEN 2000 AND 60000000
           ORDER BY avg((avg_high+avg_low)/2.0*(high_vol+low_vol)) DESC LIMIT 250"""
    ).df()
    uni = uni[~uni.item_id.map(name_of).str.lower().str.contains("bond", na=False)]
    if uni.empty:
        return []
    ids = ",".join(str(int(i)) for i in uni.item_id)
    sn = con.execute(
        f"""SELECT item_id, ts, avg_high, avg_low, high_vol, low_vol
            FROM snapshots WHERE item_id IN ({ids}) AND ts >= now() - INTERVAL 16 DAY
            ORDER BY item_id, ts"""
    ).df()
    sn["ts"] = pd.to_datetime(sn["ts"])
    W, share = 288, 0.5
    umeta = uni.set_index("item_id").to_dict("index")
    out = []
    for iid, g in sn.groupby("item_id"):
        g = g.reset_index(drop=True)
        if len(g) < W * 6:
            continue
        lo = g.avg_low.to_numpy("float64")
        hi = g.avg_high.to_numpy("float64")
        lv = g.low_vol.fillna(0).to_numpy("float64")
        hv = g.high_vol.fillna(0).to_numpy("float64")
        ts = g.ts.to_numpy()
        qb = pd.Series(lo).where(pd.Series(lv) > 0).rolling(W, min_periods=W // 2).quantile(0.20).shift(1).to_numpy()
        qs = pd.Series(hi).where(pd.Series(hv) > 0).rolling(W, min_periods=W // 2).quantile(0.80).shift(1).to_numpy()
        lim = limit_of.get(int(iid))
        lim = float(lim) if lim is not None and not pd.isna(lim) and lim > 0 else 100.0
        m = umeta[int(iid)]
        units = max(1.0, min(lim, 0.05 * float(m["units_day"])))
        state, buy_px, acc, t_in = 0, 0.0, 0.0, None
        cycles = []
        for i in range(W, len(g)):
            if np.isnan(qb[i]) or np.isnan(qs[i]):
                continue
            if state == 0:
                state, acc = 1, 0.0
            if state == 1:
                if lv[i] > 0 and lo[i] <= qb[i]:
                    acc += share * lv[i]
                    buy_px = qb[i]
                if acc >= units:
                    state, t_in = 2, ts[i]
            elif state == 2:
                if hv[i] > 0 and hi[i] >= qs[i]:
                    cycles.append(taxmod.net_sell(int(qs[i]), False) - buy_px)
                    state = 0
                elif t_in is not None and (ts[i] - t_in) / np.timedelta64(1, "h") > 48:
                    cycles.append((taxmod.net_sell(int(lo[i]), False) - buy_px) if lo[i] > 0 else -0.02 * buy_px)
                    state = 0
        if len(cycles) < 8:
            continue
        pnl = np.array(cycles) * units
        win = float((pnl > 0).mean())
        if win < 0.80:
            continue
        days = (ts[-1] - ts[W]) / np.timedelta64(1, "D")
        gpd = float(pnl.sum() / days)
        if gpd <= 0:
            continue
        capital = units * float(m["mid"])
        # live bands right now (last 24h of prints)
        tail = g.tail(W)
        b_now = tail.avg_low.where(tail.low_vol > 0).quantile(0.20)
        s_now = tail.avg_high.where(tail.high_vol > 0).quantile(0.80)
        out.append({
            "item_id": int(iid), "name": name_of.get(int(iid), str(iid)),
            "cycles": len(cycles), "cyc_day": round(len(cycles) / days, 2),
            "gp_day": round(gpd), "win": round(win, 3),
            "units": int(units), "capital": round(capital),
            "eff_pct_day": round(gpd / capital * 100, 2) if capital > 0 else None,
            "buy_band": round(float(b_now)) if pd.notna(b_now) else None,
            "sell_band": round(float(s_now)) if pd.notna(s_now) else None,
        })
    out.sort(key=lambda r: -(r["eff_pct_day"] or 0))
    return out[:20]


def rosters(con=None, cached_only: bool = False) -> dict | None:
    """Build (or serve cached) pattern rosters joined to live band positions. cached_only=True
    returns None instead of building (the planner must never block ~45s on a cold cache; the
    /api/patterns endpoint and the Proven tab are the warmers)."""
    now = time.time()
    if _CACHE["out"] is not None and now - _CACHE["ts"] < _TTL:
        return _CACHE["out"]
    if cached_only:
        return None
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
                # AT BAND means at/just under the band — a price 25%+ BELOW the band isn't an
                # oscillation entry, it's the range breaking down (League/event items collapsing
                # showed up here). Those get flagged broken, never buyable.
                broken = cur < p20 * 0.75
                rng_rows.append({
                    "item_id": int(iid), "name": name_of.get(iid, str(iid)),
                    "cycles": len(tr), "med_ret": round(float(r.median()), 4),
                    "win": round(float((r > 0).mean()), 3), "avg_days": round(held, 1),
                    "p20": round(p20), "p70": round(p70), "cur": round(cur),
                    "at_band": bool(cur <= p20 * 1.01 and not broken),
                    "broken": bool(broken),
                })
        ev = _sim_crash(g)
        if len(ev) >= 3:
            e = pd.Series(ev)
            if e.median() >= 0.10 and (e > 0).mean() >= 0.80:
                gg = g.reset_index(drop=True)
                r5_now = float(gg.mid.iloc[-1] / gg.mid.iloc[-6] - 1) if len(gg) > 6 else 0.0
                # the roster validated ~-20..-45% dips. A -50%+ collapse is OUTSIDE the historical
                # envelope (a regime break — event/League items dying), not the validated setup.
                crash_rows.append({
                    "item_id": int(iid), "name": name_of.get(iid, str(iid)),
                    "crashes": len(ev), "med_ret": round(float(e.median()), 4),
                    "win": round(float((e > 0).mean()), 3), "worst": round(float(e.min()), 4),
                    "r5_now": round(r5_now, 4),
                    "crashing_now": bool(-0.45 <= r5_now <= -0.20),
                    "broken": bool(r5_now < -0.45),
                })
    rng_rows.sort(key=lambda x: (not x["at_band"], -x["med_ret"]))
    crash_rows.sort(key=lambda x: (not x["crashing_now"], -x["med_ret"]))
    try:
        con2 = connect(read_only=True)
        try:
            swing_rows = _swing_scan(con2)
        finally:
            con2.close()
    except Exception:  # noqa: BLE001 — the swing scan must never break the roster build
        swing_rows = []
    out = {"range": rng_rows, "crash": crash_rows, "swing": swing_rows,
           "built_at": pd.Timestamp.utcnow().isoformat()}
    _CACHE["ts"], _CACHE["out"] = now, out
    return out

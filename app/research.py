"""One-off research on accumulated signal-log + real-trade data (run on the VPS).

    python -m app.research            # runs all four
    python -m app.research calib trades decay rotation

All forward-return numbers are NET OF COST (buy at the ask, sell at the bid, 2% sell
tax) — same conservative basis as the backtests, so a positive number is real edge.
"""
from __future__ import annotations

import sys
from collections import defaultdict, deque

import numpy as np
import pandas as pd

from . import tax as taxmod
from .db import connect, get_items_df, get_signal_log_df, get_trades_df, record_study_results
from .sectors import classify_one


def _items():
    it = get_items_df()
    return (dict(zip(it.item_id, it.name)),
            {int(i): bool(e) for i, e in zip(it.item_id, it.exempt)})


def _persist(study: str, rows: list[dict]) -> None:
    """Write a study's per-bucket diagnostics to study_results so calibration drift is trackable
    over time (not stdout-only). Best-effort — never let persistence break the printed diagnostic."""
    try:
        wrote = record_study_results(study, rows)
        print(f"  [persisted {wrote} {study} row(s) -> study_results]")
    except Exception as e:  # noqa: BLE001
        print(f"  [persist {study} failed: {e}]")


def _hist(item_ids, timestep, con):
    if not len(item_ids):
        return pd.DataFrame(columns=["item_id", "ts", "avg_high", "avg_low", "mid", "low_vol", "high_vol"])
    ids = ",".join(str(int(i)) for i in set(int(x) for x in item_ids))
    return con.execute(
        f"SELECT item_id, ts, CAST(avg_high AS DOUBLE) AS avg_high, CAST(avg_low AS DOUBLE) AS avg_low, "
        f"(CAST(avg_high AS DOUBLE)+CAST(avg_low AS DOUBLE))/2.0 AS mid, "
        f"CAST(low_vol AS DOUBLE) AS low_vol, CAST(high_vol AS DOUBLE) AS high_vol FROM history "
        f"WHERE timestep='{timestep}' AND item_id IN ({ids}) AND avg_high IS NOT NULL AND avg_low IS NOT NULL"
    ).df()


def _forward_net(row, h, exempt, fwd_days):
    """LIQUIDITY-FLOORED forward exit for one signal. The realized exit price is the avg_low of the LAST
    forward bar within (ts, ts+fwd_days] on which a real insta-sell actually happened (low_vol>0) — so a
    thin item that only ever printed a fantasy avg_low gets DROPPED, not counted as a 100%+ winner.
    Returns (ret_net, win, reached_target) net of the 2% sell tax, or None if there was no liquid exit."""
    if pd.isna(row.entry) or row.entry <= 0:
        return None
    fw = h[(h.item_id == row.item_id) & (h.ts > row.ts) & (h.ts <= row.ts + pd.Timedelta(days=fwd_days))]
    if fw.empty:
        return None
    liq = fw[fw.low_vol.fillna(0) > 0]            # only bars where someone actually sold = a real exit
    if liq.empty:
        return None
    entry = float(row.entry)
    end_bid = float(liq.sort_values("ts").avg_low.iloc[-1])
    net_end = taxmod.net_sell(int(round(end_bid)), exempt.get(int(row.item_id), False)) - entry
    reached = bool(pd.notna(row.target) and liq.mid.max() >= row.target)   # reached on a LIQUID bar
    return net_end / entry, net_end > 0, reached


def _block_ci(df, col, n_boot=1000, lo=5, hi=95, seed=12345):
    """Item-block bootstrap CI for the MEDIAN of df[col] — resample whole ITEMS (with replacement) so the
    interval respects same-item autocorrelation (overlapping signals on one item aren't independent N).
    Returns (ci_lo, ci_hi) percentiles of the bootstrap median distribution, or (None, None)."""
    if df is None or df.empty:
        return None, None
    rng = np.random.default_rng(seed)
    by_item = {i: g[col].to_numpy() for i, g in df.groupby("item_id")}
    items = np.array(list(by_item))
    meds = []
    for _ in range(n_boot):
        pick = rng.choice(items, size=len(items), replace=True)
        meds.append(np.median(np.concatenate([by_item[i] for i in pick])))
    return float(np.percentile(meds, lo)), float(np.percentile(meds, hi))


# --- 1) Invest calibration --------------------------------------------------
def calib(fwd_days: int = 3):
    sl = get_signal_log_df()
    _, exempt = _items()
    val = sl[sl.kind == "value"].sort_values("ts")
    first = val.groupby("item_id", as_index=False).first()
    last_ts = sl.ts.max()
    first = first[first.ts <= last_ts - pd.Timedelta(days=fwd_days)]
    con = connect(read_only=True)
    h = _hist(first.item_id.tolist(), "1h", con)
    con.close()
    rows = []
    for r in first.itertuples():
        graded = _forward_net(r, h, exempt, fwd_days)
        if graded is None:
            continue
        ret, win, reached = graded
        disc = ((r.established - r.mid) / r.established) if (pd.notna(r.established) and r.established) else np.nan
        rows.append({"item_id": int(r.item_id), "conf": r.score, "horizon": r.horizon, "disc": disc,
                     "ret_end": ret, "win_end": win, "reached_target": reached})
    d = pd.DataFrame(rows)
    print(f"\n=== [1] INVEST CALIBRATION — {len(d)} value signals w/ a LIQUID >={fwd_days}d forward exit (net of cost) ===")
    if d.empty:
        return d
    clo, chi = _block_ci(d, "ret_end")
    print(f"  overall: reached fair value {d.reached_target.mean()*100:.0f}% | profitable {d.win_end.mean()*100:.0f}% | "
          f"MEDIAN {fwd_days}d ret {d.ret_end.median()*100:+.1f}% (90% CI {clo*100:+.1f}..{chi*100:+.1f}%) | mean {d.ret_end.mean()*100:+.1f}% (diag, inflated)")
    d["bucket"] = pd.cut(d.conf, [0, 50, 65, 80, 101], labels=["<50", "50-65", "65-80", "80+"])
    srows = [{"kind": "value", "bucket": "all", "n": len(d), "win_rate": d.win_end.mean(),
              "mean_ret": d.ret_end.mean(), "median_ret": d.ret_end.median(),
              "ret_ci_lo": clo, "ret_ci_hi": chi, "reached_target": d.reached_target.mean()}]
    print("  by CONFIDENCE bucket (does higher confidence really pay? — MEDIAN net return, not mean):")
    for b in ["<50", "50-65", "65-80", "80+"]:
        s = d[d.bucket == b]
        if not len(s):
            continue
        blo, bhi = _block_ci(s, "ret_end")
        print(f"    {b:6} n={len(s):4}  reached {s.reached_target.mean()*100:3.0f}%  win {s.win_end.mean()*100:3.0f}%  "
              f"median{fwd_days}d {s.ret_end.median()*100:+5.1f}% (CI {blo*100:+.1f}..{bhi*100:+.1f}%)")
        srows.append({"kind": "value", "bucket": b, "n": int(len(s)), "win_rate": s.win_end.mean(),
                      "mean_ret": s.ret_end.mean(), "median_ret": s.ret_end.median(),
                      "ret_ci_lo": blo, "ret_ci_hi": bhi, "reached_target": s.reached_target.mean()})
    print("  by HORIZON (median):")
    for hz, s in d.groupby("horizon"):
        print(f"    {str(hz):10} n={len(s):4}  reached {s.reached_target.mean()*100:3.0f}%  median{fwd_days}d {s.ret_end.median()*100:+5.1f}%")
    _persist("calib", srows)
    return d


# --- 2) Real trade P&L ------------------------------------------------------
def trades():
    t = get_trades_df().sort_values("ts")
    name, exempt = _items()
    lots = defaultdict(deque)
    trips = []
    for r in t.itertuples():
        iid = int(r.item_id)
        if r.side == "buy":
            lots[iid].append([int(r.qty), float(r.price), r.ts])
        else:
            q, ex = int(r.qty), exempt.get(iid, False)
            net_px = taxmod.net_sell(int(round(r.price)), ex)
            while q > 0 and lots[iid]:
                lot = lots[iid][0]
                take = min(q, lot[0])
                trips.append({"item": name.get(iid, str(iid)), "sector": classify_one(name.get(iid, "")) or "(other)",
                              "pnl": (net_px - lot[1]) * take, "hold_h": (r.ts - lot[2]).total_seconds() / 3600})
                lot[0] -= take
                q -= take
                if lot[0] == 0:
                    lots[iid].popleft()
    tr = pd.DataFrame(trips)
    print(f"\n=== [2] REAL TRADE P&L — {len(t)} trades -> {len(tr)} matched round-trips ===")
    if tr.empty:
        print("  (no completed round-trips yet)")
        return tr
    wins = tr[tr.pnl > 0].pnl
    losses = tr[tr.pnl < 0].pnl
    print(f"  realized net P&L: {tr.pnl.sum():,.0f} | win rate {(tr.pnl > 0).mean()*100:.0f}% | "
          f"avg win {wins.mean() if len(wins) else 0:,.0f} | avg loss {losses.mean() if len(losses) else 0:,.0f} | median hold {tr.hold_h.median():.0f}h")
    print("  by sector:")
    for s, x in tr.groupby("sector").agg(n=("pnl", "size"), pnl=("pnl", "sum")).sort_values("pnl", ascending=False).iterrows():
        print(f"    {s:18} n={int(x.n):3}  pnl {x.pnl:>12,.0f}")
    # which traded items were also logged signals (rough source attribution)
    sl = get_signal_log_df()
    kinds_by_item = sl.groupby("item_id").kind.agg(lambda s: set(s)).to_dict()
    id_by_name = {v: k for k, v in name.items()}
    src = defaultdict(lambda: [0, 0.0])
    for s, x in tr.groupby("item").agg(n=("pnl", "size"), pnl=("pnl", "sum")).iterrows():
        ks = kinds_by_item.get(id_by_name.get(s), set()) or {"(unsignalled)"}
        for k in ks:
            src[k][0] += int(x.n)
            src[k][1] += x.pnl
    print("  by signal source (item appeared as this kind in the log):")
    for k, (n, p) in sorted(src.items(), key=lambda kv: -kv[1][1]):
        print(f"    {k:14} round-trips touching {n:3}  pnl {p:>12,.0f}")
    return tr


# --- 3) Signal decay --------------------------------------------------------
def decay(fwd_days: int = 3):
    sl = get_signal_log_df()
    _, exempt = _items()
    val = sl[sl.kind == "value"].copy()
    first = val.groupby("item_id").ts.transform("min")
    val["age_d"] = (val.ts - first).dt.total_seconds() / 86400
    last_ts = sl.ts.max()
    val = val[val.ts <= last_ts - pd.Timedelta(days=fwd_days)]
    # one snapshot per (item, integer age-day) to avoid over-weighting hourly dups
    val["age_bin"] = val.age_d.round().astype(int)
    samp = val.sort_values("ts").groupby(["item_id", "age_bin"], as_index=False).first()
    con = connect(read_only=True)
    h = _hist(samp.item_id.tolist(), "1h", con)
    con.close()
    rows = []
    for r in samp.itertuples():
        graded = _forward_net(r, h, exempt, fwd_days)
        if graded is None:
            continue
        ret, win, _ = graded
        rows.append({"item_id": int(r.item_id),
                     "age": "fresh (<1d)" if r.age_d < 1 else ("mid (1-3d)" if r.age_d < 3 else "stale (>=3d)"),
                     "ret": ret, "win": win})
    d = pd.DataFrame(rows)
    print(f"\n=== [3] SIGNAL DECAY — value signals' LIQUID {fwd_days}d forward return by how long they'd been showing ===")
    if d.empty:
        return d
    alo, ahi = _block_ci(d, "ret")
    srows = [{"kind": "value", "bucket": "all", "n": len(d), "win_rate": d.win.mean(),
              "mean_ret": d.ret.mean(), "median_ret": d.ret.median(), "ret_ci_lo": alo, "ret_ci_hi": ahi,
              "reached_target": None}]
    for a in ["fresh (<1d)", "mid (1-3d)", "stale (>=3d)"]:
        s = d[d.age == a]
        if len(s):
            slo, shi = _block_ci(s, "ret")
            print(f"    {a:12} n={len(s):4}  profitable {s.win.mean()*100:3.0f}%  median ret {s.ret.median()*100:+5.1f}% (CI {slo*100:+.1f}..{shi*100:+.1f}%)")
            srows.append({"kind": "value", "bucket": a, "n": int(len(s)), "win_rate": s.win.mean(),
                          "mean_ret": s.ret.mean(), "median_ret": s.ret.median(),
                          "ret_ci_lo": slo, "ret_ci_hi": shi, "reached_target": None})
    _persist("decay", srows)
    return d


# --- 4) Sector rotation -----------------------------------------------------
def rotation():
    from .signals import Thresholds, market_signals
    from . import sectormap
    d = market_signals(Thresholds())
    d = d[d.name.notna()].copy()
    d["sector"] = d.name.str.lower().map(sectormap.load_map())
    d["gpv"] = d.mid.fillna(0) * d.vol_daily_7d.fillna(0)
    d = d[d.sector.notna() & (d.gpv > 5_000_000)]
    con = connect(read_only=True)
    h = _hist(d.item_id.tolist(), "24h", con)
    con.close()
    sec = dict(zip(d.item_id, d.sector))
    gpv = dict(zip(d.item_id, d.gpv))
    h["sector"] = h.item_id.map(sec)
    h["ret"] = h.sort_values("ts").groupby("item_id").mid.pct_change().clip(-0.5, 0.5)
    h["w"] = h.item_id.map(gpv)
    # gp-weighted daily sector return
    h = h.dropna(subset=["ret", "sector"])
    sret = (h.assign(rw=h.ret * h.w).groupby(["sector", "ts"]).agg(rw=("rw", "sum"), w=("w", "sum")))
    sret["r"] = sret.rw / sret.w
    piv = sret.reset_index().pivot(index="ts", columns="sector", values="r").sort_index()
    # weekly (5-trading-day) compounded returns, pooled corr(week_t, week_{t+1})
    wk = (1 + piv).rolling(5).apply(np.prod, raw=True) - 1
    wk = wk.iloc[4::5]  # non-overlapping weeks
    pairs = []
    for s in wk.columns:
        v = wk[s].dropna().values
        for i in range(len(v) - 1):
            pairs.append((v[i], v[i + 1]))
    pairs = np.array(pairs)
    print("\n=== [4] SECTOR ROTATION — does a sector's weekly return predict next week? ===")
    if len(pairs) >= 8:
        c = np.corrcoef(pairs[:, 0], pairs[:, 1])[0, 1]
        sign = "MOMENTUM (winners keep winning)" if c > 0.1 else ("REVERSION (winners give back)" if c < -0.1 else "NO usable signal")
        print(f"  pooled corr(week_t, week_t+1) = {c:+.2f} over {len(pairs)} sector-week pairs -> {sign}")
    else:
        print(f"  only {len(pairs)} sector-week pairs — not enough history yet for week-over-week")
    # lag-1 DAILY autocorr as a shorter-window read
    dpairs = []
    for s in piv.columns:
        v = piv[s].dropna().values
        for i in range(len(v) - 1):
            dpairs.append((v[i], v[i + 1]))
    dpairs = np.array(dpairs)
    if len(dpairs) >= 30:
        dc = np.corrcoef(dpairs[:, 0], dpairs[:, 1])[0, 1]
        print(f"  daily lag-1 autocorr = {dc:+.2f} over {len(dpairs)} sector-day pairs "
              f"({'momentum' if dc > 0.05 else 'reversion' if dc < -0.05 else 'none'})")


# --- A) flip slippage / fill quality (real order data) ----------------------
def slippage():
    from .db import get_orders_df
    o = get_orders_df()
    name, _ = _items()
    o = o[o.filled_qty.fillna(0).astype(float) > 0].copy()
    print(f"\n=== [A] FLIP SLIPPAGE / FILL QUALITY — {len(o)} filled orders ===")
    if o.empty:
        return
    o["fill_pct"] = o.filled_qty.astype(float) / o.total_qty.astype(float).replace(0, np.nan)
    full = (o.fill_pct >= 0.999).mean()
    print(f"  fill completeness: {full*100:.0f}% fully filled | median {o.fill_pct.median()*100:.0f}% of order qty")
    con = connect(read_only=True)
    h = _hist(o.item_id.tolist(), "1h", con).sort_values("ts")
    con.close()
    rows = []
    for r in o[o.side == "buy"].itertuples():
        filled = float(r.filled_qty or 0)
        if filled <= 0 or pd.isna(r.spent):
            continue
        avg_fill = float(r.spent) / filled
        hh = h[(h.item_id == int(r.item_id)) & (h.ts <= r.opened_ts)]
        if hh.empty:
            continue
        mid = float(hh.mid.iloc[-1])
        rows.append({"below_mid": (mid - avg_fill) / mid, "at_or_under_offer": avg_fill <= float(r.price) * 1.0005})
    b = pd.DataFrame(rows)
    if len(b):
        print(f"  BUY fills ({len(b)}): bought a mean {b.below_mid.mean()*100:+.1f}% vs market mid at placement "
              f"(median {b.below_mid.median()*100:+.1f}%; positive = below mid = good)")
        print(f"    filled at/under your offer price: {b.at_or_under_offer.mean()*100:.0f}%")
        print("    -> predicted flip margins assume you buy at the bid; gap from mid here is the realistic buy-leg capture")
    srows = [{"kind": "flip", "bucket": "fill_completeness", "n": int(len(o)),
              "win_rate": None, "mean_ret": o.fill_pct.median(), "reached_target": full}]
    if len(b):
        srows.append({"kind": "flip", "bucket": "buy_capture_vs_mid", "n": int(len(b)),
                      "win_rate": b.at_or_under_offer.mean(), "mean_ret": b.below_mid.mean(),
                      "reached_target": None})
    _persist("slippage", srows)


# --- B) capital velocity (gp/hour) ------------------------------------------
def velocity():
    from .signals import Thresholds, flip_table
    t = get_trades_df().sort_values("ts")
    _, exempt = _items()
    lots, trips = defaultdict(deque), []
    for r in t.itertuples():
        iid = int(r.item_id)
        if r.side == "buy":
            lots[iid].append([int(r.qty), float(r.price), r.ts])
        else:
            q, ex = int(r.qty), exempt.get(iid, False)
            net = taxmod.net_sell(int(round(r.price)), ex)
            while q > 0 and lots[iid]:
                lot = lots[iid][0]
                take = min(q, lot[0])
                hrs = (r.ts - lot[2]).total_seconds() / 3600
                if hrs > 0.05:
                    trips.append({"gph": (net - lot[1]) * take / hrs, "hrs": hrs})
                lot[0] -= take
                q -= take
                if lot[0] == 0:
                    lots[iid].popleft()
    tr = pd.DataFrame(trips)
    print("\n=== [B] CAPITAL VELOCITY (gp/hour) ===")
    if len(tr):
        print(f"  your round-trips: median hold {tr.hrs.median():.1f}h | median {tr.gph.median():,.0f} gp/hr | best {tr.gph.max():,.0f} gp/hr")
    fl = pd.DataFrame(flip_table(Thresholds(), limit=250))
    if fl.empty:
        print("  no current flips to rank")
        return
    hv = fl["vol_daily_7d"].fillna(0) / 24.0                       # units traded per hour (whole market)
    fill_h = np.where(hv > 0, fl["buy_limit"].fillna(0) / hv, np.inf)
    cycle_h = np.clip(2 * fill_h, 0.5, 240)                        # buy leg + sell leg, floored/capped
    fl["gp_per_h"] = (fl["net_margin"].fillna(0) * fl["buy_limit"].fillna(0)) / cycle_h
    fl["est_cycle_h"] = cycle_h
    raw = fl.sort_values("realistic_profit", ascending=False).head(12)["name"].tolist()
    vel = fl.sort_values("gp_per_h", ascending=False)
    print("  current flips, top 10 by modeled gp/HOUR (turnover-adjusted) vs raw profit/cycle:")
    for r in vel.head(10).itertuples():
        flag = "" if r.name in raw else "  <- NOT in raw-profit top 12"
        print(f"    {str(r.name)[:24]:24} gp/hr {r.gp_per_h:>11,.0f}  cycle ~{r.est_cycle_h:4.1f}h  profit/cycle {r.realistic_profit:>10,.0f}{flag}")
    print("  -> if the velocity list differs a lot from the raw list, a gp/hour rank would recycle capital faster")


def main():
    which = sys.argv[1:] or ["calib", "trades", "decay", "rotation"]
    if "calib" in which: calib()
    if "trades" in which: trades()
    if "decay" in which: decay()
    if "rotation" in which: rotation()
    if "slippage" in which: slippage()
    if "velocity" in which: velocity()


if __name__ == "__main__":
    main()

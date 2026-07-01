"""Overnight-flip study.

Two strategies, fixed-time (no hindsight cherry-picking), all hours UTC:

  mode="market": buy at the evening price, sell at the next-day price. Tests a
    directional "prices rise overnight" bet.
  mode="limit":  place a LOWBALL buy at (evening bid x (1 - disc)); it only fills
    if the price dips to it overnight (you catch a dump while asleep); then sell
    the next day. This is dip-catching = the reversion edge, executed overnight.

Fills:
  * fill="spread" (CONSERVATIVE): market buy at the ask / sell at the bid (pay the
    spread). For limit mode the buy is your lowball; the sell hits the next-day bid.
  * fill="mid": buy at the bid / sell at the ask (capture the spread).

A real edge must clear the 2% tax on the conservative fill. Run on the VPS:
    python -m app.overnight --sweep                       # market, hour grid
    python -m app.overnight --mode limit --sweep          # lowball, discount grid
"""
from __future__ import annotations

import argparse
import logging

import numpy as np
import pandas as pd

from . import tax as taxmod
from .db import connect, get_items_df

# Fill-probability calibration: realized ≈ ONFILL_A + ONFILL_B × raw_backtest_rate.
# Fitted 2026-07-01 against 347 logged overnight item-nights (see fill_stats docwork);
# re-measure with `python -m app.research onfill` and update when the fit drifts.
ONFILL_A = 0.15
ONFILL_B = 0.72

log = logging.getLogger("overnight")
WINSOR = 0.50


def _load(con, timestep: str = "1h") -> pd.DataFrame:
    return con.execute(
        """
        SELECT item_id, ts, avg_high, avg_low,
               CAST(date_part('hour', ts) AS INTEGER) AS hour,
               (COALESCE(high_vol, 0) + COALESCE(low_vol, 0)) AS vol
        FROM history
        WHERE timestep = '1h' AND avg_high IS NOT NULL AND avg_low IS NOT NULL
        ORDER BY item_id, ts
        """
    ).df()


def backtest_item(g: pd.DataFrame, buy_hour: int, sell_hour: int, fill: str, exempt: bool,
                  mode: str = "market", disc: float = 0.05) -> list[dict]:
    g = g.sort_values("ts").reset_index(drop=True)
    n = len(g)
    hour = g["hour"].to_numpy()
    hi = g["avg_high"].to_numpy(dtype="float64")
    lo = g["avg_low"].to_numpy(dtype="float64")
    ts = g["ts"].to_numpy()
    trades: list[dict] = []
    for i in range(n):
        if hour[i] != buy_hour:
            continue
        # next sell_hour bar within ~36h
        sj = -1
        for j in range(i + 1, n):
            if (ts[j] - ts[i]) > np.timedelta64(36, "h"):
                break
            if hour[j] == sell_hour:
                sj = j
                break
        if sj < 0 or np.isnan(hi[sj]) or np.isnan(lo[sj]):
            continue
        if mode == "market":
            buy = hi[i] if fill == "spread" else lo[i]
            filled = not np.isnan(buy)
        else:  # limit / lowball: only fills if the overnight low reaches your offer
            if np.isnan(lo[i]):
                continue
            buy = lo[i] * (1.0 - disc)
            window = lo[i + 1: sj + 1]
            filled = window.size > 0 and np.nanmin(window) <= buy
        if not filled or not (buy > 0):
            trades.append({"filled": 0})
            continue
        sell = lo[sj] if fill == "spread" else hi[sj]
        net = taxmod.net_sell(int(round(sell)), exempt) - buy
        trades.append({"net": net, "ret": net / buy, "filled": 1})
    return trades


def run(buy_hour: int, sell_hour: int, fill: str = "spread", mode: str = "market", disc: float = 0.05,
        min_gp_vol: float = 50_000_000.0, min_price: float = 1000.0, hist=None, items=None, con=None) -> pd.DataFrame:
    if hist is None or items is None:
        own = con is None
        con = con or connect(read_only=True)
        try:
            hist = _load(con)
            items = get_items_df(con).set_index("item_id")
        finally:
            if own:
                con.close()
    if hist.empty:
        return pd.DataFrame()
    rows: list[dict] = []
    for iid, g in hist.groupby("item_id"):
        if g["avg_high"].mean() * g["vol"].mean() * 24.0 < min_gp_vol or g["avg_high"].mean() < min_price:
            continue
        exempt = bool(items.loc[iid, "exempt"]) if iid in items.index else False
        for t in backtest_item(g, buy_hour, sell_hour, fill, exempt, mode, disc):
            t["item_id"] = int(iid)
            rows.append(t)
    return pd.DataFrame(rows)


def summarize(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"trades": 0, "fills": 0}
    fills = tr[tr["filled"] == 1] if "filled" in tr.columns else tr
    out = {"placed": len(tr), "fills": len(fills), "fill_rate": len(fills) / len(tr) if len(tr) else 0.0}
    if fills.empty:
        return out
    r = fills["ret"].clip(-WINSOR, WINSOR)       # PF on winsorized returns, consistent with avg_return
    wins = r[r > 0].sum()
    losses = r[r < 0].sum()
    out.update({
        "items": fills["item_id"].nunique(),
        "win_rate": (fills["net"] > 0).mean(),
        "avg_return": fills["ret"].clip(-WINSOR, WINSOR).mean(),
        "median_return": fills["ret"].median(),
        "profit_factor": (wins / abs(losses)) if losses < 0 else float("inf"),
    })
    return out


def fill_stats(item_ids, con, disc: float, buy_hour: int = 2, sell_hour: int = 14,
               window_h: int = 12, min_nights: int = 5, exempt_map: dict | None = None) -> dict:
    """Per-item historical overnight behaviour, for the live page:
      * fill_prob  -- fraction of past nights a lowball buy at (evening bid x (1-disc))
                      would have filled by morning (overnight low reached it);
      * win_rate   -- of the nights it filled, fraction where selling next midday profits;
      * exp_margin -- median realised after-tax margin per unit on filled nights.
    Hours are UTC; defaults map to ~9pm place / ~9am sell US Central (CDT = UTC-5),
    with a ~12h overnight fill window (place 02:00 UTC -> check ~13:00 UTC)."""
    ids = [int(i) for i in item_ids]
    if not ids:
        return {}
    ph = ",".join(str(i) for i in ids)
    h = con.execute(
        f"""SELECT item_id, ts, avg_high, avg_low, low_vol,
                   CAST(date_part('hour', ts) AS INTEGER) AS hour
            FROM history WHERE timestep = '1h' AND item_id IN ({ph})
              AND avg_high IS NOT NULL AND avg_low IS NOT NULL ORDER BY item_id, ts"""
    ).df()
    out: dict = {}
    for iid, g in h.groupby("item_id"):
        g = g.reset_index(drop=True)
        lo = g["avg_low"].to_numpy("float64")
        hi = g["avg_high"].to_numpy("float64")
        lv = g["low_vol"].fillna(0).to_numpy("float64")
        hr = g["hour"].to_numpy()
        # epoch HOURS ([us]-safe) so the fill window is measured in TIME, not rows: slicing rows
        # silently stretched the "overnight" window across history gaps, crediting daytime dips as
        # overnight fills — the main source of the measured over-prediction (44% vs 32% realized).
        eh = g["ts"].to_numpy().astype("datetime64[h]").astype("int64")
        n = len(g)
        ex = bool(exempt_map.get(int(iid))) if exempt_map else False
        nights = fills = wins = 0
        margins: list[float] = []
        for i in range(n):
            if hr[i] != buy_hour or np.isnan(lo[i]):
                continue
            nights += 1
            offer = lo[i] * (1.0 - disc)
            j = i + 1
            filled = False
            while j < n and eh[j] - eh[i] <= window_h:
                # a fill needs a TRADED dip: someone actually insta-sold at/below the offer that
                # hour (a quote drifting down with zero prints can't fill anything)
                if lo[j] <= offer and lv[j] > 0:
                    filled = True
                    break
                j += 1
            if not filled:
                continue
            fills += 1
            sells = [k for k in range(i + 1, min(i + 30, n)) if hr[k] == sell_hour and not np.isnan(hi[k])]
            if sells:
                m = taxmod.net_sell(int(round(hi[sells[0]])), ex) - offer   # after-tax margin (cap + exemptions)
                margins.append(m)
                if m > 0:
                    wins += 1
        if nights >= min_nights:
            raw = fills / nights
            out[int(iid)] = {
                # empirical calibration to REALIZED fills (347 logged item-nights, 2026-07-01):
                # the raw backtest under-predicts non-uniformly (raw 0.18 -> 0.28 realized,
                # 0.36 -> 0.41) because live lowballs anchor off the instantaneous evening bid
                # while the backtest anchors off the 2AM bar's AVERAGE low (deeper offer, fewer
                # fills). Linear map fitted on the two well-populated buckets; re-measure with
                # `python -m app.research onfill` as new graded nights accumulate.
                "fill_prob": float(min(0.95, max(0.05, ONFILL_A + ONFILL_B * raw))),
                "fill_prob_raw": raw,
                "win_rate": (wins / fills) if fills else None,
                "exp_margin": float(np.median(margins)) if margins else None,
                "nights": int(nights),
            }
    return out


DISC_GRID = (0.04, 0.06, 0.08, 0.10, 0.12, 0.15)
SWEEP_LOOKBACK_DAYS = 180   # recent regime; also keeps the per-plan sweep affordable on 1 vCPU
_SWEEP_CACHE: dict[int, tuple[float, dict]] = {}   # item_id -> (expiry_epoch, result)
_SWEEP_TTL = 3600.0         # fill odds move nightly, not per page view


def sweep_fill_stats(item_ids, con, grid=DISC_GRID, buy_hour: int = 2, sell_hour: int = 14,
                     window_h: int = 12, min_nights: int = 5, exempt_map: dict | None = None) -> dict:
    """Per-item EV-MAXIMIZING lowball discount. One global discount treats a placid consumable
    and a swingy weapon identically; each item has its own sweet spot on the fill-vs-margin
    curve. For every night we precompute the deepest TRADED dip and the next-day sell, then
    grade the whole discount grid against it: EV/unit = calibrated_fill x win_rate x median
    margin. Overfitting guards: a discount needs >= 4 historical fills to be eligible, and we
    take the SHALLOWEST discount within 10% of the best EV (shallower = more fills = the more
    robust estimate of two similar EVs). Returns {item_id: {disc, fill_prob, fill_prob_raw,
    win_rate, exp_margin, nights, ev_unit}} for the chosen discount."""
    import time as _time

    ids = [int(i) for i in item_ids]
    if not ids:
        return {}
    now = _time.time()
    out: dict = {}
    fresh: list[int] = []
    for i in ids:                      # serve from the TTL cache; sweep only the missing items
        hit = _SWEEP_CACHE.get(i)
        if hit and hit[0] > now:
            if hit[1]:
                out[i] = hit[1]
        else:
            fresh.append(i)
    if not fresh:
        return out
    ph = ",".join(str(i) for i in fresh)
    h = con.execute(
        f"""SELECT item_id, ts, avg_high, avg_low, low_vol,
                   CAST(date_part('hour', ts) AS INTEGER) AS hour
            FROM history WHERE timestep = '1h' AND item_id IN ({ph})
              AND ts >= now() - INTERVAL {SWEEP_LOOKBACK_DAYS} DAY
              AND avg_high IS NOT NULL AND avg_low IS NOT NULL ORDER BY item_id, ts"""
    ).df()
    G = len(grid)
    swept: set[int] = set()
    for iid, g in h.groupby("item_id"):
        g = g.reset_index(drop=True)
        lo = g["avg_low"].to_numpy("float64")
        hi = g["avg_high"].to_numpy("float64")
        lv = g["low_vol"].fillna(0).to_numpy("float64")
        hr = g["hour"].to_numpy()
        eh = g["ts"].to_numpy().astype("datetime64[h]").astype("int64")
        n = len(g)
        ex = bool(exempt_map.get(int(iid))) if exempt_map else False
        nights = 0
        fills = [0] * G
        wins = [0] * G
        margins: list[list[float]] = [[] for _ in range(G)]
        for i in range(n):
            if hr[i] != buy_hour or np.isnan(lo[i]):
                continue
            nights += 1
            # deepest TRADED dip within the overnight window (time-indexed, not row-indexed)
            j = i + 1
            deep = np.inf
            while j < n and eh[j] - eh[i] <= window_h:
                if lv[j] > 0 and lo[j] < deep:
                    deep = lo[j]
                j += 1
            if not np.isfinite(deep):
                continue
            sells = [k for k in range(i + 1, min(i + 30, n)) if hr[k] == sell_hour and not np.isnan(hi[k])]
            sell_px = hi[sells[0]] if sells else None
            for d_idx, disc in enumerate(grid):
                offer = lo[i] * (1.0 - disc)
                if deep > offer:
                    continue
                fills[d_idx] += 1
                if sell_px is not None:
                    m = taxmod.net_sell(int(round(sell_px)), ex) - offer
                    margins[d_idx].append(m)
                    if m > 0:
                        wins[d_idx] += 1
        if nights < min_nights:
            continue
        evs = []
        for d_idx, disc in enumerate(grid):
            if fills[d_idx] < 4:            # too few historical fills to trust this depth
                evs.append(None)
                continue
            raw = fills[d_idx] / nights
            cal = min(0.95, max(0.05, ONFILL_A + ONFILL_B * raw))
            wr = wins[d_idx] / fills[d_idx]
            med = float(np.median(margins[d_idx])) if margins[d_idx] else 0.0
            evs.append((cal * wr * max(med, 0.0), disc, cal, raw, wr, med))
        valid = [e for e in evs if e is not None and e[0] > 0]
        if not valid:
            continue
        best_ev = max(e[0] for e in valid)
        # shallowest discount within 10% of the best EV (grid is ascending)
        pick = next(e for e in valid if e[0] >= 0.9 * best_ev)
        ev, disc, cal, raw, wr, med = pick
        res = {
            "disc": float(disc), "fill_prob": float(cal), "fill_prob_raw": float(raw),
            "win_rate": float(wr), "exp_margin": med, "nights": int(nights), "ev_unit": float(ev),
        }
        out[int(iid)] = res
        _SWEEP_CACHE[int(iid)] = (now + _SWEEP_TTL, res)
        swept.add(int(iid))
    # negative-cache items that produced no valid depth, so they don't re-sweep every plan view
    for i in fresh:
        if i not in swept:
            _SWEEP_CACHE[i] = (now + _SWEEP_TTL, {})
    return out


def _pct(x):
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x * 100:.1f}%"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Overnight-flip backtest.")
    ap.add_argument("--buy-hour", type=int, default=1)
    ap.add_argument("--sell-hour", type=int, default=14)
    ap.add_argument("--fill", default="spread", choices=["spread", "mid"])
    ap.add_argument("--mode", default="market", choices=["market", "limit"])
    ap.add_argument("--disc", type=float, default=0.05)
    ap.add_argument("--min-gp-vol", type=float, default=50_000_000.0)
    ap.add_argument("--min-price", type=float, default=1000.0)
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    con = connect(read_only=True)
    try:
        hist = _load(con)
        items = get_items_df(con).set_index("item_id")
    finally:
        con.close()
    base = dict(min_gp_vol=args.min_gp_vol, min_price=args.min_price, hist=hist, items=items)
    print(f"Overnight {args.mode} -- fill={args.fill}; liquid >= {args.min_gp_vol:,.0f} gp/day. Hours UTC.\n")

    if args.sweep and args.mode == "limit":
        print(f"buy {args.buy_hour:02d} -> sell {args.sell_hour:02d} next day; lowball at evening bid x (1-disc)")
        print(f"{'disc':>6}{'placed':>8}{'fills':>7}{'fill%':>7}{'win':>7}{'avg ret':>9}{'median':>9}{'PF':>7}")
        for fl in ("spread", "mid"):
            print(f"  -- fill={fl} --")
            for d in (0.02, 0.05, 0.10, 0.20):
                s = summarize(run(args.buy_hour, args.sell_hour, fl, "limit", d, **base))
                if s.get("fills"):
                    pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
                    print(f"{d * 100:>5.0f}%{s['placed']:>8}{s['fills']:>7}{_pct(s['fill_rate']):>7}{_pct(s['win_rate']):>7}"
                          f"{_pct(s['avg_return']):>9}{_pct(s['median_return']):>9}{pf:>7}")
                else:
                    print(f"{d * 100:>5.0f}%{s['placed']:>8}{0:>7}")
        return

    if args.sweep:
        print(f"{'buy->sell':>10}{'trades':>8}{'win':>7}{'avg ret':>9}{'PF':>7}")
        for bh in (23, 1, 3, 5):
            for sh in (13, 15, 17):
                s = summarize(run(bh, sh, args.fill, "market", 0.0, **base))
                if s.get("fills"):
                    pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
                    print(f"{f'{bh:02d}->{sh:02d}':>10}{s['fills']:>8}{_pct(s['win_rate']):>7}{_pct(s['avg_return']):>9}{pf:>7}")
        return

    s = summarize(run(args.buy_hour, args.sell_hour, args.fill, args.mode, args.disc, **base))
    print(s)


if __name__ == "__main__":
    main()

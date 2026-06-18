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
        f"""SELECT item_id, ts, avg_high, avg_low, CAST(date_part('hour', ts) AS INTEGER) AS hour
            FROM history WHERE timestep = '1h' AND item_id IN ({ph})
              AND avg_high IS NOT NULL AND avg_low IS NOT NULL ORDER BY item_id, ts"""
    ).df()
    out: dict = {}
    for iid, g in h.groupby("item_id"):
        g = g.reset_index(drop=True)
        lo = g["avg_low"].to_numpy("float64")
        hi = g["avg_high"].to_numpy("float64")
        hr = g["hour"].to_numpy()
        n = len(g)
        ex = bool(exempt_map.get(int(iid))) if exempt_map else False
        nights = fills = wins = 0
        margins: list[float] = []
        for i in range(n):
            if hr[i] != buy_hour or np.isnan(lo[i]):
                continue
            nights += 1
            offer = lo[i] * (1.0 - disc)
            w = lo[i + 1: i + 1 + window_h]
            if w.size == 0 or np.nanmin(w) > offer:
                continue  # never dipped to the offer overnight
            fills += 1
            sells = [j for j in range(i + 1, min(i + 30, n)) if hr[j] == sell_hour and not np.isnan(hi[j])]
            if sells:
                m = taxmod.net_sell(int(round(hi[sells[0]])), ex) - offer   # after-tax margin (cap + exemptions)
                margins.append(m)
                if m > 0:
                    wins += 1
        if nights >= min_nights:
            out[int(iid)] = {
                "fill_prob": fills / nights,
                "win_rate": (wins / fills) if fills else None,
                "exp_margin": float(np.median(margins)) if margins else None,
                "nights": int(nights),
            }
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
